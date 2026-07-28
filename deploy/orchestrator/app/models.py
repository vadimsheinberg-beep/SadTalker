"""Состояние завода. Единственный источник правды — эти таблицы, не контекст модели."""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON
from sqlmodel import Field, SQLModel

JSONType = JSON().with_variant(JSONB, "postgresql")


def now() -> datetime:
    return datetime.now(timezone.utc)


def as_aware(value: Optional[datetime]) -> Optional[datetime]:
    """Столбцы объявлены без таймзоны, поэтому SQLite и Postgres возвращают
    naive-значения. Приводим к UTC перед любым сравнением."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


class ClaimStatus(str, enum.Enum):
    draft = "draft"
    verified = "verified"
    rejected = "rejected"
    retracted = "retracted"


class ShortStage(str, enum.Enum):
    picked = "picked"
    scripted = "scripted"
    policy_checked = "policy_checked"
    voiced = "voiced"
    rendered = "rendered"
    assembled = "assembled"
    qc_passed = "qc_passed"
    awaiting_sample = "awaiting_sample"
    published = "published"
    blocked = "blocked"


class Track(str, enum.Enum):
    flagship = "flagship"
    volume = "volume"


class AtlasEntry(SQLModel, table=True):
    """Запись публичного атласа. Инвентарь, из которого собирается объёмный контур."""

    __tablename__ = "atlas_entry"

    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True)
    cluster_id: int
    title_ru: str
    title_en: str
    passport: dict = Field(default_factory=dict, sa_column=Column(JSONType))
    experiment_lock: str = ""          # хеш корпуса+модели+индекса+параметров
    status: ClaimStatus = Field(default=ClaimStatus.draft, index=True)
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    angles_used: list = Field(default_factory=list, sa_column=Column(JSONType))
    created_at: datetime = Field(default_factory=now)


class Claim(SQLModel, table=True):
    """Отдельное утверждение. Всё, что произносится в кадре, ссылается сюда."""

    __tablename__ = "claim"

    id: Optional[int] = Field(default=None, primary_key=True)
    atlas_entry_id: int = Field(foreign_key="atlas_entry.id", index=True)
    kind: str                          # ключ из POLICY.claim_types
    text_ru: str
    text_en: str = ""
    evidence: list = Field(default_factory=list, sa_column=Column(JSONType))
    status: ClaimStatus = Field(default=ClaimStatus.draft, index=True)
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    retracted_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=now)


class SourceSpan(SQLModel, table=True):
    """Точная привязка цитаты к снимку корпуса. Основа проверки на выдуманные цитаты."""

    __tablename__ = "source_span"
    __table_args__ = (UniqueConstraint("corpus_snapshot", "ref", "start", "end"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    corpus_snapshot: str = Field(index=True)
    ref: str = Field(index=True)        # напр. bavli:shabbat:31a:3
    start: int
    end: int
    exact_text: str


class Short(SQLModel, table=True):
    """Единица объёмного контура."""

    __tablename__ = "short"

    id: Optional[int] = Field(default=None, primary_key=True)
    atlas_entry_id: int = Field(foreign_key="atlas_entry.id", index=True)
    lang: str = Field(index=True)       # ru | en
    channel: str
    angle: str                          # ракурс подачи, чтобы не повторяться
    stage: ShortStage = Field(default=ShortStage.picked, index=True)
    script: str = ""
    claim_ids: list = Field(default_factory=list, sa_column=Column(JSONType))
    quotes: list = Field(default_factory=list, sa_column=Column(JSONType))
    risk: str = "low"
    qc: dict = Field(default_factory=dict, sa_column=Column(JSONType))
    blocker: Optional[str] = None
    needs_human: bool = False
    published_url: Optional[str] = None
    scheduled_for: Optional[datetime] = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class Issue(SQLModel, table=True):
    """Флагманский выпуск (контур v1)."""

    __tablename__ = "issue"

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)   # 2026-W31
    stage: str = Field(default="research", index=True)
    research_question: str = ""
    thesis: str = ""
    atlas_entry_id: Optional[int] = Field(default=None, foreign_key="atlas_entry.id")
    experiment_lock: str = ""
    null_result: bool = False
    blocker: Optional[str] = None
    next_step: Optional[str] = None
    done_criterion: Optional[str] = None
    created_at: datetime = Field(default_factory=now)


class Artifact(SQLModel, table=True):
    """Любой выходной файл. Адресуется по содержимому."""

    __tablename__ = "artifact"

    id: Optional[int] = Field(default=None, primary_key=True)
    sha256: str = Field(index=True)
    kind: str                            # script | audio | video | subtitle | post | atlas_page
    s3_key: str
    track: Track = Field(default=Track.volume)
    short_id: Optional[int] = Field(default=None, foreign_key="short.id", index=True)
    issue_id: Optional[int] = Field(default=None, foreign_key="issue.id", index=True)
    meta: dict = Field(default_factory=dict, sa_column=Column(JSONType))
    created_at: datetime = Field(default_factory=now)


class ClaimArtifact(SQLModel, table=True):
    """Граф ретракций: какие артефакты опираются на какое утверждение."""

    __tablename__ = "claim_artifact"
    __table_args__ = (UniqueConstraint("claim_id", "artifact_id"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    claim_id: int = Field(foreign_key="claim.id", index=True)
    artifact_id: int = Field(foreign_key="artifact.id", index=True)
    locator: str = ""                    # тайм-код, абзац, поле страницы атласа


class GateApproval(SQLModel, table=True):
    __tablename__ = "gate_approval"

    id: Optional[int] = Field(default=None, primary_key=True)
    gate: str = Field(index=True)
    subject_type: str                    # issue | atlas_entry | short
    subject_id: int
    approved_by: str
    note: str = ""
    policy_version: int = 0
    approved_at: datetime = Field(default_factory=now)


class LedgerEntry(SQLModel, table=True):
    """Журнал стоимости. Через N прогонов даёт собственную таблицу цен."""

    __tablename__ = "ledger_entry"

    id: Optional[int] = Field(default=None, primary_key=True)
    short_id: Optional[int] = Field(default=None, index=True)
    issue_id: Optional[int] = Field(default=None, index=True)
    stage: str
    backend: str
    gpu: str = ""
    price_per_hour: float = 0.0
    queue_s: float = 0.0
    compute_s: float = 0.0
    output_s: float = 0.0
    attempt: int = 1
    usd_total: float = 0.0
    created_at: datetime = Field(default_factory=now)
