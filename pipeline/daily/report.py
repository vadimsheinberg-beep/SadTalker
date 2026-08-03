"""The daily layer: what changed, what is rising, and what to make.

Turns the monitor's raw metrics into the four artifacts the operator reads:

    daily_new_videos.json     what competitors published since the last run
    daily_topic_signals.json  ranked topics, marked by how fresh the evidence is
    status_changes.json       editorial and metric movements worth a human look
    daily_digest.md           the readable summary of the three above

``daily_topic_signals.json`` is also the input to script writing: the scout
runs once a day, a human reads the digest, and ``pipeline build --from-signals``
takes topics from the same ranked file rather than re-deriving them. That way
the thing a person reviewed is the thing that gets made.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..config import SETTINGS, DailyPolicy, ScoutPolicy
from ..contracts import TopicCandidate, VideoSignal
from ..scout import trend_scout
from ..scout.channel_registry import ChannelRecord, EditorialStatus, domain_map, signals_from
from .snapshot import ChannelStamp, Snapshot, VideoStamp

NEW_VIDEOS_FILE = "daily_new_videos.json"
TOPIC_SIGNALS_FILE = "daily_topic_signals.json"
STATUS_CHANGES_FILE = "status_changes.json"
DIGEST_FILE = "daily_digest.md"


@dataclass(frozen=True)
class NewVideo:
    video_id: str
    channel_id: str
    channel_title: str
    title: str
    views: int
    published_at: str
    baseline_views: int
    status: str

    @property
    def outlier_ratio(self) -> float:
        if self.baseline_views <= 0:
            return 1.0
        return self.views / self.baseline_views


@dataclass(frozen=True)
class Change:
    """One movement worth a human glance."""

    kind: str
    channel_id: str
    channel_title: str
    detail: str
    before: str = ""
    after: str = ""
    magnitude: float = 0.0


@dataclass
class DailyReport:
    day: date
    new_videos: list[NewVideo] = field(default_factory=list)
    topics: list[TopicCandidate] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    baseline_run: bool = False
    channels_seen: int = 0
    possibly_missed: list[str] = field(default_factory=list)
    topic_freshness: dict[str, float] = field(default_factory=dict)


def diff_new_videos(
    current: Snapshot, previous: Snapshot | None
) -> tuple[list[NewVideo], list[str]]:
    """Videos present now that were not present before.

    Also returns channels where *every* stored video is new. The registry keeps
    only the last ten, so that pattern means the channel published more than ten
    since the previous run and some are already out of the window -- worth
    saying rather than silently under-reporting.
    """
    if previous is None:
        return [], []

    known = previous.video_ids()
    fresh: list[NewVideo] = []
    missed: list[str] = []

    for channel_id, stamp in current.channels.items():
        stored = [video for video in stamp.videos if video.video_id]
        if not stored:
            continue
        new_here = [video for video in stored if video.video_id not in known]
        if new_here and len(new_here) == len(stored) and channel_id in previous.channels:
            missed.append(channel_id)
        for video in new_here:
            fresh.append(
                NewVideo(
                    video_id=video.video_id,
                    channel_id=channel_id,
                    channel_title=stamp.title,
                    title=video.title,
                    views=video.views,
                    published_at=video.published_at,
                    baseline_views=stamp.baseline_views,
                    status=stamp.status,
                )
            )

    fresh.sort(key=lambda video: (-video.outlier_ratio, -video.views))
    return fresh, missed


def diff_status(
    current: Snapshot,
    previous: Snapshot | None,
    policy: DailyPolicy | None = None,
) -> list[Change]:
    """Editorial and metric movements between two snapshots."""
    policy = policy or SETTINGS.daily
    if previous is None:
        return []

    changes: list[Change] = []
    for channel_id, now in current.channels.items():
        before = previous.channels.get(channel_id)
        if before is None:
            changes.append(
                Change(
                    kind="channel_added",
                    channel_id=channel_id,
                    channel_title=now.title,
                    detail="appeared in the registry",
                    after=now.status,
                )
            )
            continue

        if before.status != now.status:
            changes.append(
                Change(
                    kind="status_changed",
                    channel_id=channel_id,
                    channel_title=now.title,
                    detail=f"{before.status} → {now.status}",
                    before=before.status,
                    after=now.status,
                )
            )

        if before.passes_filter != now.passes_filter:
            changes.append(
                Change(
                    kind="filter_gained" if now.passes_filter else "filter_lost",
                    channel_id=channel_id,
                    channel_title=now.title,
                    detail=(
                        "now passes the numeric filter"
                        if now.passes_filter
                        else "no longer passes the numeric filter"
                    ),
                    before=str(before.passes_filter),
                    after=str(now.passes_filter),
                )
            )

        if before.subscribers > 0:
            growth = (now.subscribers - before.subscribers) / before.subscribers * 100
            if abs(growth) >= policy.subscriber_jump_pct:
                changes.append(
                    Change(
                        kind="subscriber_jump" if growth > 0 else "subscriber_drop",
                        channel_id=channel_id,
                        channel_title=now.title,
                        detail=(
                            f"{before.subscribers:,} → {now.subscribers:,} "
                            f"({growth:+.1f}%)"
                        ),
                        magnitude=round(growth, 2),
                    )
                )

    for channel_id in previous.channels.keys() - current.channels.keys():
        gone = previous.channels[channel_id]
        changes.append(
            Change(
                kind="channel_removed",
                channel_id=channel_id,
                channel_title=gone.title,
                detail="no longer in the registry",
                before=gone.status,
            )
        )

    changes.sort(key=lambda change: (change.kind, -abs(change.magnitude)))
    return changes


def find_spikes(
    new_videos: Sequence[NewVideo], policy: DailyPolicy | None = None
) -> list[Change]:
    """New videos far above their channel's own baseline.

    This is the signal the whole scout is built on -- a video beating its
    channel's good work by a wide margin is being pushed, not merely watched --
    so it is surfaced in the digest as an event, not only inside a topic score.
    """
    policy = policy or SETTINGS.daily
    spikes: list[Change] = []
    for video in new_videos:
        if video.baseline_views <= 0 or video.outlier_ratio < policy.spike_ratio:
            continue
        spikes.append(
            Change(
                kind="view_spike",
                channel_id=video.channel_id,
                channel_title=video.channel_title,
                detail=(
                    f"«{video.title}» — {video.views:,} views, "
                    f"{video.outlier_ratio:.1f}x this channel's baseline"
                ),
                magnitude=round(video.outlier_ratio, 2),
            )
        )
    spikes.sort(key=lambda change: -change.magnitude)
    return spikes


def find_quiet(
    current: Snapshot, policy: DailyPolicy | None = None, today: date | None = None
) -> list[Change]:
    """Watched channels that have not published for a long time."""
    policy = policy or SETTINGS.daily
    today = today or date.today()
    quiet: list[Change] = []
    for stamp in current.channels.values():
        if stamp.status not in (
            EditorialStatus.WATCH_CORE.value,
            EditorialStatus.WATCH_WEEKLY.value,
        ):
            continue
        latest = _latest_publish(stamp.videos)
        if latest is None:
            continue
        days = (today - latest).days
        if days >= policy.quiet_days:
            quiet.append(
                Change(
                    kind="went_quiet",
                    channel_id=stamp.channel_id,
                    channel_title=stamp.title,
                    detail=f"no upload for {days} days (last {latest.isoformat()})",
                    magnitude=float(days),
                )
            )
    quiet.sort(key=lambda change: -change.magnitude)
    return quiet


def _latest_publish(videos: Iterable[VideoStamp]) -> date | None:
    latest: date | None = None
    for video in videos:
        parsed = _as_date(video.published_at)
        if parsed and (latest is None or parsed > latest):
            latest = parsed
    return latest


def _as_date(value: str) -> date | None:
    text = (value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def rank_daily_topics(
    records: Sequence[ChannelRecord],
    new_video_ids: set[str],
    scout_policy: ScoutPolicy | None = None,
) -> tuple[list[TopicCandidate], dict[str, float]]:
    """Rank topics and measure how much of each is driven by today's uploads.

    Freshness separates "this subject is permanently popular here" from "this
    subject moved today". Both are real, but only the second is news, and the
    digest should not lead with an evergreen topic every morning.
    """
    signals = signals_from(records)
    topics = trend_scout.rank_topics(
        signals, scout_policy, domains=domain_map(records)
    )
    freshness: dict[str, float] = {}
    for topic in topics:
        ids = {signal.video_id for signal in topic.evidence if signal.video_id}
        freshness[topic.slug] = (
            round(len(ids & new_video_ids) / len(ids), 3) if ids else 0.0
        )
    return topics, freshness


def build_report(
    records: Sequence[ChannelRecord],
    current: Snapshot,
    prior: Snapshot | None,
    policy: DailyPolicy | None = None,
    scout_policy: ScoutPolicy | None = None,
    today: date | None = None,
) -> DailyReport:
    policy = policy or SETTINGS.daily
    new_videos, missed = diff_new_videos(current, prior)
    changes = diff_status(current, prior, policy)
    changes += find_spikes(new_videos, policy)
    changes += find_quiet(current, policy, today)

    new_ids = {video.video_id for video in new_videos}
    topics, freshness = rank_daily_topics(records, new_ids, scout_policy)

    return DailyReport(
        day=today or current.day,
        new_videos=new_videos,
        topics=topics,
        changes=changes,
        baseline_run=prior is None,
        channels_seen=len(current.channels),
        possibly_missed=missed,
        topic_freshness=freshness,
    )


def write_artifacts(report: DailyReport, outdir: Path) -> dict[str, Path]:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    written["new_videos"] = _write_json(
        outdir / NEW_VIDEOS_FILE,
        {
            "day": report.day.isoformat(),
            "baseline_run": report.baseline_run,
            "channels_seen": report.channels_seen,
            "possibly_missed_channels": report.possibly_missed,
            "count": len(report.new_videos),
            "videos": [
                {**asdict(video), "outlier_ratio": round(video.outlier_ratio, 3)}
                for video in report.new_videos
            ],
        },
    )

    written["topic_signals"] = _write_json(
        outdir / TOPIC_SIGNALS_FILE,
        {
            "day": report.day.isoformat(),
            "count": len(report.topics),
            "topics": [
                {
                    "slug": topic.slug,
                    "title": topic.title,
                    "score": topic.score,
                    "freshness": report.topic_freshness.get(topic.slug, 0.0),
                    "domain": topic.domain,
                    "distinct_channels": topic.distinct_channels,
                    "keywords": list(topic.keywords),
                    "evidence": [
                        {
                            "video_id": signal.video_id,
                            "channel_id": signal.channel_id,
                            "title": signal.title,
                            "views": signal.views,
                            "outlier_ratio": round(signal.outlier_ratio, 3),
                            "published_at": signal.published_at.isoformat(),
                            "duration_s": signal.duration_s,
                            "channel_median_views": signal.channel_median_views,
                        }
                        for signal in topic.evidence
                    ],
                }
                for topic in report.topics
            ],
        },
    )

    written["status_changes"] = _write_json(
        outdir / STATUS_CHANGES_FILE,
        {
            "day": report.day.isoformat(),
            "baseline_run": report.baseline_run,
            "count": len(report.changes),
            "changes": [asdict(change) for change in report.changes],
        },
    )

    digest = outdir / DIGEST_FILE
    digest.write_text(render_digest(report), encoding="utf-8")
    written["digest"] = digest
    return written


def _write_json(path: Path, payload: Any) -> Path:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def render_digest(report: DailyReport, policy: DailyPolicy | None = None) -> str:
    policy = policy or SETTINGS.daily
    lines: list[str] = [
        f"# Daily digest — {report.day.isoformat()}",
        "",
        f"{report.channels_seen} channels · {len(report.new_videos)} new videos · "
        f"{len(report.topics)} topics · {len(report.changes)} changes",
        "",
    ]

    if report.baseline_run:
        lines += [
            "> **First run — baseline only.** There is no earlier snapshot to",
            "> compare against, so no videos are reported as new and no status",
            "> changes are listed. Tomorrow's run is the first real digest.",
            "",
        ]

    if report.possibly_missed:
        lines += [
            f"> **Possibly missed uploads on {len(report.possibly_missed)} channels.**",
            "> Every stored video on these is new, and the registry keeps only the",
            "> last ten — they published more than ten since the previous run, so",
            "> some are already outside the window.",
            "",
        ]

    spikes = [c for c in report.changes if c.kind == "view_spike"]
    if spikes:
        lines += ["## Unexpected spikes", ""]
        lines += [
            f"- **{c.channel_title}** — {c.detail}" for c in spikes[: policy.digest_videos]
        ]
        lines += [""]

    if report.topics:
        lines += ["## Rising topics", "", "Sorted by score; ★ marks topics driven by today's uploads.", ""]
        for rank, topic in enumerate(report.topics[: policy.digest_topics], 1):
            fresh = report.topic_freshness.get(topic.slug, 0.0)
            mark = " ★" if fresh >= policy.fresh_topic_threshold else ""
            lines.append(
                f"{rank}. **{topic.slug}**{mark} — score {topic.score:.3f}, "
                f"{topic.distinct_channels} channels, {fresh:.0%} fresh "
                f"_({topic.domain})_"
            )
            lines.append(f"   - {topic.title}")
            lines.append(f"   - keywords: {', '.join(topic.keywords[:6])}")
        lines += [""]

    if report.new_videos:
        lines += ["## New videos", ""]
        for video in report.new_videos[: policy.digest_videos]:
            lines.append(
                f"- **{video.channel_title}** — «{video.title}» · "
                f"{video.views:,} views · {video.outlier_ratio:.1f}x baseline "
                f"· {video.status}"
            )
        if len(report.new_videos) > policy.digest_videos:
            lines.append(
                f"- _…and {len(report.new_videos) - policy.digest_videos} more, "
                f"see {NEW_VIDEOS_FILE}_"
            )
        lines += [""]

    editorial = [c for c in report.changes if c.kind != "view_spike"]
    if editorial:
        lines += ["## Status changes", ""]
        for change in editorial:
            lines.append(
                f"- `{change.kind}` **{change.channel_title or change.channel_id}** — "
                f"{change.detail}"
            )
        lines += [""]

    lines += [
        "## Next step",
        "",
        "Topics above are already written to "
        f"`{TOPIC_SIGNALS_FILE}`. To turn one into a script:",
        "",
        "```bash",
        f"python -m pipeline build --channel tamha --from-signals {TOPIC_SIGNALS_FILE} \\",
        "    --topic <slug>",
        "```",
        "",
        "The publication gate still applies: a topic with no verified atlas",
        "cluster behind it produces no video, however well it scored here.",
    ]
    return "\n".join(lines)


def load_topic_signals(path: Path) -> list[TopicCandidate]:
    """Read back ``daily_topic_signals.json`` as scout topics.

    Used by ``build --from-signals`` so the script is written from the same
    ranked list a human read in the digest, rather than from a fresh ranking
    that may have moved since.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    topics: list[TopicCandidate] = []
    for raw in payload.get("topics", []):
        evidence: list[VideoSignal] = []
        for item in raw.get("evidence", []):
            published = item.get("published_at", "")
            try:
                stamp = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
            except ValueError:
                stamp = datetime.now(timezone.utc)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            evidence.append(
                VideoSignal(
                    video_id=str(item.get("video_id", "")),
                    channel_id=str(item.get("channel_id", "")),
                    title=str(item.get("title", "")),
                    published_at=stamp,
                    views=int(item.get("views", 0) or 0),
                    duration_s=int(item.get("duration_s", 0) or 0),
                    channel_median_views=int(item.get("channel_median_views", 0) or 0),
                )
            )
        topics.append(
            TopicCandidate(
                slug=str(raw.get("slug", "")),
                title=str(raw.get("title", "")),
                keywords=tuple(raw.get("keywords", [])),
                evidence=tuple(evidence),
                score=float(raw.get("score", 0.0)),
                domain=str(raw.get("domain", "")),
            )
        )
    return topics
