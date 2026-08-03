"""Daily snapshots of the channel registries.

The monitor keeps a *current* view: it overwrites each channel's metrics on
every run. Everything the daily layer reports -- new videos, status changes,
view spikes -- is a difference between two points in time, so the difference
has to be stored before it is lost.

A snapshot is deliberately small: only the fields a diff needs. Keeping the
whole registry would make snapshots big enough that nobody keeps a year of
them, and a year of them is what makes "this is unusual for this channel"
answerable later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..scout.channel_registry import ChannelRecord, EditorialStatus, _as_int
from ..scout.channel_registry import VIDEO_ALIASES, _pick

SNAPSHOT_DIR = Path("/opt/tzoar/deploy/production/daily/snapshots")


@dataclass(frozen=True)
class VideoStamp:
    video_id: str
    title: str
    views: int
    published_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "title": self.title,
            "views": self.views,
            "published_at": self.published_at,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "VideoStamp":
        return cls(
            video_id=str(raw.get("video_id", "")),
            title=str(raw.get("title", "")),
            views=_as_int(raw.get("views", 0)),
            published_at=str(raw.get("published_at", "")),
        )


@dataclass(frozen=True)
class ChannelStamp:
    channel_id: str
    title: str
    status: str
    passes_filter: bool
    subscribers: int
    best_views: int
    baseline_views: int
    videos: tuple[VideoStamp, ...] = ()
    profiles: tuple[str, ...] = ()

    @property
    def video_ids(self) -> set[str]:
        return {video.video_id for video in self.videos if video.video_id}

    def to_json(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "title": self.title,
            "status": self.status,
            "passes_filter": self.passes_filter,
            "subscribers": self.subscribers,
            "best_views": self.best_views,
            "baseline_views": self.baseline_views,
            "profiles": list(self.profiles),
            "videos": [video.to_json() for video in self.videos],
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "ChannelStamp":
        return cls(
            channel_id=str(raw.get("channel_id", "")),
            title=str(raw.get("title", "")),
            status=str(raw.get("status", EditorialStatus.UNCLASSIFIED.value)),
            passes_filter=bool(raw.get("passes_filter", False)),
            subscribers=_as_int(raw.get("subscribers", 0)),
            best_views=_as_int(raw.get("best_views", 0)),
            baseline_views=_as_int(raw.get("baseline_views", 0)),
            videos=tuple(
                VideoStamp.from_json(v) for v in raw.get("videos", []) if isinstance(v, dict)
            ),
            profiles=tuple(raw.get("profiles", [])),
        )

    @classmethod
    def from_record(cls, record: ChannelRecord) -> "ChannelStamp":
        videos: list[VideoStamp] = []
        for raw in record.videos:
            if not isinstance(raw, dict):
                continue
            video_id = str(_pick(raw, VIDEO_ALIASES["video_id"], "") or "")
            if not video_id:
                continue
            videos.append(
                VideoStamp(
                    video_id=video_id,
                    title=str(_pick(raw, VIDEO_ALIASES["title"], "") or ""),
                    views=_as_int(_pick(raw, VIDEO_ALIASES["views"], 0)),
                    published_at=str(_pick(raw, VIDEO_ALIASES["published_at"], "") or ""),
                )
            )
        return cls(
            channel_id=record.channel_id,
            title=record.title,
            status=record.status.value,
            passes_filter=record.passes_filter,
            subscribers=record.subscribers,
            best_views=record.best_views,
            baseline_views=record.baseline_views,
            videos=tuple(videos),
            profiles=tuple(sorted(record.profiles)),
        )


@dataclass
class Snapshot:
    taken_at: datetime
    channels: dict[str, ChannelStamp] = field(default_factory=dict)

    @property
    def day(self) -> date:
        return self.taken_at.date()

    def video_ids(self) -> set[str]:
        ids: set[str] = set()
        for stamp in self.channels.values():
            ids |= stamp.video_ids
        return ids

    @classmethod
    def from_records(
        cls, records: Iterable[ChannelRecord], taken_at: datetime | None = None
    ) -> "Snapshot":
        stamps = [ChannelStamp.from_record(record) for record in records]
        return cls(
            taken_at=taken_at or datetime.now(timezone.utc),
            channels={stamp.channel_id: stamp for stamp in stamps},
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "taken_at": self.taken_at.isoformat(),
                "channels": [stamp.to_json() for stamp in self.channels.values()],
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, text: str) -> "Snapshot":
        payload = json.loads(text)
        taken = payload.get("taken_at", "")
        try:
            taken_at = datetime.fromisoformat(str(taken).replace("Z", "+00:00"))
        except ValueError:
            taken_at = datetime.now(timezone.utc)
        if taken_at.tzinfo is None:
            taken_at = taken_at.replace(tzinfo=timezone.utc)
        stamps = [
            ChannelStamp.from_json(raw)
            for raw in payload.get("channels", [])
            if isinstance(raw, dict)
        ]
        return cls(
            taken_at=taken_at,
            channels={stamp.channel_id: stamp for stamp in stamps},
        )


def snapshot_path(directory: Path, day: date) -> Path:
    return Path(directory) / f"{day.isoformat()}.json"


def save(snapshot: Snapshot, directory: Path | None = None) -> Path:
    directory = Path(directory or SNAPSHOT_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    path = snapshot_path(directory, snapshot.day)
    path.write_text(snapshot.to_json(), encoding="utf-8")
    return path


def load(path: Path) -> Snapshot:
    return Snapshot.from_json(Path(path).read_text(encoding="utf-8"))


def previous(
    directory: Path | None = None, before: date | None = None
) -> Snapshot | None:
    """The most recent snapshot strictly before ``before``.

    Returns ``None`` when there is no earlier snapshot -- the first run. That
    case must not be treated as "620 channels just published everything", so
    callers check for it explicitly rather than diffing against an empty
    snapshot.
    """
    directory = Path(directory or SNAPSHOT_DIR)
    if not directory.exists():
        return None
    before = before or date.today()

    candidates: list[tuple[date, Path]] = []
    for path in directory.glob("*.json"):
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if day < before:
            candidates.append((day, path))
    if not candidates:
        return None
    return load(max(candidates)[1])


def prune(directory: Path | None = None, keep: int = 120) -> list[Path]:
    """Drop the oldest snapshots, keeping the most recent ``keep`` days."""
    directory = Path(directory or SNAPSHOT_DIR)
    if not directory.exists():
        return []
    dated: list[tuple[date, Path]] = []
    for path in directory.glob("*.json"):
        try:
            dated.append((date.fromisoformat(path.stem), path))
        except ValueError:
            continue
    dated.sort(reverse=True)
    removed: list[Path] = []
    for _, path in dated[keep:]:
        path.unlink(missing_ok=True)
        removed.append(path)
    return removed
