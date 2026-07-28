"""HTTP-интерфейс контент-завода.

Вывод по умолчанию — рабочий режим на шесть строк. Полный отчёт из двенадцати
секций отдаётся только по явному запросу /report.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel, select

from . import pipeline
from .backends import get_backend
from .config import policy, settings
from .models import (
    AtlasEntry,
    Claim,
    ClaimStatus,
    GateApproval,
    LedgerEntry,
    Short,
    ShortStage,
    now,
)

engine = create_engine(settings().database_url, pool_pre_ping=True)


def get_session():
    with Session(engine) as session:
        yield session


@asynccontextmanager
async def lifespan(app: FastAPI):
    SQLModel.metadata.create_all(engine)
    yield


app = FastAPI(title="Tzoar Content Factory", version="2.0", lifespan=lifespan)


class ApproveIn(BaseModel):
    approved_by: str
    note: str = ""


class ScriptIn(BaseModel):
    script: str
    claim_ids: list[int]
    quotes: list[dict] = []
    meta: dict = {}


class RetractIn(BaseModel):
    reason: str


@app.get("/health")
def health():
    return {"ok": True, "policy_version": policy()["version"], "gpu": get_backend().name}


@app.get("/status")
def status(session: Session = Depends(get_session)):
    """Рабочий режим: коротко и по делу."""
    active = session.exec(
        select(Short).where(Short.stage.notin_([ShortStage.published])).order_by(Short.updated_at)
    ).all()
    blocked = [s for s in active if s.stage == ShortStage.blocked]
    waiting = [s for s in active if s.needs_human]
    inventory = session.exec(
        select(AtlasEntry).where(AtlasEntry.status == ClaimStatus.verified)
    ).all()
    return {
        "inventory_verified": len(inventory),
        "in_flight": len(active),
        "blocked": len(blocked),
        "awaiting_human": len(waiting),
        "gpu_backend": get_backend().name,
        "next_step": (
            f"разблокировать: {blocked[0].blocker}" if blocked
            else f"выборочный контроль: {len(waiting)} роликов" if waiting
            else "инвентарь пуст — нужен гейт inventory" if not inventory
            else "конвейер идёт"
        ),
    }


@app.get("/report")
def report(session: Session = Depends(get_session)):
    """Полный отчёт — только по явному запросу."""
    ledger = session.exec(select(LedgerEntry)).all()
    shorts = session.exec(select(Short)).all()
    published = [s for s in shorts if s.stage == ShortStage.published]
    blocked = [s for s in shorts if s.stage == ShortStage.blocked]
    return {
        "pipeline": status(session),
        "totals": {
            "shorts_total": len(shorts),
            "published": len(published),
            "blocked": len(blocked),
            "rework_rate": round(len(blocked) / len(shorts), 3) if shorts else 0.0,
            "usd_total": round(sum(l.usd_total for l in ledger), 4),
            "usd_per_published": (
                round(sum(l.usd_total for l in ledger) / len(published), 4) if published else None
            ),
        },
        "thresholds": policy()["thresholds"],
        "blockers": [{"id": s.id, "stage": s.stage, "blocker": s.blocker} for s in blocked[:20]],
    }


# ───────────────────────────── инвентарь ────────────────────────────────


@app.post("/atlas", status_code=201)
def create_atlas_entry(entry: AtlasEntry, session: Session = Depends(get_session)):
    entry.status = ClaimStatus.draft  # утверждение только через гейт
    session.add(entry)
    session.commit()
    session.refresh(entry)
    return entry


@app.post("/atlas/{entry_id}/approve")
def approve_entry(entry_id: int, body: ApproveIn, session: Session = Depends(get_session)):
    """Гейт inventory: человек утверждает запись атласа, а не каждое видео."""
    entry = session.get(AtlasEntry, entry_id)
    if entry is None:
        raise HTTPException(404, "запись атласа не найдена")
    if not entry.experiment_lock:
        raise HTTPException(409, "нет experiment_lock: утверждение невоспроизводимо")

    claims = session.exec(select(Claim).where(Claim.atlas_entry_id == entry_id)).all()
    if not claims:
        raise HTTPException(409, "у записи нет утверждений")

    for c in claims:
        if c.status == ClaimStatus.draft:
            c.status = ClaimStatus.verified
            c.approved_by = body.approved_by
            c.approved_at = now()
            session.add(c)

    entry.status = ClaimStatus.verified
    entry.approved_by = body.approved_by
    entry.approved_at = now()
    session.add(entry)
    session.add(
        GateApproval(
            gate="inventory",
            subject_type="atlas_entry",
            subject_id=entry_id,
            approved_by=body.approved_by,
            note=body.note,
            policy_version=policy()["version"],
        )
    )
    session.commit()
    return {"ok": True, "entry": entry_id, "claims_verified": len(claims)}


@app.post("/claims/{claim_id}/retract")
def retract(claim_id: int, body: RetractIn, session: Session = Depends(get_session)):
    affected = pipeline.retract_claim(session, claim_id, body.reason)
    return {"retracted": claim_id, "affected_shorts": [s.id for s in affected]}


# ───────────────────────────── ролики ───────────────────────────────────


@app.post("/shorts", status_code=201)
def new_short(lang: str, channel: str, session: Session = Depends(get_session)):
    short = pipeline.create_short(session, lang, channel)
    if short is None:
        raise HTTPException(409, "нет доступных записей атласа: пополните инвентарь")
    return short


@app.post("/shorts/{short_id}/script")
def attach_script(short_id: int, body: ScriptIn, session: Session = Depends(get_session)):
    short = session.get(Short, short_id)
    if short is None:
        raise HTTPException(404, "ролик не найден")
    short.script = body.script
    short.claim_ids = body.claim_ids
    short.quotes = body.quotes
    short.qc = {**(short.qc or {}), "meta": body.meta}
    short.stage = ShortStage.scripted
    session.add(short)
    session.commit()
    return pipeline.advance(session, short)


@app.post("/shorts/{short_id}/advance")
def advance_short(short_id: int, session: Session = Depends(get_session)):
    short = session.get(Short, short_id)
    if short is None:
        raise HTTPException(404, "ролик не найден")
    return pipeline.advance(session, short)


@app.post("/shorts/{short_id}/approve")
def approve_short(short_id: int, body: ApproveIn, session: Session = Depends(get_session)):
    short = session.get(Short, short_id)
    if short is None:
        raise HTTPException(404, "ролик не найден")
    if short.stage != ShortStage.awaiting_sample:
        raise HTTPException(409, f"ролик в стадии {short.stage}, утверждение не требуется")
    short.needs_human = False
    short.blocker = "готов к публикации"
    session.add(short)
    session.add(
        GateApproval(
            gate="sample",
            subject_type="short",
            subject_id=short_id,
            approved_by=body.approved_by,
            note=body.note,
            policy_version=policy()["version"],
        )
    )
    session.commit()
    return {"ok": True}
