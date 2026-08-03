"""Data contracts shared by every stage of the pipeline.

One rule governs the whole module: a claim that is not backed by a verified
atlas cluster never becomes part of a package. Everything else is scheduling.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Language(str, Enum):
    RU = "ru"
    EN = "en"


class Channel(str, Enum):
    """Publication targets. TAMHA is the RU channel, Iahalom the EN one."""

    TAMHA = "tamha"
    IAHALOM = "iahalom"

    @property
    def language(self) -> Language:
        return Language.RU if self is Channel.TAMHA else Language.EN


class ClusterStatus(str, Enum):
    """Lifecycle of an atlas cluster.

    Only ``VERIFIED`` clusters may source a claim. ``DRAFT`` is where all 119
    clusters currently sit; ``PROPOSED`` is a machine-suggested theme_name
    awaiting a human; ``REJECTED`` is a theme a human turned down.
    """

    DRAFT = "draft"
    PROPOSED = "proposed"
    VERIFIED = "verified"
    REJECTED = "rejected"


@dataclass(frozen=True)
class VideoSignal:
    """One observed video, normalised into the signals the scout scores on."""

    video_id: str
    channel_id: str
    title: str
    published_at: datetime
    views: int
    duration_s: int
    channel_median_views: int = 0

    @property
    def age_hours(self) -> float:
        now = datetime.now(timezone.utc)
        published = self.published_at
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        return max((now - published).total_seconds() / 3600.0, 1.0)

    @property
    def velocity(self) -> float:
        """Views per hour since publication."""
        return self.views / self.age_hours

    @property
    def outlier_ratio(self) -> float:
        """Views relative to the channel's own median.

        This is the load-bearing signal. A video far above its channel's
        baseline is being pushed by recommendation, not merely watched by
        subscribers -- which is exactly the question the third concept asks.
        """
        if self.channel_median_views <= 0:
            return 1.0
        return self.views / self.channel_median_views


@dataclass(frozen=True)
class TopicCandidate:
    """A subject the scout believes YouTube is currently amplifying."""

    slug: str
    title: str
    keywords: tuple[str, ...]
    evidence: tuple[VideoSignal, ...]
    score: float
    domain: str = "corporate_collapse"
    scored_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def distinct_channels(self) -> int:
        return len({signal.channel_id for signal in self.evidence})

    def query_text(self) -> str:
        """The text handed to the atlas RAG when looking for a matching claim."""
        return " ".join((self.title, *self.keywords))


@dataclass(frozen=True)
class ClaimSource:
    """A citation back into the corpus. Every field here is required."""

    section_id: str
    tractate: str
    folio: str
    quote: str
    url: str = ""


@dataclass(frozen=True)
class AtlasClaim:
    """A single verified thought lifted out of the Bavli atlas.

    ``verified`` is not a hint. :func:`pipeline.publish.gate.check_package`
    refuses any package whose claim is not verified, and the constructor below
    refuses to mark a claim verified without sources.
    """

    cluster_id: str
    theme_name: str
    text: dict[Language, str]
    sources: tuple[ClaimSource, ...]
    verified: bool
    relevance: float = 0.0

    def __post_init__(self) -> None:
        if self.verified and not self.sources:
            raise ValueError(
                f"cluster {self.cluster_id} marked verified with no sources"
            )
        if self.verified and not self.theme_name.strip():
            raise ValueError(
                f"cluster {self.cluster_id} marked verified with empty theme_name"
            )

    def render(self, language: Language) -> str:
        try:
            return self.text[language]
        except KeyError:
            raise ValueError(
                f"cluster {self.cluster_id} has no {language.value} text"
            ) from None


@dataclass(frozen=True)
class Beat:
    """One narration unit. In Beluga style a beat is a single cut."""

    role: str  # hook | escalation | turn | claim | payoff | cta
    text: str
    visual: str = ""

    @property
    def words(self) -> int:
        return len(self.text.split())


@dataclass(frozen=True)
class Script:
    topic_slug: str
    language: Language
    beats: tuple[Beat, ...]
    claim_cluster_id: str
    style: str = "beluga"

    @property
    def words(self) -> int:
        return sum(beat.words for beat in self.beats)

    @property
    def text(self) -> str:
        return "\n".join(beat.text for beat in self.beats)

    def beats_of(self, role: str) -> tuple[Beat, ...]:
        return tuple(beat for beat in self.beats if beat.role == role)


@dataclass(frozen=True)
class Package:
    """Everything needed to publish one video, plus its provenance."""

    topic: TopicCandidate
    claim: AtlasClaim
    script: Script
    channel: Channel
    target_duration_s: int
    assets: dict[str, str] = field(default_factory=dict)

    @property
    def package_id(self) -> str:
        raw = f"{self.channel.value}:{self.topic.slug}:{self.claim.cluster_id}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def to_json(self) -> str:
        return json.dumps(_jsonable(asdict(self)), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "Package":
        """Rebuild a package written by :meth:`to_json`.

        The round trip has to reconstruct a *verified* claim, so it goes through
        the same ``AtlasClaim`` constructor as everything else — a package file
        edited by hand to flip ``verified`` without adding sources fails here
        rather than at publication time.
        """
        payload = json.loads(raw)
        claim = payload["claim"]
        topic = payload["topic"]
        return cls(
            topic=TopicCandidate(
                slug=topic["slug"],
                title=topic["title"],
                keywords=tuple(topic["keywords"]),
                evidence=tuple(
                    VideoSignal(
                        video_id=item["video_id"],
                        channel_id=item["channel_id"],
                        title=item["title"],
                        published_at=datetime.fromisoformat(item["published_at"]),
                        views=item["views"],
                        duration_s=item["duration_s"],
                        channel_median_views=item.get("channel_median_views", 0),
                    )
                    for item in topic.get("evidence", [])
                ),
                score=topic["score"],
                domain=topic.get("domain", "corporate_collapse"),
            ),
            claim=AtlasClaim(
                cluster_id=claim["cluster_id"],
                theme_name=claim["theme_name"],
                text={Language(k): v for k, v in claim["text"].items()},
                sources=tuple(
                    ClaimSource(**source) for source in claim["sources"]
                ),
                verified=claim["verified"],
                relevance=claim.get("relevance", 0.0),
            ),
            script=Script(
                topic_slug=payload["script"]["topic_slug"],
                language=Language(payload["script"]["language"]),
                beats=tuple(Beat(**beat) for beat in payload["script"]["beats"]),
                claim_cluster_id=payload["script"]["claim_cluster_id"],
                style=payload["script"].get("style", "beluga"),
            ),
            channel=Channel(payload["channel"]),
            target_duration_s=payload["target_duration_s"],
            assets=dict(payload.get("assets", {})),
        )


@dataclass(frozen=True)
class Refusal:
    """A reason a package must not ship. Never a warning -- always fatal."""

    code: str
    detail: str


@dataclass(frozen=True)
class GateResult:
    refusals: tuple[Refusal, ...]

    @property
    def ok(self) -> bool:
        return not self.refusals

    def raise_if_blocked(self) -> None:
        if self.refusals:
            lines = "\n".join(f"  [{r.code}] {r.detail}" for r in self.refusals)
            raise PublicationBlocked(f"package blocked by gate:\n{lines}")


class PublicationBlocked(RuntimeError):
    """Raised instead of publishing. Callers must not catch and continue."""


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {_key(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def _key(value: Any) -> str:
    return value.value if isinstance(value, Enum) else str(value)
