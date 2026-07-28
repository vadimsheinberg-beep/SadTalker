"""Воркер: тик планировщика.

Не пытается «догнать план» любой ценой — если инвентарь пуст, он останавливается
и показывает блокер, а не производит шаблонные ролики.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel, select

from . import pipeline
from .config import policy, settings
from .models import AtlasEntry, ClaimStatus, Short, ShortStage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker")

TICK_SECONDS = 60
CHANNELS = {"ru": "tzoar-ru", "en": "tzoar-en"}


def tick(session: Session) -> None:
    cfg = policy()["tracks"]["volume"]
    per_day = cfg["per_day_per_channel"]

    for lang, channel in CHANNELS.items():
        in_flight = session.exec(
            select(Short).where(
                Short.channel == channel,
                Short.stage.notin_([ShortStage.published, ShortStage.blocked]),
            )
        ).all()
        if len(in_flight) >= per_day:
            continue

        inventory = session.exec(
            select(AtlasEntry).where(AtlasEntry.status == ClaimStatus.verified)
        ).all()
        if not inventory:
            log.warning("[%s] инвентарь пуст: нужен гейт inventory, ролики не создаются", channel)
            continue

        short = pipeline.create_short(session, lang, channel)
        if short is None:
            log.warning("[%s] все записи в кулдауне или ракурсы исчерпаны", channel)
        else:
            log.info("[%s] создан ролик %s, ракурс %s", channel, short.id, short.angle)

    active = session.exec(
        select(Short).where(
            Short.stage.notin_([ShortStage.published, ShortStage.blocked]),
            Short.needs_human == False,  # noqa: E712
        )
    ).all()
    for short in active:
        before = short.stage
        pipeline.advance(session, short)
        if short.stage != before:
            log.info("ролик %s: %s -> %s", short.id, before, short.stage)
        if short.stage == ShortStage.blocked:
            log.warning("ролик %s заблокирован: %s", short.id, short.blocker)


def main() -> None:
    engine = create_engine(settings().database_url, pool_pre_ping=True)
    SQLModel.metadata.create_all(engine)
    log.info("воркер запущен, бэкенд GPU: %s", settings().gpu_backend)
    while True:
        try:
            with Session(engine) as session:
                tick(session)
        except Exception:  # noqa: BLE001 — воркер не должен падать на одной задаче
            log.exception("ошибка тика")
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
