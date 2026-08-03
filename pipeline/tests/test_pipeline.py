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
from pipeline.scout import channel_registry, registry, trend_scout, youtube_analytics
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


class RegistryTests(unittest.TestCase):
    def test_raw_channel_id_needs_no_resolution(self):
        entry = registry.parse_entry("UC" + "a" * 22)
        self.assertTrue(entry.resolved)
        self.assertEqual(entry.channel_id, "UC" + "a" * 22)

    def test_bare_handle_is_recognised(self):
        entry = registry.parse_entry("@tamha4")
        self.assertEqual(entry.handle, "@tamha4")
        self.assertFalse(entry.resolved)

    def test_handle_url_is_recognised(self):
        entry = registry.parse_entry("https://www.youtube.com/@Tamha2")
        self.assertEqual(entry.handle, "@Tamha2")

    def test_channel_url_yields_the_id_without_an_api_call(self):
        channel_id = "UC" + "b" * 22
        entries = registry.parse_registry(
            f"https://www.youtube.com/channel/{channel_id}"
        )
        self.assertEqual(entries[0].channel_id, channel_id)
        self.assertTrue(entries[0].resolved)

    def test_commented_out_channel_url_is_not_watched(self):
        # The shipped registry documents its own accepted URL forms in
        # comments; those examples must never become watched channels.
        entries = registry.parse_registry(
            "#     https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx\n"
            "UC" + "a" * 22
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].channel_id, "UC" + "a" * 22)

    def test_shipped_example_registry_parses_to_real_channels_only(self):
        entries = registry.load(Path("pipeline/data/channels.example.txt"))
        self.assertTrue(entries)
        for entry in entries:
            self.assertNotIn("x" * 10, entry.channel_id)
        self.assertEqual(sum(1 for e in entries if e.resolved), 7)
        self.assertEqual(sum(1 for e in entries if not e.resolved), 2)

    def test_our_own_channels_are_not_in_the_watch_list(self):
        # Watching our own channels would rank our back catalogue as trends.
        text = Path("pipeline/data/channels.example.txt").read_text(encoding="utf-8")
        entries = registry.parse_registry(text)
        handles = {e.handle.casefold() for e in entries if e.handle}
        self.assertNotIn("@tamha4", handles)
        self.assertNotIn("@tamha2", handles)

    def test_comments_and_blanks_are_skipped(self):
        entries = registry.parse_registry("# heading\n\n@one\n@two  # trailing note")
        self.assertEqual([e.handle for e in entries], ["@one", "@two"])
        self.assertEqual(entries[1].note, "trailing note")

    def test_unparseable_line_is_rejected_loudly(self):
        with self.assertRaises(registry.RegistryError):
            registry.parse_entry("just some prose")

    def test_resolution_uses_cache_and_dedupes(self):
        class FakeClient:
            def __init__(self):
                self.calls = 0

            def channel_id_for_handle(self, handle):
                self.calls += 1
                return "UC" + handle.lstrip("@").ljust(22, "x")[:22]

        with tempfile.TemporaryDirectory() as tmp:
            cache = registry.HandleCache(Path(tmp) / "cache.json")
            client = FakeClient()
            entries = registry.parse_registry("@alpha\n@alpha\n@beta")
            ids, problems = registry.resolve(entries, client, cache)
            self.assertEqual(len(ids), 2, ids)
            self.assertEqual(problems, [])
            # Second run reads the cache instead of the API.
            before = client.calls
            registry.resolve(entries, client, registry.HandleCache(cache.path))
            self.assertEqual(client.calls, before)

    def test_one_dead_channel_does_not_stop_the_others(self):
        class FlakyClient:
            def channel_id_for_handle(self, handle):
                if handle == "@dead":
                    raise RuntimeError("no channel found")
                return "UC" + handle.lstrip("@").ljust(22, "x")[:22]

        entries = registry.parse_registry("@dead\n@alive")
        ids, problems = registry.resolve(entries, FlakyClient(), None)
        self.assertEqual(len(ids), 1)
        self.assertEqual(len(problems), 1)
        self.assertIn("@dead", problems[0])


class ChannelRegistryTests(unittest.TestCase):
    """The deployed registries could not be inspected, so these fix the
    contract the reader assumes and prove the alias fallbacks work."""

    def _channel(self, channel_id: str, status: str = "WATCH_CORE", **overrides):
        record = {
            "channel_id": channel_id,
            "title": f"Channel {channel_id}",
            "subscribers": 100_000,
            "median_top3": 50_000,
            "status": status,
            "passes_filter": True,
            "checked_at": "2026-08-03T06:00:00Z",
            "last_videos": [
                {
                    "video_id": f"{channel_id}-v1",
                    "title": "New AI model breaks benchmark records",
                    "views": 400_000,
                    "published_at": "2026-08-02T10:00:00Z",
                    "duration_s": 700,
                },
                {
                    "video_id": f"{channel_id}-v2",
                    "title": "Quiet week in research",
                    "views": 20_000,
                    "published_at": "2026-08-01T10:00:00Z",
                    "duration_s": 500,
                },
            ],
        }
        record.update(overrides)
        return record

    def _write(self, tmp: Path, name: str, channels: list) -> Path:
        path = tmp / name
        path.write_text(json.dumps({"channels": channels}), encoding="utf-8")
        return path

    def test_reads_a_registry_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(Path(tmp), "ai_science_en.json", [self._channel("UC1")])
            records = channel_registry.load_dir(Path(tmp))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].subscribers, 100_000)
            self.assertEqual(records[0].baseline_views, 50_000)

    def test_channel_in_two_profiles_is_merged_not_doubled(self):
        # 27 of the 620 channels sit in more than one profile. Counting one
        # twice would fake cross-channel agreement for a topic.
        with tempfile.TemporaryDirectory() as tmp:
            self._write(Path(tmp), "ai_science_en.json", [self._channel("UC1")])
            self._write(Path(tmp), "academic_science_en.json", [self._channel("UC1")])
            records = channel_registry.load_dir(Path(tmp))
            self.assertEqual(len(records), 1)
            self.assertEqual(len(records[0].profiles), 2)

    def test_excluded_channels_are_never_scouted(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(
                Path(tmp),
                "ai_science_en.json",
                [
                    self._channel("UC1", "WATCH_CORE"),
                    self._channel("UC2", "EXCLUDE_TOPIC"),
                    self._channel("UC3", "EXCLUDE_QUALITY"),
                ],
            )
            records = channel_registry.load_dir(Path(tmp))
            scoutable = channel_registry.scoutable(records)
            self.assertEqual({r.channel_id for r in scoutable}, {"UC1"})

    def test_rights_check_is_watchable_but_not_a_source(self):
        status = channel_registry.EditorialStatus.RIGHTS_CHECK
        self.assertFalse(status.excluded)
        self.assertFalse(status.usable_as_source)
        self.assertTrue(channel_registry.EditorialStatus.WATCH_CORE.usable_as_source)

    def test_channels_below_the_numeric_filter_are_still_watched(self):
        # The filter says who is worth imitating, not who is worth watching:
        # a quiet channel that suddenly spikes is the signal worth having.
        with tempfile.TemporaryDirectory() as tmp:
            self._write(
                Path(tmp),
                "ai_science_en.json",
                [self._channel("UC1", passes_filter=False)],
            )
            records = channel_registry.load_dir(Path(tmp))
            self.assertEqual(len(channel_registry.scoutable(records)), 1)

    def test_signals_use_the_registry_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(Path(tmp), "ai_science_en.json", [self._channel("UC1")])
            signals = channel_registry.signals_from(
                channel_registry.load_dir(Path(tmp))
            )
            self.assertEqual(len(signals), 2)
            self.assertEqual(signals[0].channel_median_views, 50_000)
            self.assertAlmostEqual(signals[0].outlier_ratio, 8.0)

    def test_alias_fallback_handles_different_field_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            alt = {
                "id": "UC9",
                "channel_name": "Alt shape",
                "subscriberCount": 5000,
                "recent_videos": [
                    {
                        "videoId": "v9",
                        "name": "Quantum result replicated",
                        "viewCount": 9000,
                        "publishedAt": "2026-08-02T10:00:00Z",
                    }
                ],
                "editorial_status": "WATCH_WEEKLY",
            }
            (Path(tmp) / "ai_science_ru.json").write_text(
                json.dumps(alt and [alt]), encoding="utf-8"
            )
            records = channel_registry.load_dir(Path(tmp))
            self.assertEqual(records[0].channel_id, "UC9")
            self.assertEqual(records[0].subscribers, 5000)
            self.assertEqual(len(records[0].to_signals()), 1)

    def test_registry_keyed_by_channel_id_is_understood(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {"UC7": {"title": "Keyed", "subscribers": 10, "status": "REVIEW"}}
            (Path(tmp) / "ai_science_en.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            records = channel_registry.load_dir(Path(tmp))
            self.assertEqual(records[0].channel_id, "UC7")

    def test_unknown_status_does_not_crash_or_silently_exclude(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(
                Path(tmp), "ai_science_en.json", [self._channel("UC1", "SOMETHING_NEW")]
            )
            records = channel_registry.load_dir(Path(tmp))
            self.assertEqual(
                records[0].status, channel_registry.EditorialStatus.UNCLASSIFIED
            )
            self.assertEqual(len(channel_registry.scoutable(records)), 1)

    def test_unparseable_shape_names_the_keys_it_saw(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "ai_science_en.json").write_text(
                json.dumps({"unexpected_wrapper": 42}), encoding="utf-8"
            )
            with self.assertRaises(channel_registry.RegistryError) as ctx:
                channel_registry.load_dir(Path(tmp))
            self.assertIn("unexpected_wrapper", str(ctx.exception))

    def test_doctor_warns_when_aliases_match_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(
                Path(tmp),
                "ai_science_en.json",
                [{"channel_id": "UC1", "totally_unknown_field": 1}],
            )
            report = channel_registry.describe_registry(
                channel_registry.load_dir(Path(tmp))
            )
            self.assertIn("WARNING", report)
            self.assertIn("subscribers", report)

    def test_doctor_reports_status_breakdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(
                Path(tmp),
                "ai_science_en.json",
                [self._channel("UC1", "WATCH_CORE"), self._channel("UC2", "REVIEW")],
            )
            report = channel_registry.describe_registry(
                channel_registry.load_dir(Path(tmp))
            )
            self.assertIn("WATCH_CORE", report)
            self.assertIn("REVIEW", report)
            self.assertNotIn("WARNING", report)

    def test_missing_directory_is_a_clear_error(self):
        with self.assertRaises(channel_registry.RegistryError):
            channel_registry.load_dir(Path("/nonexistent/registries"))


class DomainProfileTests(unittest.TestCase):
    def test_deployed_registry_domains_have_vocabularies(self):
        for domain in set(trend_scout.REGISTRY_DOMAINS.values()):
            self.assertIn(domain, trend_scout.DOMAIN_PROFILES)
            self.assertGreater(len(trend_scout.DOMAIN_PROFILES[domain]), 10)

    def test_each_domain_covers_both_languages(self):
        for domain, terms in trend_scout.DOMAIN_PROFILES.items():
            has_cyrillic = any(any("а" <= c <= "я" for c in t) for t in terms)
            has_latin = any(any("a" <= c <= "z" for c in t) for t in terms)
            self.assertTrue(has_cyrillic, f"{domain} has no RU terms")
            self.assertTrue(has_latin, f"{domain} has no EN terms")

    def test_domain_is_derived_per_channel_from_its_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = {
                "subscribers": 100_000,
                "median_top3": 10_000,
                "status": "WATCH_CORE",
                "last_videos": [],
            }
            (Path(tmp) / "ai_science_en.json").write_text(
                json.dumps([{**base, "channel_id": "UC_AI"}]), encoding="utf-8"
            )
            (Path(tmp) / "academic_science_ru.json").write_text(
                json.dumps([{**base, "channel_id": "UC_SCI"}]), encoding="utf-8"
            )
            mapping = channel_registry.domain_map(
                channel_registry.load_dir(Path(tmp))
            )
            self.assertEqual(mapping["UC_AI"], "ai_science")
            self.assertEqual(mapping["UC_SCI"], "academic_science")

    def test_per_channel_domain_beats_a_wrong_global_default(self):
        ai = signal("v1", "UC_AI", "New LLM breaks every benchmark")
        ai2 = signal("v2", "UC_AI2", "LLM benchmark results replicated")
        policy = ScoutPolicy(domain="corporate_collapse", min_distinct_channels=2)

        without = trend_scout.rank_topics([ai, ai2], policy)
        with_map = trend_scout.rank_topics(
            [ai, ai2], policy, domains={"UC_AI": "ai_science", "UC_AI2": "ai_science"}
        )
        self.assertTrue(without and with_map)
        self.assertGreater(with_map[0].score, without[0].score)

    def test_unmapped_channel_falls_back_to_policy_domain(self):
        signals = [
            signal("v1", "UC_X", "Enron collapse explained"),
            signal("v2", "UC_Y", "The Enron collapse timeline"),
        ]
        topics = trend_scout.rank_topics(
            signals, ScoutPolicy(domain="corporate_collapse"), domains={}
        )
        self.assertTrue(topics)

    def test_wrong_domain_flattens_scores_rather_than_erroring(self):
        # Choosing the wrong domain is silent, not fatal -- worth a test so the
        # symptom (everything at the 0.25 floor) is documented somewhere.
        ai_title = "New LLM breaks every benchmark"
        right = trend_scout.domain_affinity(ai_title, "ai_science")
        wrong = trend_scout.domain_affinity(ai_title, "corporate_collapse")
        self.assertGreater(right, wrong)
        self.assertAlmostEqual(wrong, 0.25)


class AnalyticsTests(unittest.TestCase):
    PAYLOAD = {
        "columnHeaders": [
            {"name": "day", "columnType": "DIMENSION"},
            {"name": "views", "columnType": "METRIC"},
            {"name": "averageViewPercentage", "columnType": "METRIC"},
        ],
        "rows": [["2026-01-01", 100, 20.0], ["2026-01-02", 900, 40.0]],
    }

    def test_report_rows_are_named(self):
        rows = youtube_analytics.parse_report(self.PAYLOAD)
        self.assertEqual(rows[0].dimensions, ("2026-01-01",))
        self.assertEqual(rows[1].metrics["views"], 900.0)

    def test_average_is_weighted_by_views(self):
        summary = youtube_analytics.retention_summary(
            youtube_analytics.parse_report(self.PAYLOAD)
        )
        # An unweighted mean would be 30.0; weighting by views gives 38.
        self.assertAlmostEqual(summary["averageViewPercentage"], 38.0)
        self.assertEqual(summary["views"], 1000.0)

    def test_empty_report_is_not_an_error(self):
        self.assertEqual(youtube_analytics.retention_summary([]), {})

    def test_zero_views_does_not_divide_by_zero(self):
        rows = youtube_analytics.parse_report(
            {
                "columnHeaders": self.PAYLOAD["columnHeaders"],
                "rows": [["2026-01-01", 0, 0.0]],
            }
        )
        self.assertEqual(youtube_analytics.retention_summary(rows)["averageViewPercentage"], 0.0)

    def test_analytics_window_ends_before_today(self):
        from datetime import date

        start, end = youtube_analytics.recent_window(28)
        self.assertLess(end, date.today())
        self.assertEqual((end - start).days, 28)


class ShippedLexiconTests(unittest.TestCase):
    def test_both_languages_ship_a_lexicon(self):
        for language in (Language.RU, Language.EN):
            self.assertGreater(len(Lexicon.for_language(language)), 40)

    def test_comment_keys_are_not_substitution_rules(self):
        self.assertEqual(Lexicon({"_comment": "x", "a": "b"}).apply("_comment"), "_comment")

    def test_russian_lexicon_renders_tractates_in_cyrillic(self):
        rendered = Lexicon.for_language(Language.RU).apply("Chagigah")
        self.assertTrue(
            all(char.isalpha() is False or "Ѐ" <= char <= "ӿ" or char in "́"
                for char in rendered),
            f"expected Cyrillic, got {rendered!r}",
        )

    def test_multiword_terms_are_replaced(self):
        self.assertNotIn(
            "Metzia", Lexicon.for_language(Language.EN).apply("Bava Metzia")
        )

    def test_both_lexicons_cover_the_same_tractates(self):
        import json as _json
        from pathlib import Path as _Path

        base = _Path("pipeline/data")
        ru = _json.loads((base / "lexicon_ru.json").read_text(encoding="utf-8"))
        en = _json.loads((base / "lexicon_en.json").read_text(encoding="utf-8"))
        ru_keys = {k for k in ru if not k.startswith("_")}
        en_keys = {k for k in en if not k.startswith("_")}
        # RU carries one extra Cyrillic alias for the channel name.
        self.assertEqual(en_keys - ru_keys, set())


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


class SectionSourceTests(unittest.TestCase):
    """Retrieval must be by id.

    The original code used the section ids as a semantic search *query*, so
    unrelated passages came back, the id filter dropped them, and every cluster
    was skipped with "no section texts" — a silent no-op that looked like a
    successful run.
    """

    def setUp(self):
        from pipeline.atlas import sections

        self.sections = sections

    def test_inline_source_returns_the_requested_sections(self):
        source = self.sections.InlineSectionSource.from_records(
            [
                {"section_id": "s1", "text": "first", "tractate": "T", "folio": "1a"},
                {"section_id": "s2", "text": "second"},
            ]
        )
        found = source.fetch(["s1", "s2"])
        self.assertEqual([s.section_id for s in found], ["s1", "s2"])
        self.assertEqual(found[0].tractate, "T")

    def test_inline_source_raises_rather_than_returning_empty(self):
        source = self.sections.InlineSectionSource.from_records(
            [{"section_id": "s1", "text": "first"}]
        )
        with self.assertRaises(self.sections.SectionsUnavailable):
            source.fetch(["nope"])

    def test_rag_source_refuses_when_nothing_matches_the_ids(self):
        class WrongClient:
            def fetch_by_ids(self, ids, collection=None):
                # What semantic search would return: plausible, but not ours.
                return [RagHit(section_id="other", score=0.9, text="unrelated")]

        source = self.sections.RagSectionSource(WrongClient())
        with self.assertRaises(self.sections.SectionsUnavailable) as ctx:
            source.fetch(["s1"])
        self.assertIn("none matched", str(ctx.exception))
        self.assertIn("do not fall back", str(ctx.exception))

    def test_rag_source_returns_matching_sections(self):
        class GoodClient:
            def fetch_by_ids(self, ids, collection=None):
                return [RagHit(section_id=i, score=1.0, text=f"text {i}") for i in ids]

        found = self.sections.RagSectionSource(GoodClient()).fetch(["s1", "s2"])
        self.assertEqual([s.section_id for s in found], ["s1", "s2"])

    def test_empty_id_list_is_an_error_not_an_empty_result(self):
        class AnyClient:
            def fetch_by_ids(self, ids, collection=None):
                return []

        with self.assertRaises(self.sections.SectionsUnavailable):
            self.sections.RagSectionSource(AnyClient()).fetch([])

    def test_resolve_prefers_inline_and_falls_back_to_rag(self):
        class GoodClient:
            def fetch_by_ids(self, ids, collection=None):
                return [RagHit(section_id=i, score=1.0, text="from rag") for i in ids]

        inline = self.sections.InlineSectionSource.from_records(
            [{"section_id": "s1", "text": "from inline"}]
        )
        rag = self.sections.RagSectionSource(GoodClient())

        texts, origin = self.sections.resolve(["s1"], inline, rag)
        self.assertEqual(origin, "inline")
        self.assertEqual(texts[0].text, "from inline")

        texts, origin = self.sections.resolve(["s9"], inline, rag)
        self.assertEqual(origin, "rag")

    def test_resolve_reports_every_source_it_tried(self):
        with self.assertRaises(self.sections.SectionsUnavailable) as ctx:
            self.sections.resolve(["s1"], None, None)
        self.assertIn("no section source", str(ctx.exception))


class InventoryImportTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = inventory.JsonInventoryStore(Path(self.dir.name) / "inv.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_imports_a_list_of_clusters(self):
        count, problems = inventory.import_clusters(
            self.store,
            [{"cluster_id": "c1", "section_ids": ["s1", "s2"]},
             {"cluster_id": "c2", "section_ids": ["s3"]}],
        )
        self.assertEqual(count, 2)
        self.assertEqual(problems, [])
        self.assertEqual(len(self.store.get("c1").section_ids), 2)

    def test_import_never_marks_anything_verified(self):
        # Even if the export claims approval, a human must still pass it
        # through approve(), which re-checks completeness.
        inventory.import_clusters(
            self.store,
            [{"cluster_id": "c1", "section_ids": ["s1"], "status": "verified",
              "theme_name": "claimed", "text_ru": "р", "text_en": "e"}],
        )
        self.assertEqual(self.store.get("c1").status, ClusterStatus.DRAFT)
        self.assertEqual(inventory.verified_count(self.store), 0)

    def test_alias_field_names_are_understood(self):
        count, _ = inventory.import_clusters(
            self.store, {"clusters": [{"id": "c9", "members": ["s1"], "theme": "x"}]}
        )
        self.assertEqual(count, 1)
        self.assertEqual(self.store.get("c9").theme_name, "x")

    def test_object_keyed_by_cluster_id_is_understood(self):
        count, _ = inventory.import_clusters(
            self.store, {"c7": {"section_ids": ["s1"]}}
        )
        self.assertEqual(count, 1)
        self.assertIsNotNone(self.store.get("c7"))

    def test_records_without_an_id_are_reported_not_silently_dropped(self):
        count, problems = inventory.import_clusters(
            self.store, [{"section_ids": ["s1"]}]
        )
        self.assertEqual(count, 0)
        self.assertEqual(len(problems), 1)

    def test_missing_section_ids_are_reported(self):
        _, problems = inventory.import_clusters(self.store, [{"cluster_id": "c1"}])
        self.assertTrue(any("no section ids" in p for p in problems))

    def test_reimport_does_not_undo_an_approval(self):
        cluster = inventory.Cluster(
            cluster_id="c1", section_ids=["s1"], theme_name="t",
            text_ru="р", text_en="e",
            sources=[{"section_id": "s1", "tractate": "T", "folio": "1a", "quote": "q"}],
        )
        self.store.save(cluster)
        inventory.approve(self.store, "c1", "expert")

        inventory.import_clusters(
            self.store, [{"cluster_id": "c1", "section_ids": ["s1", "s2"]}]
        )
        refreshed = self.store.get("c1")
        self.assertTrue(refreshed.verified, "an import must not undo human approval")
        self.assertEqual(refreshed.section_ids, ["s1", "s2"])

    def test_unsupported_payload_type_raises(self):
        with self.assertRaises(ValueError):
            inventory.import_clusters(self.store, "not a cluster export")
