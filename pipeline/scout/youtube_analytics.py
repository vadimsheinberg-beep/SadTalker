"""Analytics for our own two channels (TAMHA and Iahalom).

Distinct from :mod:`pipeline.scout.youtube_data`, which reads *other people's*
channels to find topics. This module reads our own performance, which needs
OAuth rather than an API key: the Analytics API only exposes a channel's data
to its owner.

Credentials live in ``/opt/tzoar/deploy/.env.youtube-analytics``:

    YT_ANALYTICS_CLIENT_ID
    YT_ANALYTICS_CLIENT_SECRET
    YT_ANALYTICS_REFRESH_TOKEN_RU
    YT_ANALYTICS_REFRESH_TOKEN_EN

Refresh tokens are long-lived and do not expire on their own, so treat them
exactly like passwords: 0600 on the server, never in a repository, never in
chat. If one is ever exposed, revoke it in the Google account's third-party
access settings -- rotating the client secret alone does not invalidate it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Sequence

from ..config import SECRETS, Secrets
from ..contracts import Channel

TOKEN_URL = "https://oauth2.googleapis.com/token"
REPORTS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"

DEFAULT_METRICS = (
    "views",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "averageViewPercentage",
    "subscribersGained",
)


class AnalyticsError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReportRow:
    dimensions: tuple[str, ...]
    metrics: dict[str, float]


class YouTubeAnalyticsClient:
    def __init__(self, secrets: Secrets | None = None, timeout: int = 30) -> None:
        self._secrets = secrets or SECRETS
        self._timeout = timeout
        self._access_tokens: dict[Channel, str] = {}

    def _refresh_token(self, channel: Channel) -> str:
        suffix = "RU" if channel is Channel.TAMHA else "EN"
        return self._secrets.require(
            "analytics", f"YT_ANALYTICS_REFRESH_TOKEN_{suffix}"
        )

    def access_token(self, channel: Channel) -> str:
        """Exchange the refresh token for a short-lived access token.

        Cached per process: access tokens last an hour and a scout run is
        minutes, so one exchange per channel per run is right.
        """
        if channel in self._access_tokens:
            return self._access_tokens[channel]

        body = urllib.parse.urlencode(
            {
                "client_id": self._secrets.require("analytics", "YT_ANALYTICS_CLIENT_ID"),
                "client_secret": self._secrets.require(
                    "analytics", "YT_ANALYTICS_CLIENT_SECRET"
                ),
                "refresh_token": self._refresh_token(channel),
                "grant_type": "refresh_token",
            }
        ).encode("utf-8")
        try:
            with urllib.request.urlopen(
                urllib.request.Request(TOKEN_URL, data=body), timeout=self._timeout
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise AnalyticsError(
                f"token refresh failed for {channel.value} ({exc.code}): {detail}. "
                "If this says invalid_grant the refresh token was revoked or "
                "expired and must be reissued."
            ) from exc
        except urllib.error.URLError as exc:
            raise AnalyticsError(f"token endpoint unreachable: {exc}") from exc

        token = payload.get("access_token")
        if not token:
            raise AnalyticsError(f"no access_token in refresh response for {channel.value}")
        self._access_tokens[channel] = token
        return token

    def query(
        self,
        channel: Channel,
        start: date,
        end: date,
        metrics: Sequence[str] = DEFAULT_METRICS,
        dimensions: Sequence[str] = ("day",),
        sort: str = "day",
        max_results: int = 200,
    ) -> list[ReportRow]:
        params = {
            "ids": "channel==MINE",
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "metrics": ",".join(metrics),
            "maxResults": max_results,
        }
        if dimensions:
            params["dimensions"] = ",".join(dimensions)
        if sort:
            params["sort"] = sort

        request = urllib.request.Request(
            f"{REPORTS_URL}?{urllib.parse.urlencode(params)}",
            headers={"Authorization": f"Bearer {self.access_token(channel)}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise AnalyticsError(f"analytics query failed ({exc.code}): {detail}") from exc
        except urllib.error.URLError as exc:
            raise AnalyticsError(f"analytics endpoint unreachable: {exc}") from exc

        return parse_report(payload)


def parse_report(payload: dict[str, Any]) -> list[ReportRow]:
    """Turn the column-header + row-array response into named rows."""
    headers = [column.get("name", "") for column in payload.get("columnHeaders", [])]
    kinds = [column.get("columnType", "") for column in payload.get("columnHeaders", [])]
    rows: list[ReportRow] = []
    for raw in payload.get("rows", []):
        dimensions: list[str] = []
        metrics: dict[str, float] = {}
        for name, kind, value in zip(headers, kinds, raw):
            if kind == "DIMENSION":
                dimensions.append(str(value))
            else:
                try:
                    metrics[name] = float(value)
                except (TypeError, ValueError):
                    metrics[name] = 0.0
        rows.append(ReportRow(dimensions=tuple(dimensions), metrics=metrics))
    return rows


def retention_summary(rows: Sequence[ReportRow]) -> dict[str, float]:
    """Aggregate a day-dimensioned report into headline numbers.

    ``averageViewPercentage`` is the one that matters for pacing decisions: if
    the corrected 130-155 wpm band is right, retention should move, and this is
    where that shows up.
    """
    if not rows:
        return {}
    total_views = sum(row.metrics.get("views", 0.0) for row in rows)
    summary = {
        "views": total_views,
        "estimatedMinutesWatched": sum(
            row.metrics.get("estimatedMinutesWatched", 0.0) for row in rows
        ),
        "subscribersGained": sum(
            row.metrics.get("subscribersGained", 0.0) for row in rows
        ),
    }
    # View-weighted, not a mean of means: a day with 12 views must not weigh
    # the same as a day with 12,000.
    for metric in ("averageViewDuration", "averageViewPercentage"):
        if total_views > 0:
            summary[metric] = (
                sum(
                    row.metrics.get(metric, 0.0) * row.metrics.get("views", 0.0)
                    for row in rows
                )
                / total_views
            )
        else:
            summary[metric] = 0.0
    return summary


def recent_window(days: int = 28) -> tuple[date, date]:
    """Analytics lags by roughly two days, so the window ends before today."""
    end = date.today() - timedelta(days=2)
    return end - timedelta(days=days), end
