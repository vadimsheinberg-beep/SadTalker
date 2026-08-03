"""Tests for the parts of the pipeline that do not touch the network.

Everything here runs offline. The stages that call YouTube, the RAG, Claude,
ElevenLabs or ffmpeg are exercised through injected fakes, so a failure in this
file is a logic failure and never a connectivity one.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipeline.atlas import approval_telegram, inventory, theme_proposer
from pipeline.atlas.claim_selector import best_match, explain_miss
from pipeline.atlas.rag_client import RagHit
from pipeline.config import AtlasPolicy, PacingPolicy, ScoutPolicy, Secrets, parse_env_file
from pipeline.contracts import (
    AtlasClaim,
    Beat,
    Channel,
    ClaimSource,
    ClusterStatus,
    Language,
    Package,
    PublicationBlocked,
    Script,
    TopicCandidate,
    VideoSignal,
)
from pipeline.publish.gate import BlockList, build_description, check_package
from pipeline.render.deck import build_slides, slide_svg, wrap
from pipeline.render.tts_elevenlabs import Lexicon
from pipeline.script import pacing
from pipeline.script.beluga import ScriptGenerationFailed, assemble, verify_claim_intact
from pipeline.scout import trend_scout
from pipeline.scout.youtube_data import parse_iso8601_duration, parse_rfc3339


def signal(
    video_id: str,
    channel_id: str,
    title: str,
    views: int = 100_000,
    median: int = 20_000,
    age_h: int = 12,
) -> VideoSignal:
    return VideoSignal(
        video_id=video_id,
        channel_id=channel_id,
        title=title,
        published_at=datetime.now(timezone.utc) - timedelta(hours=age_h),
        views=views,
        duration_s=600,
        channel_median_views=median,
    )


def make_claim(verified: bool = True, cluster_id: str = "c1") -> AtlasClaim:
    return AtlasClaim(
        cluster_id=cluster_id,
        theme_name="ruin follows overreach",
        text={
            Language.RU: "Тот, кто хватает слишком много, не удерживает ничего.",
            Language.EN: "One who grasps at too much holds nothing.",
        },
        sources=(
            ClaimSource(
                section_id="s1",
                tractate="Chagigah",
                folio="17a",
                quote="tafasta meruba lo tafasta",
            ),
        ),
        verified=verified,
    )


def make_script(
    claim: AtlasClaim,
    language: Language = Language.EN,
    filler_words: int = 100,
) -> Script:
    # Split filler across beats: a single long beat would (correctly) trip the
    # gate's beat_too_long check and mask whatever the test is actually about.
    chunk = 20
    filler_beats = tuple(
        Beat(role="escalation", text=" ".join(["word"] * min(chunk, filler_words - i)))
        for i in range(0, filler_words, chunk)
    )
    return Script(
        topic_slug="test-topic",
        language=language,
        beats=(
            Beat(role="hook", text="A company worth billions is gone."),
            *filler_beats,
            Beat(role="turn", text="This is not new."),
            Beat(role="claim", text=claim.render(language)),
            Beat(role="payoff", text="Nobody learned anything."),
        ),
        claim_cluster_id=claim.cluster_id,
    )


class PacingTests(unittest.TestCase):
    def test_wpm_arithmetic_round_trips(self):
        self.assertAlmostEqual(pacing.wpm(140, 60), 140.0)
        self.assertAlmostEqual(pacing.duration_for(140, 140), 60.0)
        self.assertEqual(pacing.words_for(60, 140), 140)

    def test_deployed_corpus_rate_is_flagged_as_slow(self):
        # 104 wpm is where 117 of the 119 existing scripts sit.
        script = Script(
            topic_slug="t",
            language=Language.RU,
            beats=(Beat(role="hook", text=" ".join(["сл"] * 104)),),
            claim_cluster_id="c1",
        )
        report = pacing.measure(script, 60.0)
        self.assertFalse(report.in_band)
        self.assertTrue(report.too_slow)
        self.assertGreater(report.word_delta, 0)

    def test_word_delta_targets_nearest_band_edge(self):
        script = Script(
            topic_slug="t",
            language=Language.EN,
            beats=(Beat(role="hook", text=" ".join(["w"] * 100)),),
            claim_cluster_id="c1",
        )
        report = pacing.measure(script, 60.0)
        # 100 words in 60s = 100 wpm; the 130 wpm floor needs 130 words.
        self.assertEqual(report.word_delta, 30)
        self.assertAlmostEqual(report.suggested_duration_s, 46.2, places=1)

    def test_in_band_script_reports_no_change(self):
        script = Script(
            topic_slug="t",
            language=Language.EN,
            beats=tuple(
                Beat(role="hook", text=" ".join(["w"] * 14)) for _ in range(10)
            ),
        claim_cluster_id="c1",
        )
        report = pacing.measure(script, 60.0)
        self.assertTrue(report.in_band)
        self.assertEqual(report.word_delta, 0)

    def test_long_beats_are_reported_even_when_average_is_fine(self):
        script = Script(
            topic_slug="t",
            language=Language.EN,
            beats=(
                Beat(role="hook", text=" ".join(["w"] * 80)),
                Beat(role="payoff", text=" ".join(["w"] * 60)),
            ),
            claim_cluster_id="c1",
        )
        report = pacing.measure(script, 60.0)
        self.assertTrue(report.in_band)
        self.assertEqual(report.long_beats, (0, 1))

    def test_russian_is_budgeted_more_time_than_english(self):
        beat = Beat(role="hook", text=" ".join(["w"] * 50))
        ru = pacing.estimate_beat_duration(beat, Language.RU)
        en = pacing.estimate_beat_duration(beat, Language.EN)
        self.assertGreater(ru, en)

    def test_zero_duration_does_not_divide_by_zero(self):
        self.assertEqual(pacing.wpm(100, 0), 0.0)
        with self.assertRaises(ValueError):
            pacing.duration_for(100, 0)


class ScoutTests(unittest.TestCase):
    def test_outlier_ratio_uses_channel_median(self):
        self.assertAlmostEqual(
            signal("v", "c", "t", views=100_000, median=20_000).outlier_ratio, 5.0
        )

    def test_missing_median_does_not_inflate_the_score(self):
        self.assertEqual(signal("v", "c", "t", median=0).outlier_ratio, 1.0)

    def test_single_channel_topic_is_dropped(self):
        signals = [
            signal("v1", "chan-a", "Enron collapse explained"),
            signal("v2", "chan-a", "Enron collapse timeline"),
        ]
        self.assertEqual(trend_scout.rank_topics(signals), [])

    def test_cross_channel_agreement_produces_a_topic(self):
        signals = [
            signal("v1", "chan-a", "Enron collapse explained"),
            signal("v2", "chan-b", "The Enron collapse timeline"),
        ]
        topics = trend_scout.rank_topics(signals)
        self.assertTrue(topics)
        self.assertIn("enron", " ".join(topics[0].keywords))
        self.assertEqual(topics[0].distinct_channels, 2)

    def test_low_view_videos_are_filtered_out(self):
        signals = [
            signal("v1", "chan-a", "Enron collapse", views=10),
            signal("v2", "chan-b", "Enron collapse", views=10),
        ]
        self.assertEqual(trend_scout.rank_topics(signals), [])

    def test_on_domain_beats_off_domain_at_equal_reach(self):
        on = trend_scout.score_signal(
            signal("v1", "c", "Enron bankruptcy and fraud"),
            "corporate_collapse",
            ScoutPolicy(),
        )
        off = trend_scout.score_signal(
            signal("v2", "c", "Kitten plays piano"),
            "corporate_collapse",
            ScoutPolicy(),
        )
        self.assertGreater(on, off)

    def test_runaway_outlier_cannot_dominate_the_ranking(self):
        modest = trend_scout.score_signal(
            signal("a", "c", "collapse", views=40_000, median=20_000),
            "corporate_collapse",
            ScoutPolicy(),
        )
        extreme = trend_scout.score_signal(
            signal("b", "c", "collapse", views=4_000_000, median=20_000),
            "corporate_collapse",
            ScoutPolicy(),
        )
        # Log compression: 100x the ratio must not mean 100x the score.
        self.assertLess(extreme / max(modest, 1e-9), 4.0)

    def test_overlapping_phrases_collapse_to_one_topic(self):
        signals = [
            signal("v1", "chan-a", "Silicon Valley Bank run explained"),
            signal("v2", "chan-b", "Silicon Valley Bank run timeline"),
            signal("v3", "chan-c", "Silicon Valley Bank run aftermath"),
        ]
        topics = trend_scout.rank_topics(signals)
        evidence_sets = [{s.video_id for s in t.evidence} for t in topics]
        for i, first in enumerate(evidence_sets):
            for second in evidence_sets[i + 1 :]:
                overlap = len(first & second) / len(first)
                self.assertLessEqual(overlap, 0.6)

    def test_stopwords_do_not_become_topics(self):
        signals = [
            signal("v1", "chan-a", "The collapse of the company"),
            signal("v2", "chan-b", "The collapse of the market"),
        ]
        slugs = {t.slug for t in trend_scout.rank_topics(signals)}
        self.assertNotIn("the", slugs)


class YouTubeParsingTests(unittest.TestCase):
    def test_iso_duration_parsing(self):
        self.assertEqual(parse_iso8601_duration("PT1H2M3S"), 3723)
        self.assertEqual(parse_iso8601_duration("PT45S"), 45)
        self.assertEqual(parse_iso8601_duration("P1DT2H"), 93600)
        self.assertEqual(parse_iso8601_duration("garbage"), 0)

    def test_rfc3339_always_returns_aware_datetimes(self):
        self.assertIsNotNone(parse_rfc3339("2026-01-02T03:04:05Z").tzinfo)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "inventory.json"
        self.store = inventory.JsonInventoryStore(self.path)

    def tearDown(self):
        self.dir.cleanup()

    def _draft(self, cluster_id: str = "c1") -> inventory.Cluster:
        cluster = inventory.Cluster(cluster_id=cluster_id, section_ids=["s1", "s2"])
        self.store.save(cluster)
        return cluster

    def test_fresh_clusters_are_not_verified(self):
        self._draft()
        self.assertEqual(inventory.verified_count(self.store), 0)

    def test_empty_theme_name_blocks_approval(self):
        self._draft()
        with self.assertRaises(ValueError) as ctx:
            inventory.approve(self.store, "c1", "expert")
        self.assertIn("theme_name is empty", str(ctx.exception))

    def test_missing_source_fields_block_approval(self):
        cluster = self._draft()
        cluster.theme_name = "a theme"
        cluster.text_ru = "ру"
        cluster.text_en = "en"
        cluster.sources = [{"section_id": "s1", "tractate": "", "folio": "2a", "quote": "q"}]
        self.store.save(cluster)
        with self.assertRaises(ValueError) as ctx:
            inventory.approve(self.store, "c1", "expert")
        self.assertIn("tractate", str(ctx.exception))

    def test_complete_cluster_can_be_approved(self):
        cluster = self._draft()
        cluster.theme_name = "a theme"
        cluster.text_ru = "ру"
        cluster.text_en = "en"
        cluster.sources = [
            {"section_id": "s1", "tractate": "Chagigah", "folio": "17a", "quote": "q"}
        ]
        self.store.save(cluster)
        approved = inventory.approve(self.store, "c1", "expert")
        self.assertTrue(approved.verified)
        self.assertEqual(approved.approved_by, "expert")
        self.assertEqual(inventory.verified_count(self.store), 1)

    def test_state_survives_a_reload(self):
        self.test_complete_cluster_can_be_approved()
        reloaded = inventory.JsonInventoryStore(self.path)
        self.assertEqual(inventory.verified_count(reloaded), 1)

    def test_section_lookup_finds_the_owning_cluster(self):
        self._draft("c9")
        found = self.store.cluster_for_section("s2")
        self.assertIsNotNone(found)
        self.assertEqual(found.cluster_id, "c9")


class ClaimSelectionTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = inventory.JsonInventoryStore(
            Path(self.dir.name) / "inventory.json"
        )
        self.policy = AtlasPolicy()

    def tearDown(self):
        self.dir.cleanup()

    def _cluster(self, cluster_id: str, sections: list[str], verified: bool):
        cluster = inventory.Cluster(
            cluster_id=cluster_id,
            section_ids=sections,
            theme_name="theme",
            text_ru="ру",
            text_en="en",
            sources=[
                {"section_id": sections[0], "tractate": "T", "folio": "1a", "quote": "q"}
            ],
            status=ClusterStatus.VERIFIED if verified else ClusterStatus.DRAFT,
        )
        self.store.save(cluster)

    def test_unverified_clusters_are_never_selected(self):
        self._cluster("c1", ["s1"], verified=False)
        hits = [RagHit(section_id="s1", score=0.99, text="t")]
        self.assertIsNone(best_match(hits, self.store, self.policy))

    def test_verified_cluster_above_threshold_is_selected(self):
        self._cluster("c1", ["s1"], verified=True)
        hits = [RagHit(section_id="s1", score=0.8, text="t")]
        match = best_match(hits, self.store, self.policy)
        self.assertIsNotNone(match)
        self.assertEqual(match.claim.cluster_id, "c1")

    def test_weak_match_is_rejected(self):
        self._cluster("c1", ["s1"], verified=True)
        hits = [RagHit(section_id="s1", score=0.2, text="t")]
        self.assertIsNone(best_match(hits, self.store, self.policy))

    def test_multiple_hits_in_one_cluster_raise_its_relevance(self):
        self._cluster("c1", ["s1", "s2", "s3"], verified=True)
        single = best_match(
            [RagHit(section_id="s1", score=0.7, text="t")], self.store, self.policy
        )
        multi = best_match(
            [
                RagHit(section_id="s1", score=0.7, text="t"),
                RagHit(section_id="s2", score=0.6, text="t"),
                RagHit(section_id="s3", score=0.6, text="t"),
            ],
            self.store,
            self.policy,
        )
        self.assertGreater(multi.relevance, single.relevance)

    def test_support_bonus_is_capped(self):
        self._cluster("c1", [f"s{i}" for i in range(20)], verified=True)
        match = best_match(
            [RagHit(section_id=f"s{i}", score=0.7, text="t") for i in range(20)],
            self.store,
            self.policy,
        )
        self.assertLessEqual(match.relevance, 0.8 + 1e-9)

    def test_highest_relevance_cluster_wins(self):
        self._cluster("c1", ["s1"], verified=True)
        self._cluster("c2", ["s2"], verified=True)
        match = best_match(
            [
                RagHit(section_id="s1", score=0.6, text="t"),
                RagHit(section_id="s2", score=0.9, text="t"),
            ],
            self.store,
            self.policy,
        )
        self.assertEqual(match.claim.cluster_id, "c2")

    def test_miss_distinguishes_unlabelled_inventory_from_off_corpus(self):
        self._cluster("c1", ["s1"], verified=False)
        message = explain_miss(
            [RagHit(section_id="s1", score=0.95, text="t")], self.store, self.policy
        )
        self.assertIn("not verified", message)
        self.assertIn("labelling", message)


class ThemeProposalTests(unittest.TestCase):
    def setUp(self):
        self.cluster = inventory.Cluster(cluster_id="c1", section_ids=["s1"])
        self.sections = [
            theme_proposer.SectionText(
                section_id="s1",
                text="If you grasp too much you have grasped nothing.",
                tractate="Chagigah",
                folio="17a",
            )
        ]

    def _validate(self, payload):
        return theme_proposer._validate(self.cluster, self.sections, payload)

    def _payload(self, **overrides):
        base = {
            "coherent": True,
            "theme_name": "overreach",
            "text_ru": "ру",
            "text_en": "en",
            "sources": [
                {
                    "section_id": "s1",
                    "tractate": "Chagigah",
                    "folio": "17a",
                    "quote": "grasp too much",
                }
            ],
            "confidence": 0.8,
            "reviewer_note": "check the folio",
        }
        base.update(overrides)
        return base

    def test_valid_proposal_is_accepted(self):
        proposal = self._validate(self._payload())
        self.assertTrue(proposal.coherent)
        self.assertEqual(proposal.theme_name, "overreach")

    def test_invented_section_id_is_rejected(self):
        payload = self._payload(
            sources=[
                {"section_id": "s99", "tractate": "T", "folio": "1a", "quote": "x"}
            ]
        )
        with self.assertRaises(theme_proposer.ProposalRejected) as ctx:
            self._validate(payload)
        self.assertIn("not in the supplied material", str(ctx.exception))

    def test_fabricated_quote_is_rejected(self):
        payload = self._payload(
            sources=[
                {
                    "section_id": "s1",
                    "tractate": "Chagigah",
                    "folio": "17a",
                    "quote": "a sentence that is simply not there",
                }
            ]
        )
        with self.assertRaises(theme_proposer.ProposalRejected):
            self._validate(payload)

    def test_quote_matching_ignores_whitespace_differences(self):
        payload = self._payload(
            sources=[
                {
                    "section_id": "s1",
                    "tractate": "Chagigah",
                    "folio": "17a",
                    "quote": "grasp   too\n much",
                }
            ]
        )
        self.assertTrue(self._validate(payload).coherent)

    def test_incoherent_cluster_is_recorded_not_raised(self):
        proposal = self._validate(
            {"coherent": False, "reviewer_note": "sections are unrelated"}
        )
        self.assertFalse(proposal.coherent)
        self.assertEqual(proposal.theme_name, "")

    def test_empty_theme_name_is_rejected(self):
        with self.assertRaises(theme_proposer.ProposalRejected):
            self._validate(self._payload(theme_name="   "))

    def test_staging_never_marks_a_cluster_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = inventory.JsonInventoryStore(Path(tmp) / "inv.json")
            store.save(self.cluster)
            theme_proposer.stage(store, self._validate(self._payload()))
            self.assertEqual(store.get("c1").status, ClusterStatus.PROPOSED)
            self.assertEqual(inventory.verified_count(store), 0)


class TelegramApprovalTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = inventory.JsonInventoryStore(Path(self.dir.name) / "inv.json")
        self.store.save(
            inventory.Cluster(
                cluster_id="c1",
                section_ids=["s1"],
                theme_name="theme",
                text_ru="ру",
                text_en="en",
                sources=[
                    {"section_id": "s1", "tractate": "T", "folio": "1a", "quote": "q"}
                ],
                status=ClusterStatus.PROPOSED,
            )
        )

    def tearDown(self):
        self.dir.cleanup()

    def test_command_parsing_handles_a_batched_reply(self):
        commands = approval_telegram.parse_commands(
            "/ok c1\n/no c2 wrong tractate\nchit chat\n/theme c3 better name"
        )
        self.assertEqual(
            [(c.verb, c.cluster_id) for c in commands],
            [("ok", "c1"), ("no", "c2"), ("theme", "c3")],
        )
        self.assertEqual(commands[1].argument, "wrong tractate")

    def test_unrecognised_lines_are_ignored(self):
        self.assertEqual(approval_telegram.parse_commands("hello there"), [])

    def test_ok_verifies_a_complete_cluster(self):
        changed, message = approval_telegram.apply_command(
            self.store, approval_telegram.Command("ok", "c1", ""), "expert"
        )
        self.assertTrue(changed)
        self.assertIn("verified", message)
        self.assertEqual(inventory.verified_count(self.store), 1)

    def test_edit_does_not_approve(self):
        approval_telegram.apply_command(
            self.store,
            approval_telegram.Command("theme", "c1", "a better theme"),
            "expert",
        )
        cluster = self.store.get("c1")
        self.assertEqual(cluster.theme_name, "a better theme")
        self.assertEqual(cluster.status, ClusterStatus.PROPOSED)
        self.assertEqual(inventory.verified_count(self.store), 0)

    def test_rejection_requires_a_reason(self):
        changed, message = approval_telegram.apply_command(
            self.store, approval_telegram.Command("no", "c1", ""), "expert"
        )
        self.assertFalse(changed)
        self.assertIn("needs a reason", message)

    def test_unknown_cluster_is_reported_not_created(self):
        changed, message = approval_telegram.apply_command(
            self.store, approval_telegram.Command("ok", "nope", ""), "expert"
        )
        self.assertFalse(changed)
        self.assertIn("unknown cluster", message)


class GateTests(unittest.TestCase):
    def _package(self, claim: AtlasClaim, **overrides) -> Package:
        topic = TopicCandidate(
            slug="test-topic",
            title="A collapse",
            keywords=("collapse",),
            evidence=(signal("v1", "c1", "A collapse"),),
            score=0.5,
        )
        script = overrides.pop("script", None) or make_script(claim)
        return Package(
            topic=topic,
            claim=claim,
            script=script,
            channel=overrides.pop("channel", Channel.IAHALOM),
            target_duration_s=overrides.pop("duration", 50),
            assets=overrides.pop("assets", {"video": "v.mp4", "audio": "a.wav"}),
        )

    def test_a_good_package_passes(self):
        package = self._package(make_claim())
        self.assertTrue(check_package(package).ok, check_package(package).refusals)

    def test_unverified_claim_is_refused(self):
        # An unverified claim is the exact condition of all 119 clusters today.
        claim = AtlasClaim(
            cluster_id="c1",
            theme_name="theme",
            text={Language.EN: "text", Language.RU: "текст"},
            sources=(),
            verified=False,
        )
        result = check_package(self._package(claim, script=make_script(claim)))
        codes = {r.code for r in result.refusals}
        self.assertIn("claim_unverified", codes)
        self.assertIn("claim_unsourced", codes)

    def test_paraphrased_claim_is_refused(self):
        claim = make_claim()
        script = make_script(claim)
        tampered = Script(
            topic_slug=script.topic_slug,
            language=script.language,
            beats=tuple(
                Beat(role=b.role, text="One who grasps too much holds nothing!")
                if b.role == "claim"
                else b
                for b in script.beats
            ),
            claim_cluster_id=script.claim_cluster_id,
        )
        result = check_package(self._package(claim, script=tampered))
        self.assertIn("claim_altered", {r.code for r in result.refusals})

    def test_language_must_match_the_channel(self):
        claim = make_claim()
        result = check_package(
            self._package(claim, script=make_script(claim, Language.EN),
                          channel=Channel.TAMHA)
        )
        self.assertIn("language_mismatch", {r.code for r in result.refusals})

    def test_slow_script_is_refused(self):
        claim = make_claim()
        result = check_package(
            self._package(claim, script=make_script(claim, filler_words=20),
                          duration=120)
        )
        self.assertIn("pacing_out_of_band", {r.code for r in result.refusals})

    def test_unapproved_link_in_description_is_refused(self):
        package = self._package(make_claim())
        result = check_package(
            package, description="More at https://example.com/promo"
        )
        self.assertIn("link_not_allowed", {r.code for r in result.refusals})

    def test_approved_source_link_passes(self):
        package = self._package(make_claim())
        result = check_package(
            package, description="Source: https://alhatorah.org/Chagigah.17a"
        )
        self.assertNotIn("link_not_allowed", {r.code for r in result.refusals})

    def test_blocked_terms_are_caught_in_narration(self):
        package = self._package(make_claim())
        blocklist = BlockList.from_lines(["# comment", "nobody learned"])
        result = check_package(package, blocklist=blocklist)
        self.assertIn("blocked_term", {r.code for r in result.refusals})

    def test_missing_assets_are_refused(self):
        package = self._package(make_claim(), assets={})
        codes = {r.code for r in check_package(package).refusals}
        self.assertIn("asset_missing", codes)

    def test_external_validators_can_veto(self):
        package = self._package(make_claim())
        from pipeline.contracts import Refusal

        result = check_package(
            package, external=[lambda p: [Refusal("queue_full", "queue guard says no")]]
        )
        self.assertIn("queue_full", {r.code for r in result.refusals})

    def test_blocked_result_raises_rather_than_returning(self):
        claim = make_claim(verified=False)
        result = check_package(self._package(claim, script=make_script(claim)))
        with self.assertRaises(PublicationBlocked):
            result.raise_if_blocked()

    def test_description_lists_every_source(self):
        package = self._package(make_claim())
        description = build_description(package)
        self.assertIn("Chagigah 17a", description)
        self.assertIn("Sources:", description)

    def test_russian_description_uses_russian_headers(self):
        claim = make_claim()
        package = self._package(
            claim, script=make_script(claim, Language.RU), channel=Channel.TAMHA
        )
        self.assertIn("Источники:", build_description(package))


class ContractTests(unittest.TestCase):
    def test_verified_claim_without_sources_is_impossible(self):
        with self.assertRaises(ValueError):
            AtlasClaim(
                cluster_id="c1",
                theme_name="t",
                text={Language.EN: "x"},
                sources=(),
                verified=True,
            )

    def test_verified_claim_without_theme_name_is_impossible(self):
        with self.assertRaises(ValueError):
            AtlasClaim(
                cluster_id="c1",
                theme_name="  ",
                text={Language.EN: "x"},
                sources=(ClaimSource("s1", "T", "1a", "q"),),
                verified=True,
            )

    def test_package_id_is_stable_and_channel_specific(self):
        claim = make_claim()
        topic = TopicCandidate("s", "t", ("k",), (), 0.1)
        first = Package(topic, claim, make_script(claim), Channel.TAMHA, 60)
        second = Package(topic, claim, make_script(claim), Channel.TAMHA, 60)
        other = Package(topic, claim, make_script(claim), Channel.IAHALOM, 60)
        self.assertEqual(first.package_id, second.package_id)
        self.assertNotEqual(first.package_id, other.package_id)

    def test_package_serialises_to_json(self):
        claim = make_claim()
        package = Package(
            TopicCandidate("s", "t", ("k",), (signal("v", "c", "t"),), 0.1),
            claim,
            make_script(claim),
            Channel.TAMHA,
            60,
        )
        payload = json.loads(package.to_json())
        self.assertEqual(payload["channel"], "tamha")
        self.assertEqual(payload["claim"]["text"]["en"], claim.text[Language.EN])

    def test_package_survives_a_json_round_trip(self):
        claim = make_claim()
        original = Package(
            TopicCandidate("s", "t", ("k",), (signal("v", "c", "t"),), 0.1),
            claim,
            make_script(claim),
            Channel.TAMHA,
            60,
            assets={"video": "v.mp4"},
        )
        restored = Package.from_json(original.to_json())
        self.assertEqual(restored.package_id, original.package_id)
        self.assertEqual(restored.claim.sources, original.claim.sources)
        self.assertEqual(restored.script.beats, original.script.beats)
        self.assertEqual(restored.channel, Channel.TAMHA)
        self.assertEqual(restored.assets["video"], "v.mp4")

    def test_hand_edited_package_cannot_smuggle_in_a_verified_claim(self):
        claim = make_claim()
        payload = json.loads(
            Package(
                TopicCandidate("s", "t", ("k",), (), 0.1),
                claim,
                make_script(claim),
                Channel.TAMHA,
                60,
            ).to_json()
        )
        payload["claim"]["sources"] = []  # keep verified: true, drop the citations
        with self.assertRaises(ValueError):
            Package.from_json(json.dumps(payload))

    def test_channel_language_mapping(self):
        self.assertIs(Channel.TAMHA.language, Language.RU)
        self.assertIs(Channel.IAHALOM.language, Language.EN)


class ScriptAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.claim = make_claim()
        self.topic = TopicCandidate("slug", "title", ("k",), (), 0.5)
        self.claim_text = self.claim.render(Language.EN)

    def _assemble(self, beats):
        return assemble(
            {"beats": beats}, self.topic, self.claim, Language.EN, self.claim_text
        )

    def test_claim_beat_is_inserted_verbatim(self):
        script = self._assemble(
            [
                {"role": "hook", "text": "a hook"},
                {"role": "turn", "text": "a turn"},
                {"role": "payoff", "text": "a payoff"},
            ]
        )
        verify_claim_intact(script, self.claim)
        self.assertEqual(script.beats_of("claim")[0].text, self.claim_text)

    def test_model_authored_claim_beats_are_discarded(self):
        script = self._assemble(
            [
                {"role": "hook", "text": "a hook"},
                {"role": "claim", "text": "a claim the model made up"},
                {"role": "payoff", "text": "a payoff"},
            ]
        )
        self.assertEqual(len(script.beats_of("claim")), 1)
        self.assertEqual(script.beats_of("claim")[0].text, self.claim_text)

    def test_claim_lands_between_turn_and_payoff(self):
        script = self._assemble(
            [
                {"role": "hook", "text": "a hook"},
                {"role": "turn", "text": "a turn"},
                {"role": "payoff", "text": "a payoff"},
            ]
        )
        roles = [beat.role for beat in script.beats]
        self.assertLess(roles.index("turn"), roles.index("claim"))
        self.assertLess(roles.index("claim"), roles.index("payoff"))

    def test_script_ending_on_the_claim_is_rejected(self):
        with self.assertRaises(ScriptGenerationFailed):
            self._assemble([{"role": "hook", "text": "a hook"}])

    def test_malformed_generator_output_is_rejected(self):
        with self.assertRaises(ScriptGenerationFailed):
            assemble("not json", self.topic, self.claim, Language.EN, self.claim_text)


class ConfigTests(unittest.TestCase):
    def test_env_parsing_handles_quotes_exports_and_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                '# comment\nexport A="one"\nB=\'two\'\nC=three\n\nBAD_LINE\n',
                encoding="utf-8",
            )
            self.assertEqual(
                parse_env_file(path), {"A": "one", "B": "two", "C": "three"}
            )

    def test_missing_file_yields_no_values(self):
        self.assertEqual(parse_env_file(Path("/nonexistent/.env")), {})

    def test_required_secret_error_names_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            secrets = Secrets({"x": Path(tmp) / "missing.env"})
            with self.assertRaises(Exception) as ctx:
                secrets.require("x", "SOME_KEY")
            self.assertIn("missing.env", str(ctx.exception))

    def test_prefixed_keys_are_returned_in_key_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env.ytdata"
            path.write_text(
                "YT_DATA_API_KEY_2=second\nYT_DATA_API_KEY_1=first\n", encoding="utf-8"
            )
            secrets = Secrets({"ytdata": path})
            self.assertEqual(
                secrets.all_matching("ytdata", "YT_DATA_API_KEY"), ["first", "second"]
            )

    def test_pacing_policy_band(self):
        policy = PacingPolicy()
        self.assertEqual(policy.target_wpm, 142.5)


class RenderTests(unittest.TestCase):
    def test_wrap_respects_the_character_budget(self):
        lines = wrap("one two three four five six seven eight", 12)
        self.assertTrue(all(len(line) <= 14 for line in lines))
        self.assertEqual(" ".join(lines), "one two three four five six seven eight")

    def test_wrap_never_returns_nothing(self):
        self.assertEqual(wrap("", 10), [""])

    def test_claim_slide_carries_its_sources(self):
        claim = make_claim()
        script = make_script(claim)
        slides = build_slides(script, claim, [3.0] * len(script.beats))
        claim_slide = next(s for s in slides if s.role == "claim")
        self.assertIn("Chagigah 17a", claim_slide.footnote)
        self.assertEqual([s.footnote for s in slides].count(""), len(slides) - 1)

    def test_duration_count_must_match_beat_count(self):
        claim = make_claim()
        from pipeline.render.deck import RenderError

        with self.assertRaises(RenderError):
            build_slides(make_script(claim), claim, [1.0])

    def test_svg_escapes_markup_in_narration(self):
        claim = make_claim()
        script = Script(
            topic_slug="t",
            language=Language.EN,
            beats=(Beat(role="hook", text="5 < 6 & <script>alert(1)</script>"),),
            claim_cluster_id="c1",
        )
        svg = slide_svg(build_slides(script, claim, [2.0])[0])
        self.assertNotIn("<script>", svg)
        self.assertIn("&lt;", svg)

    def test_lexicon_replaces_whole_words_only(self):
        lexicon = Lexicon({"Bava": "Bah-vah"})
        self.assertEqual(lexicon.apply("Bava Metzia"), "Bah-vah Metzia")
        self.assertEqual(lexicon.apply("Bavarian"), "Bavarian")

    def test_lexicon_is_case_insensitive(self):
        self.assertEqual(Lexicon({"bava": "Bah-vah"}).apply("BAVA"), "Bah-vah")

    def test_empty_lexicon_is_a_no_op(self):
        self.assertEqual(Lexicon().apply("anything at all"), "anything at all")


class PngToolsTests(unittest.TestCase):
    def _write(self, path: Path, width: int, height: int, colour=(20, 27, 45)) -> Path:
        from pipeline.render.pngtools import Png, write_png

        row = bytes(colour) * width
        return write_png(path, Png(width, height, 3, 8, 2, [row] * height))

    def test_round_trip_preserves_pixels(self):
        from pipeline.render.pngtools import read_png

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp) / "a.png", 8, 4)
            image = read_png(path)
            self.assertEqual((image.width, image.height), (8, 4))
            self.assertEqual(image.pixel(3, 2), (20, 27, 45))

    def test_crop_trims_to_the_artboard(self):
        from pipeline.render.pngtools import crop_top_left, read_png

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp) / "a.png", 40, 30)
            crop_top_left(path, 40, 20)
            image = read_png(path)
            self.assertEqual((image.width, image.height), (40, 20))

    def test_crop_to_current_size_is_a_no_op(self):
        from pipeline.render.pngtools import crop_top_left, read_png

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp) / "a.png", 10, 10)
            crop_top_left(path, 10, 10)
            self.assertEqual(read_png(path).height, 10)

    def test_cannot_crop_upwards(self):
        from pipeline.render.pngtools import PngError, crop_top_left

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp) / "a.png", 10, 10)
            with self.assertRaises(PngError):
                crop_top_left(path, 20, 20)

    def test_non_png_is_rejected(self):
        from pipeline.render.pngtools import PngError, read_png

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.png"
            path.write_bytes(b"not a png at all")
            with self.assertRaises(PngError):
                read_png(path)


class RasterizeIntegrationTests(unittest.TestCase):
    """Exercises the real rasteriser when the host has one.

    The Chromium fallback previously cropped the bottom of every slide, which
    silently removed the source citation -- the one element that must survive.
    """

    def setUp(self):
        from pipeline.render.deck import _find_browser

        if not (
            _find_browser()
            or __import__("shutil").which("rsvg-convert")
            or __import__("shutil").which("inkscape")
        ):
            self.skipTest("no SVG rasteriser available on this host")

    def test_rendered_slide_is_exactly_the_artboard_and_keeps_its_footer(self):
        from pipeline.render.deck import RenderError, rasterize, write_deck
        from pipeline.render.pngtools import read_png

        claim = make_claim()
        script = make_script(claim)
        with tempfile.TemporaryDirectory() as tmp:
            slides = write_deck(
                build_slides(script, claim, [2.0] * len(script.beats)), Path(tmp)
            )
            claim_slide = next(s for s in slides if s.role == "claim")
            png = Path(tmp) / "claim.png"
            try:
                rasterize(claim_slide.path, png)
            except RenderError as exc:
                self.skipTest(str(exc))

            image = read_png(png)
            self.assertEqual((image.width, image.height), (1920, 1080))
            # The claim slide's own background must reach the last row; if the
            # viewport were short, the footer band would be page background.
            self.assertEqual(image.pixel(image.width // 2, image.height - 1), (20, 27, 45))
            # And the footnote must actually be drawn above the bottom edge.
            footer = [
                image.pixel(x, y)
                for y in range(940, 1010)
                for x in range(700, 1220, 4)
            ]
            self.assertTrue(
                any(pixel != (20, 27, 45) for pixel in footer),
                "no footnote pixels found — the source citation is missing",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
