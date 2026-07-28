"""Стадийная машина объёмного контура.

Инвариант: ни одна стадия не создаёт утверждений. Всё, что произносится,
приходит из записи атласа со статусом verified.
"""
from __future__ import annotations

import random
from datetime import timedelta
from typing import Optional

from sqlmodel import Session, select

from .backends import RenderRequest, cost_usd, get_backend
from .config import policy
from .models import (
    AtlasEntry,
    Claim,
    ClaimStatus,
    LedgerEntry,
    Short,
    ShortStage,
    SourceSpan,
    as_aware,
    now,
)
from .validators import Verdict, run_all

ANGLES = ["термин", "пример", "метод", "ошибка модели", "как читать ссылку", "контрпример"]

ORDER = [
    ShortStage.picked,
    ShortStage.scripted,
    ShortStage.policy_checked,
    ShortStage.voiced,
    ShortStage.rendered,
    ShortStage.assembled,
    ShortStage.qc_passed,
    ShortStage.awaiting_sample,
    ShortStage.published,
]


# ─────────────────────────── выбор инвентаря ────────────────────────────


def pick_entry(session: Session, lang: str) -> Optional[AtlasEntry]:
    """Только утверждённые записи, вне кулдауна, с неиспользованным ракурсом."""
    cooldown = policy()["thresholds"]["atlas_entry_cooldown_days"]
    cutoff = now() - timedelta(days=cooldown)

    candidates = session.exec(
        select(AtlasEntry).where(AtlasEntry.status == ClaimStatus.verified)
    ).all()

    fresh = [
        e
        for e in candidates
        if (as_aware(e.last_used_at) is None or as_aware(e.last_used_at) < cutoff)
        and len(e.angles_used or []) < len(ANGLES)
    ]
    if not fresh:
        return None
    fresh.sort(
        key=lambda e: (
            len(e.angles_used or []),
            as_aware(e.last_used_at) or as_aware(e.created_at),
        )
    )
    return fresh[0]


def next_angle(entry: AtlasEntry) -> str:
    used = set(entry.angles_used or [])
    free = [a for a in ANGLES if a not in used]
    return random.choice(free) if free else ANGLES[0]


def create_short(session: Session, lang: str, channel: str) -> Optional[Short]:
    entry = pick_entry(session, lang)
    if entry is None:
        return None
    angle = next_angle(entry)
    short = Short(
        atlas_entry_id=entry.id,
        lang=lang,
        channel=channel,
        angle=angle,
        stage=ShortStage.picked,
    )
    entry.angles_used = list(entry.angles_used or []) + [angle]
    entry.last_used_at = now()
    session.add(entry)
    session.add(short)
    session.commit()
    session.refresh(short)
    return short


# ─────────────────────────── контекст проверок ──────────────────────────


def build_context(session: Session, short: Short) -> dict:
    claims = session.exec(
        select(Claim).where(Claim.atlas_entry_id == short.atlas_entry_id)
    ).all()
    claims_by_id = {c.id: c for c in claims}

    previous = session.exec(
        select(Short)
        .where(Short.channel == short.channel, Short.stage == ShortStage.published)
        .order_by(Short.created_at.desc())
        .limit(50)
    ).all()

    def span_lookup(ref, start, end):
        row = session.exec(
            select(SourceSpan).where(
                SourceSpan.ref == ref, SourceSpan.start == start, SourceSpan.end == end
            )
        ).first()
        return row.exact_text if row else None

    return {
        "claims_by_id": claims_by_id,
        "claim_kinds": [claims_by_id[c].kind for c in short.claim_ids if c in claims_by_id],
        "previous_scripts": [p.script for p in previous if p.script],
        "span_lookup": span_lookup,
        "target_sec": 60,
    }


def check(session: Session, short: Short) -> Verdict:
    ctx = build_context(session, short)
    payload = {
        "script": short.script,
        "claim_ids": short.claim_ids,
        "quotes": short.quotes,
        "meta": (short.qc or {}).get("meta", {}),
        "qc": (short.qc or {}).get("video"),
    }
    return run_all(payload, ctx)


# ─────────────────────────── продвижение по стадиям ─────────────────────


def advance(session: Session, short: Short) -> Short:
    """Один шаг вперёд. Никогда не перепрыгивает через проверку."""
    backend = get_backend()

    if short.stage == ShortStage.picked:
        short.blocker = "нужен сценарий: вызов Script-агента"
        short.stage = ShortStage.scripted if short.script else short.stage

    elif short.stage == ShortStage.scripted:
        verdict = check(session, short)
        short.qc = {**(short.qc or {}), "policy": verdict.metrics}
        if verdict.ok:
            short.stage = ShortStage.policy_checked
            short.blocker = None
        else:
            short.stage = ShortStage.blocked
            short.blocker = "; ".join(verdict.violations)

    elif short.stage in (ShortStage.policy_checked, ShortStage.voiced, ShortStage.rendered):
        kind = {
            ShortStage.policy_checked: "tts",
            ShortStage.voiced: "lipsync",
            ShortStage.rendered: "post",
        }[short.stage]
        result = backend.run(RenderRequest(kind=kind, payload={"short_id": short.id}))
        session.add(
            LedgerEntry(
                short_id=short.id,
                stage=kind,
                backend=backend.name,
                price_per_hour=backend.price_per_hour(),
                compute_s=result.compute_s,
                queue_s=result.queue_s,
                output_s=60.0,
                usd_total=cost_usd(backend.price_per_hour(), result.compute_s, result.queue_s),
            )
        )
        if result.ok:
            short.stage = ORDER[ORDER.index(short.stage) + 1]
            short.blocker = None
        else:
            short.stage = ShortStage.blocked
            short.blocker = result.error

    elif short.stage == ShortStage.assembled:
        verdict = check(session, short)
        if verdict.ok:
            short.stage = ShortStage.qc_passed
        else:
            short.stage = ShortStage.blocked
            short.blocker = "; ".join(verdict.violations)

    elif short.stage == ShortStage.qc_passed:
        short.needs_human = needs_sample_review(session, short)
        short.stage = ShortStage.awaiting_sample if short.needs_human else short.stage
        if not short.needs_human:
            short.blocker = "готов к публикации"

    short.updated_at = now()
    session.add(short)
    session.commit()
    session.refresh(short)
    return short


def needs_sample_review(session: Session, short: Short) -> bool:
    """Выборочный контроль вместо гейта на каждый ролик.

    100% первых N роликов канала, далее заданная доля + всё, где риск не низкий.
    """
    cfg = policy()["tracks"]["volume"]
    published = session.exec(
        select(Short).where(Short.channel == short.channel, Short.stage == ShortStage.published)
    ).all()
    if len(published) < cfg["sample_review_first_n"]:
        return True
    if short.risk != "low":
        return True
    return random.random() < cfg["sample_review_rate"]


# ─────────────────────────── ретракция ──────────────────────────────────


def retract_claim(session: Session, claim_id: int, reason: str) -> list[Short]:
    """Отзыв утверждения обязан догнать все производные артефакты."""
    claim = session.get(Claim, claim_id)
    if claim is None:
        return []
    claim.status = ClaimStatus.retracted
    claim.retracted_reason = reason
    session.add(claim)

    affected = [
        s
        for s in session.exec(select(Short).where(Short.atlas_entry_id == claim.atlas_entry_id)).all()
        if claim_id in (s.claim_ids or [])
    ]
    for s in affected:
        s.blocker = f"ретракция утверждения {claim_id}: {reason}"
        s.needs_human = True
        session.add(s)
    session.commit()
    return affected
