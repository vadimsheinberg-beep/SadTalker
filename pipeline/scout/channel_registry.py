"""Reader for the deployed channel registries.

The control plane already monitors the watch list: ``youtube-channel-monitor``
refreshes four JSON registries under
``/opt/tzoar/deploy/production/channel_registries/`` and stores, per channel,
the subscriber count, the last ten videos with their view counts, the last
publication date, 30-day activity, the best result, the median of the top three
videos, the views-to-subscribers ratio, whether the numeric filter passed, and
when it was last checked.

The scout therefore must **read** this, not re-fetch it. With 620 channels a
``search.list`` sweep costs ~62,000 quota units a day against the 20,000 two
keys provide -- 3.1x over, failing every day before lunch. Reading the
registry costs nothing and uses numbers that are already there.

This module deliberately does not know the exact field names: the registries
were written by another tool that could not be inspected from here, so every
field is looked up through a list of plausible aliases and
:func:`describe_registry` reports what was actually found. Run
``python -m pipeline registry --doctor`` on the server once and correct the
alias tables from its output rather than guessing twice.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..contracts import VideoSignal

REGISTRY_DIR = Path("/opt/tzoar/deploy/production/channel_registries")

REGISTRY_FILES: tuple[str, ...] = (
    "ai_science_en.json",
    "ai_science_ru.json",
    "academic_science_en.json",
    "academic_science_ru.json",
)


class EditorialStatus(str, Enum):
    """Editorial classification (v2), as maintained by the operator."""

    WATCH_CORE = "WATCH_CORE"
    WATCH_WEEKLY = "WATCH_WEEKLY"
    REVIEW = "REVIEW"
    RIGHTS_CHECK = "RIGHTS_CHECK"
    EXCLUDE_TOPIC = "EXCLUDE_TOPIC"
    EXCLUDE_QUALITY = "EXCLUDE_QUALITY"
    UNCLASSIFIED = "UNCLASSIFIED"

    @property
    def excluded(self) -> bool:
        """Excluded channels are never read for topics. An editorial decision."""
        return self in (EditorialStatus.EXCLUDE_TOPIC, EditorialStatus.EXCLUDE_QUALITY)

    @property
    def usable_as_source(self) -> bool:
        """Whether material from this channel may inform a published video.

        ``RIGHTS_CHECK`` still counts as a trend signal -- reading a public
        title tells us what YouTube is promoting -- but it must not become a
        source until the rights question is settled, so it is separated here
        rather than lumped in with the watch statuses.
        """
        return self in (
            EditorialStatus.WATCH_CORE,
            EditorialStatus.WATCH_WEEKLY,
            EditorialStatus.REVIEW,
        )


class RegistryError(RuntimeError):
    pass


# Field aliases. Each tuple is tried in order; the first present wins.
ALIASES: dict[str, tuple[str, ...]] = {
    "channel_id": ("channel_id", "channelId", "id", "uc_id", "ucid"),
    "title": ("title", "name", "channel_title", "channel_name"),
    "handle": ("handle", "custom_url", "customUrl", "url"),
    "subscribers": (
        "subscribers", "subscriber_count", "subscriberCount", "subs", "subscribers_count",
    ),
    "videos": ("last_videos", "recent_videos", "videos", "last_10_videos", "latest_videos"),
    "last_published": (
        "last_published", "last_publish_date", "last_video_date", "lastPublishedAt",
        "last_upload", "latest_publish_date",
    ),
    "activity_30d": ("activity_30d", "videos_last_30d", "activity_last_30_days", "activity30d"),
    "best_views": ("best_views", "best_result", "max_views", "top_views"),
    "median_top3": (
        "median_top3", "median_views_top3", "top3_median_views", "median_top_3", "median_views",
    ),
    "views_per_sub": (
        "views_per_sub", "views_to_subs_ratio", "ratio", "view_sub_ratio", "vps",
    ),
    "passes_filter": (
        "passes_filter", "numeric_filter_passed", "filter_passed", "passed_filter", "active",
    ),
    "status": ("status", "editorial_status", "status_v2", "editorial_status_v2"),
    "checked_at": ("checked_at", "last_checked", "last_check", "updated_at", "checkedAt"),
}

VIDEO_ALIASES: dict[str, tuple[str, ...]] = {
    "video_id": ("video_id", "videoId", "id"),
    "title": ("title", "name"),
    "views": ("views", "view_count", "viewCount"),
    "published_at": ("published_at", "publishedAt", "date", "published"),
    "duration_s": ("duration_s", "duration", "length_s", "durationSec"),
}


def _pick(record: dict[str, Any], names: Sequence[str], default: Any = None) -> Any:
    for name in names:
        if name in record and record[name] not in (None, ""):
            return record[name]
    return default


def _as_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _as_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    for parse in (
        datetime.fromisoformat,
        lambda t: datetime.strptime(t, "%Y-%m-%d"),
        lambda t: datetime.strptime(t, "%Y-%m-%d %H:%M:%S"),
    ):
        try:
            parsed = parse(text)
        except (ValueError, TypeError):
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


@dataclass
class ChannelRecord:
    channel_id: str
    title: str = ""
    handle: str = ""
    subscribers: int = 0
    videos: list[dict[str, Any]] = field(default_factory=list)
    last_published: datetime | None = None
    activity_30d: int = 0
    best_views: int = 0
    median_top3: int = 0
    views_per_sub: float = 0.0
    passes_filter: bool = False
    status: EditorialStatus = EditorialStatus.UNCLASSIFIED
    checked_at: datetime | None = None
    profiles: set[str] = field(default_factory=set)

    @property
    def baseline_views(self) -> int:
        """The per-channel yardstick an outlier is measured against.

        Prefers the registry's own median-of-top-three. That is a
        high-percentile figure rather than a true median, so it is a
        deliberately conservative baseline: a video only reads as an outlier
        when it beats the channel's *good* videos, not its typical ones.
        """
        if self.median_top3 > 0:
            return self.median_top3
        counts = sorted(
            (_as_int(_pick(v, VIDEO_ALIASES["views"], 0)) for v in self.videos),
            reverse=True,
        )
        if not counts:
            return 0
        top = counts[: max(1, min(3, len(counts)))]
        return sorted(top)[len(top) // 2]

    def to_signals(self) -> list[VideoSignal]:
        """Turn the stored last-N videos into scout signals."""
        signals: list[VideoSignal] = []
        baseline = self.baseline_views
        for raw in self.videos:
            if not isinstance(raw, dict):
                continue
            published = _as_datetime(_pick(raw, VIDEO_ALIASES["published_at"]))
            if published is None:
                continue
            video_id = str(_pick(raw, VIDEO_ALIASES["video_id"], "") or "")
            title = str(_pick(raw, VIDEO_ALIASES["title"], "") or "")
            if not title:
                continue
            signals.append(
                VideoSignal(
                    video_id=video_id,
                    channel_id=self.channel_id,
                    title=title,
                    published_at=published,
                    views=_as_int(_pick(raw, VIDEO_ALIASES["views"], 0)),
                    duration_s=_as_int(_pick(raw, VIDEO_ALIASES["duration_s"], 0)),
                    channel_median_views=baseline,
                )
            )
        return signals


def parse_channel(record: dict[str, Any], profile: str = "") -> ChannelRecord | None:
    channel_id = _pick(record, ALIASES["channel_id"], "")
    if not channel_id:
        return None
    raw_status = str(_pick(record, ALIASES["status"], "") or "").strip().upper()
    try:
        status = EditorialStatus(raw_status)
    except ValueError:
        status = EditorialStatus.UNCLASSIFIED

    passes = _pick(record, ALIASES["passes_filter"], False)
    return ChannelRecord(
        channel_id=str(channel_id),
        title=str(_pick(record, ALIASES["title"], "") or ""),
        handle=str(_pick(record, ALIASES["handle"], "") or ""),
        subscribers=_as_int(_pick(record, ALIASES["subscribers"], 0)),
        videos=list(_pick(record, ALIASES["videos"], []) or []),
        last_published=_as_datetime(_pick(record, ALIASES["last_published"])),
        activity_30d=_as_int(_pick(record, ALIASES["activity_30d"], 0)),
        best_views=_as_int(_pick(record, ALIASES["best_views"], 0)),
        median_top3=_as_int(_pick(record, ALIASES["median_top3"], 0)),
        views_per_sub=float(_pick(record, ALIASES["views_per_sub"], 0.0) or 0.0),
        passes_filter=bool(passes) and str(passes).lower() not in ("false", "0", "no"),
        status=status,
        checked_at=_as_datetime(_pick(record, ALIASES["checked_at"])),
        profiles={profile} if profile else set(),
    )


def _records_in(payload: Any) -> list[dict[str, Any]]:
    """Find the channel list regardless of how the file wraps it."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("channels", "records", "items", "entries", "data", "registry"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                # Keyed by channel id: fold the key in as channel_id.
                return [
                    {**item, "channel_id": item.get("channel_id", key_id)}
                    for key_id, item in value.items()
                    if isinstance(item, dict)
                ]
        # The whole object may itself be keyed by channel id.
        if payload and all(isinstance(v, dict) for v in payload.values()):
            return [
                {**item, "channel_id": item.get("channel_id", key_id)}
                for key_id, item in payload.items()
            ]
    return []


def load_file(path: Path, profile: str = "") -> list[ChannelRecord]:
    path = Path(path)
    if not path.exists():
        raise RegistryError(f"registry file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RegistryError(f"{path} is not valid JSON: {exc}") from exc

    records = _records_in(payload)
    if not records:
        raise RegistryError(
            f"no channel records found in {path}. Top-level keys: "
            f"{sorted(payload)[:10] if isinstance(payload, dict) else type(payload).__name__}. "
            "Add the wrapping key to _records_in() or the field to ALIASES."
        )
    parsed = [parse_channel(record, profile or path.stem) for record in records]
    return [record for record in parsed if record is not None]


def load_dir(
    directory: Path | None = None,
    files: Iterable[str] = REGISTRY_FILES,
    problems: list[str] | None = None,
) -> list[ChannelRecord]:
    """Load every registry and merge channels that appear in several profiles.

    27 of the 620 channels sit in more than one profile. Merging on channel id
    means such a channel is read once and contributes one set of signals, not
    two -- otherwise it would count double in the cross-channel agreement that
    decides whether a topic is real.

    One unreadable file does not abort the load: it is appended to ``problems``
    and the rest are read. A daily job that dies because one profile is empty
    or was half-written produces no digest at all, which is a worse outcome
    than a digest missing one profile and saying so. Only a total failure
    raises.
    """
    directory = Path(directory or REGISTRY_DIR)
    merged: dict[str, ChannelRecord] = {}
    # Collected regardless of whether the caller wants them: when every file
    # fails, the reason is the only useful thing left to report.
    collected: list[str] = []
    for name in files:
        path = directory / name
        if not path.exists():
            continue
        try:
            loaded = load_file(path, profile=path.stem)
        except RegistryError as exc:
            collected.append(str(exc))
            if problems is not None:
                problems.append(str(exc))
            continue
        for record in loaded:
            existing = merged.get(record.channel_id)
            if existing is None:
                merged[record.channel_id] = record
            else:
                existing.profiles |= record.profiles
                # Keep the most recently checked copy's metrics.
                if (record.checked_at or datetime.min.replace(tzinfo=timezone.utc)) > (
                    existing.checked_at or datetime.min.replace(tzinfo=timezone.utc)
                ):
                    record.profiles = existing.profiles
                    merged[record.channel_id] = record
    if not merged:
        detail = f" Problems: {'; '.join(collected)}" if collected else ""
        raise RegistryError(
            f"no registries loaded from {directory}. "
            f"Expected: {', '.join(files)}.{detail}"
        )
    return list(merged.values())


def scoutable(records: Iterable[ChannelRecord]) -> list[ChannelRecord]:
    """Channels the scout may read for topics.

    Excluded channels are dropped on editorial grounds. Everything else is
    read, including channels that have not passed the numeric filter: the
    filter decides who is worth imitating, not who is worth watching, and a
    quiet channel that suddenly spikes is exactly the signal worth having.
    """
    return [record for record in records if not record.status.excluded]


def domain_map(records: Iterable[ChannelRecord]) -> dict[str, str]:
    """Map each channel to the domain vocabulary of its source registry.

    This removes the need for a global domain setting: a channel from
    ``ai_science_ru.json`` is scored against AI vocabulary and one from
    ``academic_science_en.json`` against academic vocabulary, so the
    wrong-domain failure -- every title flattened to the affinity floor while
    still producing a plausible-looking ranking -- cannot happen by omission.

    A channel in several profiles takes the domain its profiles agree on;
    where they disagree the first sorted profile wins, deterministically.
    """
    from .trend_scout import REGISTRY_DOMAINS

    mapping: dict[str, str] = {}
    for record in records:
        domains = {
            REGISTRY_DOMAINS[profile]
            for profile in record.profiles
            if profile in REGISTRY_DOMAINS
        }
        if len(domains) == 1:
            mapping[record.channel_id] = next(iter(domains))
        elif domains:
            for profile in sorted(record.profiles):
                if profile in REGISTRY_DOMAINS:
                    mapping[record.channel_id] = REGISTRY_DOMAINS[profile]
                    break
    return mapping


def signals_from(records: Iterable[ChannelRecord]) -> list[VideoSignal]:
    signals: list[VideoSignal] = []
    for record in scoutable(records):
        signals.extend(record.to_signals())
    return signals


def describe_registry(records: Sequence[ChannelRecord]) -> str:
    """Report what was parsed, for verifying the aliases against real files."""
    if not records:
        return "no channels parsed"

    by_status: dict[str, int] = {}
    for record in records:
        by_status[record.status.value] = by_status.get(record.status.value, 0) + 1

    with_videos = sum(1 for r in records if r.videos)
    with_subs = sum(1 for r in records if r.subscribers > 0)
    with_baseline = sum(1 for r in records if r.baseline_views > 0)
    multi = sum(1 for r in records if len(r.profiles) > 1)
    signals = signals_from(records)

    lines = [
        f"channels parsed:      {len(records)}",
        f"in >1 profile:        {multi}",
        f"with subscriber count:{with_subs:>5}",
        f"with stored videos:   {with_videos:>5}",
        f"with usable baseline: {with_baseline:>5}",
        f"usable video signals: {len(signals):>5}",
        f"scoutable (not excluded): {len(scoutable(records))}",
        "",
        "editorial status:",
    ]
    lines += [f"  {name:<18} {count}" for name, count in sorted(by_status.items())]

    unresolved = [
        name
        for name, count in (
            ("subscribers", with_subs),
            ("videos", with_videos),
            ("baseline", with_baseline),
        )
        if count == 0
    ]
    if unresolved:
        lines += [
            "",
            f"WARNING: no values found for {', '.join(unresolved)} -- the field "
            "names in ALIASES do not match these files. Correct them before "
            "trusting any ranking.",
        ]
    return "\n".join(lines)
