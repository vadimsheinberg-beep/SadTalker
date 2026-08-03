"""Configuration loading.

Secrets live on the server as 0600 env files under ``/opt/tzoar/deploy``.
Nothing in this repository holds a credential value; this module only knows
the paths and the variable names.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEPLOY_ROOT = Path(os.environ.get("TZOAR_DEPLOY_ROOT", "/opt/tzoar/deploy"))

ENV_FILES: dict[str, Path] = {
    "control": DEPLOY_ROOT / ".env",
    "analytics": DEPLOY_ROOT / ".env.youtube-analytics",
    "ytdata": DEPLOY_ROOT / ".env.ytdata",
    "claude": DEPLOY_ROOT / "secrets" / "claude.env",
    "telegram": DEPLOY_ROOT / "secrets" / "telegram.env",
    "elevenlabs": DEPLOY_ROOT / "secrets" / "elevenlabs.env",
    "rag": Path(os.environ.get("AZ_RAG_ENV", "/opt/az_rag/az_search.env")),
}


class MissingSecret(RuntimeError):
    """A required variable was absent from both the environment and its file."""


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a dotenv file. Blank lines and ``#`` comments are skipped.

    Values may be wrapped in single or double quotes; ``export`` prefixes are
    tolerated because the deployed files mix both conventions.
    """
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


class Secrets:
    """Lazy, cached view over the deployed env files.

    Process environment wins over file contents, so a container can override a
    single variable without rewriting a 0600 file.
    """

    def __init__(self, files: dict[str, Path] | None = None) -> None:
        self._files = files if files is not None else ENV_FILES
        self._cache: dict[str, dict[str, str]] = {}

    def _bundle(self, name: str) -> dict[str, str]:
        if name not in self._cache:
            path = self._files.get(name)
            if path is None:
                raise KeyError(f"unknown env bundle: {name}")
            self._cache[name] = parse_env_file(path)
        return self._cache[name]

    def get(self, bundle: str, key: str, default: str | None = None) -> str | None:
        if key in os.environ:
            return os.environ[key]
        return self._bundle(bundle).get(key, default)

    def require(self, bundle: str, key: str) -> str:
        value = self.get(bundle, key)
        if not value:
            path = self._files.get(bundle)
            raise MissingSecret(
                f"{key} not set in environment or {path}. "
                "Write it on the server with mode 0600; never paste it into chat."
            )
        return value

    def all_matching(self, bundle: str, prefix: str) -> list[str]:
        """Every value whose key starts with ``prefix``, ordered by key.

        Used for the rotating Data API keys (``YT_DATA_API_KEY_1``, ``_2``).
        """
        merged = dict(self._bundle(bundle))
        merged.update({k: v for k, v in os.environ.items() if k.startswith(prefix)})
        return [merged[k] for k in sorted(merged) if k.startswith(prefix) and merged[k]]


@dataclass(frozen=True)
class PacingPolicy:
    """Speech-rate band the narration must land inside.

    The deployed corpus sits at ~104 wpm across 117 of 119 scripts, which reads
    as sluggish for the Beluga format. These are the corrected bounds.
    """

    min_wpm: float = 130.0
    max_wpm: float = 155.0

    @property
    def target_wpm(self) -> float:
        return (self.min_wpm + self.max_wpm) / 2


@dataclass(frozen=True)
class ScoutPolicy:
    """Weights and thresholds for trend scoring.

    ``min_distinct_channels`` is the anti-fluke rule: a topic carried by a
    single channel is that channel's news, not a platform trend.
    """

    domain: str = "corporate_collapse"
    lookback_hours: int = 72
    min_distinct_channels: int = 2
    min_views: int = 5_000
    max_candidates: int = 20
    weight_outlier: float = 0.5
    weight_velocity: float = 0.3
    weight_spread: float = 0.2


@dataclass(frozen=True)
class AtlasPolicy:
    """How a Bavli claim is chosen and when the pipeline refuses to choose."""

    rag_url: str = "http://127.0.0.1:8010/search"
    collection: str = "bavli_qwen3_4b_sections"
    top_k: int = 24
    min_relevance: float = 0.55
    require_verified: bool = True


@dataclass(frozen=True)
class DailyPolicy:
    """Thresholds for the daily digest.

    These decide what counts as worth a human's attention, so they are tuning
    knobs rather than constants -- if the digest is too noisy to read every
    morning, it stops being read, and then nothing downstream matters.
    """

    spike_ratio: float = 3.0  # video views over its channel's own baseline
    subscriber_jump_pct: float = 5.0
    quiet_days: int = 21
    digest_topics: int = 12
    digest_videos: int = 25
    fresh_topic_threshold: float = 0.34  # share of evidence published today
    keep_snapshots: int = 120


@dataclass(frozen=True)
class RenderPolicy:
    fps: int = 25
    width: int = 1920
    height: int = 1080
    elevenlabs_model: str = "eleven_multilingual_v2"


@dataclass(frozen=True)
class Settings:
    pacing: PacingPolicy = field(default_factory=PacingPolicy)
    scout: ScoutPolicy = field(default_factory=ScoutPolicy)
    atlas: AtlasPolicy = field(default_factory=AtlasPolicy)
    daily: DailyPolicy = field(default_factory=DailyPolicy)
    render: RenderPolicy = field(default_factory=RenderPolicy)
    workdir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("TZOAR_WORKDIR", "/opt/tzoar/var/pipeline")
        )
    )


SETTINGS = Settings()
SECRETS = Secrets()
