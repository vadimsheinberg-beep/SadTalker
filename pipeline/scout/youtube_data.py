"""Thin YouTube Data API v3 client with key rotation.

Two keys live in ``/opt/tzoar/deploy/.env.ytdata``. Data API quota is per-key
and per-day, so a single 403 ``quotaExceeded`` must not end the run -- it
rotates to the next key and retries once per key.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator, Sequence

from ..config import SECRETS, Secrets
from ..contracts import VideoSignal

API_ROOT = "https://www.googleapis.com/youtube/v3"
_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?$"
)


class QuotaExhausted(RuntimeError):
    """Every configured key returned quotaExceeded."""


def parse_iso8601_duration(value: str) -> int:
    """ISO-8601 duration -> seconds. Returns 0 for anything unparseable."""
    match = _DURATION_RE.match(value or "")
    if not match:
        return 0
    parts = {k: int(v) for k, v in match.groupdict(default="0").items()}
    return (
        parts["days"] * 86400 + parts["h"] * 3600 + parts["m"] * 60 + parts["s"]
    )


def parse_rfc3339(value: str) -> datetime:
    text = (value or "").replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class _Transport:
    """Seam for tests: swap this out to avoid touching the network."""

    timeout: int = 20

    def get(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))


class YouTubeDataClient:
    def __init__(
        self,
        secrets: Secrets | None = None,
        transport: _Transport | None = None,
        keys: Sequence[str] | None = None,
    ) -> None:
        self._secrets = secrets or SECRETS
        self._transport = transport or _Transport()
        self._keys = list(keys) if keys is not None else None

    @property
    def keys(self) -> list[str]:
        if self._keys is None:
            found = self._secrets.all_matching("ytdata", "YT_DATA_API_KEY")
            if not found:
                found = self._secrets.all_matching("ytdata", "YOUTUBE_API_KEY")
            if not found:
                raise QuotaExhausted(
                    "no YouTube Data API keys found in .env.ytdata "
                    "(expected YT_DATA_API_KEY_1 / YT_DATA_API_KEY_2)"
                )
            self._keys = found
        return self._keys

    def _call(self, endpoint: str, **params: Any) -> dict[str, Any]:
        last_error: Exception | None = None
        for key in self.keys:
            query = urllib.parse.urlencode({**params, "key": key}, doseq=True)
            url = f"{API_ROOT}/{endpoint}?{query}"
            try:
                return self._transport.get(url)
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 429):
                    last_error = exc
                    continue  # quota or rate limit -- try the next key
                raise
        raise QuotaExhausted(
            f"all {len(self.keys)} Data API keys exhausted on {endpoint}"
        ) from last_error

    def recent_uploads(
        self, channel_id: str, lookback_hours: int, max_results: int = 50
    ) -> list[str]:
        """Video ids published by ``channel_id`` inside the lookback window."""
        after = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        payload = self._call(
            "search",
            part="id",
            channelId=channel_id,
            type="video",
            order="date",
            publishedAfter=after.strftime("%Y-%m-%dT%H:%M:%SZ"),
            maxResults=min(max_results, 50),
        )
        return [
            item["id"]["videoId"]
            for item in payload.get("items", [])
            if item.get("id", {}).get("videoId")
        ]

    def videos(self, video_ids: Iterable[str]) -> list[dict[str, Any]]:
        """Full snippet+statistics+contentDetails for up to 50 ids per call."""
        collected: list[dict[str, Any]] = []
        for batch in _chunk(list(video_ids), 50):
            if not batch:
                continue
            payload = self._call(
                "videos",
                part="snippet,statistics,contentDetails",
                id=",".join(batch),
                maxResults=50,
            )
            collected.extend(payload.get("items", []))
        return collected

    def signals(
        self, channel_ids: Sequence[str], lookback_hours: int
    ) -> list[VideoSignal]:
        """Fetch and normalise recent uploads across every registered channel.

        The per-channel median is computed from that channel's own window, so
        ``outlier_ratio`` compares a video against its siblings rather than
        against the platform.
        """
        signals: list[VideoSignal] = []
        for channel_id in channel_ids:
            items = self.videos(self.recent_uploads(channel_id, lookback_hours))
            views = sorted(
                int(item.get("statistics", {}).get("viewCount", 0)) for item in items
            )
            median = _median(views)
            for item in items:
                stats = item.get("statistics", {})
                snippet = item.get("snippet", {})
                signals.append(
                    VideoSignal(
                        video_id=item.get("id", ""),
                        channel_id=snippet.get("channelId", channel_id),
                        title=snippet.get("title", ""),
                        published_at=parse_rfc3339(snippet.get("publishedAt", "")),
                        views=int(stats.get("viewCount", 0)),
                        duration_s=parse_iso8601_duration(
                            item.get("contentDetails", {}).get("duration", "")
                        ),
                        channel_median_views=median,
                    )
                )
        return signals


def _chunk(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _median(values: Sequence[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2
