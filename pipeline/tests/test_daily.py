"""Tests for the daily digest layer. Offline: no network, no registries."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from pipeline.config import DailyPolicy
from pipeline.daily import report as daily_report
from pipeline.daily import snapshot as snap
from pipeline.scout import channel_registry

NOW = datetime.now(timezone.utc)


def video(video_id: str, title: str, views: int, days_ago: int = 0) -> dict:
    return {
        "video_id": video_id,
        "title": title,
        "views": views,
        "published_at": (NOW - timedelta(days=days_ago)).isoformat(),
        "duration_s": 700,
    }


def channel(
    channel_id: str,
    videos: list[dict],
    status: str = "WATCH_CORE",
    subscribers: int = 100_000,
    baseline: int = 50_000,
    passes: bool = True,
    title: str = "",
) -> dict:
    return {
        "channel_id": channel_id,
        "title": title or f"Channel {channel_id}",
        "status": status,
        "subscribers": subscribers,
        "median_top3": baseline,
        "passes_filter": passes,
        "last_videos": videos,
        "checked_at": NOW.isoformat(),
    }


def registry_of(tmp: Path, channels: list[dict], name: str = "ai_science_en.json"):
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / name).write_text(json.dumps({"channels": channels}), encoding="utf-8")
    return channel_registry.load_dir(tmp)


class SnapshotTests(unittest.TestCase):
    def test_round_trip_preserves_channels_and_videos(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = registry_of(Path(tmp) / "reg", [channel("UC1", [video("v1", "T", 10)])])
            original = snap.Snapshot.from_records(records)
            restored = snap.Snapshot.from_json(original.to_json())
            self.assertEqual(restored.channels["UC1"].video_ids, {"v1"})
            self.assertEqual(restored.channels["UC1"].baseline_views, 50_000)

    def test_previous_returns_none_on_the_first_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(snap.previous(Path(tmp), before=date.today()))

    def test_previous_finds_the_most_recent_earlier_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for offset in (1, 3, 9):
                day = date.today() - timedelta(days=offset)
                shot = snap.Snapshot(taken_at=datetime.combine(day, datetime.min.time()))
                snap.save(shot, directory)
            found = snap.previous(directory, before=date.today())
            self.assertEqual(found.day, date.today() - timedelta(days=1))

    def test_previous_ignores_today_and_the_future(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            snap.save(snap.Snapshot(taken_at=NOW), directory)
            self.assertIsNone(snap.previous(directory, before=date.today()))

    def test_prune_keeps_only_the_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for offset in range(6):
                day = date.today() - timedelta(days=offset)
                snap.save(
                    snap.Snapshot(taken_at=datetime.combine(day, datetime.min.time())),
                    directory,
                )
            snap.prune(directory, keep=3)
            self.assertEqual(len(list(directory.glob("*.json"))), 3)

    def test_stray_files_do_not_break_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "notes.json").write_text("{}", encoding="utf-8")
            self.assertIsNone(snap.previous(directory, before=date.today()))


class NewVideoTests(unittest.TestCase):
    def _snapshots(self, before: list[dict], after: list[dict]):
        with tempfile.TemporaryDirectory() as tmp:
            first = snap.Snapshot.from_records(
                registry_of(Path(tmp) / "a", before)
            )
            second = snap.Snapshot.from_records(
                registry_of(Path(tmp) / "b", after)
            )
            return first, second

    def test_first_run_reports_nothing_as_new(self):
        # 620 channels did not all publish at once; a baseline is not news.
        with tempfile.TemporaryDirectory() as tmp:
            current = snap.Snapshot.from_records(
                registry_of(Path(tmp), [channel("UC1", [video("v1", "T", 10)])])
            )
            fresh, missed = daily_report.diff_new_videos(current, None)
            self.assertEqual(fresh, [])
            self.assertEqual(missed, [])

    def test_only_unseen_videos_are_new(self):
        before, after = self._snapshots(
            [channel("UC1", [video("v1", "Old", 5)])],
            [channel("UC1", [video("v1", "Old", 5), video("v2", "New", 0)])],
        )
        fresh, _ = daily_report.diff_new_videos(after, before)
        self.assertEqual([v.video_id for v in fresh], ["v2"])

    def test_new_videos_are_sorted_by_outlier_ratio(self):
        before, after = self._snapshots(
            [channel("UC1", [video("v0", "Seed", 1)])],
            [
                channel(
                    "UC1",
                    [video("v0", "Seed", 1), video("v1", "Modest", 60_000),
                     video("v2", "Huge", 500_000)],
                )
            ],
        )
        fresh, _ = daily_report.diff_new_videos(after, before)
        self.assertEqual([v.video_id for v in fresh], ["v2", "v1"])

    def test_wholly_new_window_is_flagged_as_possibly_missed(self):
        # The registry keeps ten videos; if all ten are new, more were posted.
        before, after = self._snapshots(
            [channel("UC1", [video("v0", "Old", 9)])],
            [channel("UC1", [video(f"n{i}", f"New {i}", 1000) for i in range(10)])],
        )
        _, missed = daily_report.diff_new_videos(after, before)
        self.assertEqual(missed, ["UC1"])

    def test_new_channel_is_not_reported_as_missed(self):
        before, after = self._snapshots(
            [channel("UC1", [video("v0", "Old", 9)])],
            [
                channel("UC1", [video("v0", "Old", 9)]),
                channel("UC2", [video("x1", "First", 1000)]),
            ],
        )
        _, missed = daily_report.diff_new_videos(after, before)
        self.assertEqual(missed, [])


class StatusChangeTests(unittest.TestCase):
    def _pair(self, before: list[dict], after: list[dict]):
        with tempfile.TemporaryDirectory() as tmp:
            return (
                snap.Snapshot.from_records(registry_of(Path(tmp) / "a", before)),
                snap.Snapshot.from_records(registry_of(Path(tmp) / "b", after)),
            )

    def test_no_changes_on_the_first_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = snap.Snapshot.from_records(
                registry_of(Path(tmp), [channel("UC1", [])])
            )
            self.assertEqual(daily_report.diff_status(current, None), [])

    def test_editorial_status_transition_is_reported(self):
        before, after = self._pair(
            [channel("UC1", [], status="WATCH_CORE")],
            [channel("UC1", [], status="REVIEW")],
        )
        kinds = {c.kind: c for c in daily_report.diff_status(after, before)}
        self.assertIn("status_changed", kinds)
        self.assertEqual(kinds["status_changed"].after, "REVIEW")

    def test_numeric_filter_crossings_are_reported_both_ways(self):
        before, after = self._pair(
            [channel("UC1", [], passes=False)], [channel("UC1", [], passes=True)]
        )
        self.assertIn(
            "filter_gained", {c.kind for c in daily_report.diff_status(after, before)}
        )
        back = daily_report.diff_status(before, after)
        self.assertIn("filter_lost", {c.kind for c in back})

    def test_subscriber_move_below_threshold_is_ignored(self):
        before, after = self._pair(
            [channel("UC1", [], subscribers=100_000)],
            [channel("UC1", [], subscribers=101_000)],
        )
        self.assertEqual(
            [c for c in daily_report.diff_status(after, before) if "subscriber" in c.kind],
            [],
        )

    def test_large_subscriber_jump_is_reported(self):
        before, after = self._pair(
            [channel("UC1", [], subscribers=100_000)],
            [channel("UC1", [], subscribers=130_000)],
        )
        jumps = [c for c in daily_report.diff_status(after, before) if c.kind == "subscriber_jump"]
        self.assertEqual(len(jumps), 1)
        self.assertAlmostEqual(jumps[0].magnitude, 30.0)

    def test_added_and_removed_channels_are_reported(self):
        before, after = self._pair(
            [channel("UC1", [])], [channel("UC2", [])]
        )
        kinds = {c.kind for c in daily_report.diff_status(after, before)}
        self.assertEqual(kinds, {"channel_added", "channel_removed"})


class SpikeAndQuietTests(unittest.TestCase):
    def _new(self, views: int, baseline: int) -> daily_report.NewVideo:
        return daily_report.NewVideo(
            video_id="v", channel_id="UC1", channel_title="C", title="T",
            views=views, published_at=NOW.isoformat(), baseline_views=baseline,
            status="WATCH_CORE",
        )

    def test_spike_needs_to_clear_the_ratio(self):
        self.assertEqual(daily_report.find_spikes([self._new(100_000, 50_000)]), [])
        self.assertEqual(len(daily_report.find_spikes([self._new(200_000, 50_000)])), 1)

    def test_missing_baseline_never_counts_as_a_spike(self):
        self.assertEqual(daily_report.find_spikes([self._new(9_000_000, 0)]), [])

    def test_quiet_channels_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = snap.Snapshot.from_records(
                registry_of(Path(tmp), [channel("UC1", [video("v1", "Old", 1000, 60)])])
            )
            quiet = daily_report.find_quiet(current, today=date.today())
            self.assertEqual(len(quiet), 1)
            self.assertGreaterEqual(quiet[0].magnitude, 21)

    def test_only_watched_channels_are_chased_for_silence(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = snap.Snapshot.from_records(
                registry_of(
                    Path(tmp),
                    [channel("UC1", [video("v1", "Old", 1000, 60)], status="REVIEW")],
                )
            )
            self.assertEqual(daily_report.find_quiet(current, today=date.today()), [])


class ArtifactTests(unittest.TestCase):
    def _report(self, tmp: Path) -> daily_report.DailyReport:
        before_records = registry_of(
            tmp / "a", [channel("UC1", [video("v0", "AI model seed", 60_000, 5)])]
        )
        after_records = registry_of(
            tmp / "b",
            [
                channel(
                    "UC1",
                    [
                        video("v0", "AI model seed", 60_000, 5),
                        video("v1", "New AI model breaks benchmark", 400_000),
                    ],
                ),
                channel(
                    "UC2",
                    [video("v2", "The new AI model benchmark, explained", 300_000)],
                    title="Second",
                ),
            ],
        )
        return daily_report.build_report(
            after_records,
            snap.Snapshot.from_records(after_records),
            snap.Snapshot.from_records(before_records),
        )

    def test_all_four_artifacts_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = daily_report.write_artifacts(
                self._report(Path(tmp)), Path(tmp) / "out"
            )
            self.assertEqual(
                sorted(p.name for p in written.values()),
                sorted([
                    daily_report.DIGEST_FILE,
                    daily_report.NEW_VIDEOS_FILE,
                    daily_report.STATUS_CHANGES_FILE,
                    daily_report.TOPIC_SIGNALS_FILE,
                ]),
            )
            for path in written.values():
                self.assertTrue(path.stat().st_size > 0, path)

    def test_topic_signals_round_trip_back_into_topics(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = daily_report.write_artifacts(
                self._report(Path(tmp)), Path(tmp) / "out"
            )
            topics = daily_report.load_topic_signals(written["topic_signals"])
            self.assertTrue(topics)
            self.assertTrue(topics[0].evidence)
            self.assertTrue(topics[0].slug)
            # Evidence must survive: build() reports why a topic is trending.
            self.assertGreater(topics[0].evidence[0].views, 0)

    def test_freshness_marks_topics_driven_by_new_uploads(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = self._report(Path(tmp))
            self.assertTrue(report.topics)
            self.assertGreater(max(report.topic_freshness.values()), 0.0)

    def test_digest_mentions_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = daily_report.render_digest(self._report(Path(tmp)))
            self.assertIn("verified atlas", text)

    def test_baseline_run_says_so_in_the_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = registry_of(Path(tmp), [channel("UC1", [video("v1", "T", 10)])])
            report = daily_report.build_report(
                records, snap.Snapshot.from_records(records), None
            )
            self.assertTrue(report.baseline_run)
            self.assertIn("baseline", daily_report.render_digest(report).lower())

    def test_new_videos_file_records_the_outlier_ratio(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = daily_report.write_artifacts(
                self._report(Path(tmp)), Path(tmp) / "out"
            )
            payload = json.loads(written["new_videos"].read_text(encoding="utf-8"))
            self.assertTrue(payload["videos"])
            self.assertIn("outlier_ratio", payload["videos"][0])


class RegistryResilienceTests(unittest.TestCase):
    def test_one_broken_registry_does_not_lose_the_others(self):
        # A daily job that dies on one half-written profile produces no digest
        # at all, which is worse than a digest missing one profile.
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "ai_science_en.json").write_text(
                json.dumps({"channels": [channel("UC1", [])]}), encoding="utf-8"
            )
            (directory / "academic_science_en.json").write_text(
                "{ not json", encoding="utf-8"
            )
            problems: list[str] = []
            records = channel_registry.load_dir(directory, problems=problems)
            self.assertEqual(len(records), 1)
            self.assertEqual(len(problems), 1)
            self.assertIn("academic_science_en", problems[0])

    def test_total_failure_still_raises_and_names_the_problems(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "ai_science_en.json").write_text("{ not json", encoding="utf-8")
            problems: list[str] = []
            with self.assertRaises(channel_registry.RegistryError) as ctx:
                channel_registry.load_dir(directory, problems=problems)
            self.assertIn("ai_science_en", str(ctx.exception))


class DomainLabelTests(unittest.TestCase):
    def test_topic_records_the_domain_it_was_scored_against(self):
        # The label previously came from the policy default, mislabelling every
        # registry-sourced topic while the scoring underneath was correct.
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            payload = [
                channel("UC1", [video("v1", "New LLM benchmark record", 400_000)]),
                channel("UC2", [video("v2", "LLM benchmark record analysed", 300_000)]),
            ]
            (directory / "ai_science_en.json").write_text(
                json.dumps({"channels": payload}), encoding="utf-8"
            )
            records = channel_registry.load_dir(directory)
            topics, _ = daily_report.rank_daily_topics(records, set())
            self.assertTrue(topics)
            self.assertEqual(topics[0].domain, "ai_science")


class ZipappTests(unittest.TestCase):
    """The single-file deployment must stay runnable.

    Two things silently break a zipapp and nothing else catches them: reading
    data files through ``__file__`` instead of ``importlib.resources``, and a
    module that only imports because the repository happens to be the working
    directory.
    """

    @classmethod
    def setUpClass(cls):
        import subprocess
        import sys

        from pipeline import build_pyz

        cls._tmp = tempfile.TemporaryDirectory()
        cls.pyz = build_pyz.build(Path(cls._tmp.name) / "p.pyz")
        cls.invoke = staticmethod(lambda *args: subprocess.run(
            [sys.executable, str(cls.pyz), *args],
            capture_output=True, text=True, cwd=cls._tmp.name,
        ))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_archive_is_built_and_small(self):
        size_kb = self.pyz.stat().st_size / 1024
        self.assertLess(size_kb, 500, "archive should stay far smaller than the repo")

    def test_runs_outside_the_repository(self):
        result = type(self).invoke("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("daily", result.stdout)
        self.assertIn("registry", result.stdout)

    def test_doctor_runs_from_the_archive(self):
        directory = Path(self._tmp.name) / "reg"
        directory.mkdir(exist_ok=True)
        (directory / "ai_science_en.json").write_text(
            json.dumps({"channels": [channel("UC1", [video("v1", "AI result", 90_000)])]}),
            encoding="utf-8",
        )
        result = type(self).invoke(
            "--registry-dir", str(directory), "registry", "--doctor"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("channels parsed:", result.stdout)
        self.assertNotIn("WARNING", result.stdout)

    def test_daily_writes_all_four_artifacts_from_the_archive(self):
        directory = Path(self._tmp.name) / "reg2"
        directory.mkdir(exist_ok=True)
        (directory / "ai_science_en.json").write_text(
            json.dumps({"channels": [channel("UC1", [video("v1", "AI result", 90_000)])]}),
            encoding="utf-8",
        )
        out = Path(self._tmp.name) / "out"
        result = type(self).invoke(
            "--registry-dir", str(directory), "daily",
            "--out-dir", str(out), "--snapshot-dir", str(out / "snap"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in (
            daily_report.NEW_VIDEOS_FILE,
            daily_report.TOPIC_SIGNALS_FILE,
            daily_report.STATUS_CHANGES_FILE,
            daily_report.DIGEST_FILE,
        ):
            self.assertTrue((out / name).exists(), f"{name} missing")

    def test_data_files_are_readable_from_inside_the_archive(self):
        # Regression: Lexicon read its JSON via __file__, which has no
        # filesystem path inside a zipapp and silently yielded no terms.
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-c",
             "from pipeline.contracts import Language\n"
             "from pipeline.render.tts_elevenlabs import Lexicon\n"
             "print(len(Lexicon.for_language(Language.RU)))"],
            capture_output=True, text=True,
            env={"PYTHONPATH": str(self.pyz), "PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(int(result.stdout.strip()), 40)

    def test_tests_and_docs_are_not_shipped(self):
        import zipfile

        with zipfile.ZipFile(self.pyz) as archive:
            names = archive.namelist()
        self.assertFalse([n for n in names if "/tests/" in n], "tests shipped")
        self.assertFalse([n for n in names if "/docs/" in n], "docs shipped")
        self.assertTrue([n for n in names if n.endswith("lexicon_ru.json")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
