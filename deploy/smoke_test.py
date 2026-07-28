#!/usr/bin/env python3
"""Проверка, что политика действительно исполняется, а не декларируется.

Каждый тест соответствует запрету из POLICY.yaml. Запуск без GPU и без Postgres:
    docker compose -f docker-compose.control.yml run --rm orchestrator python smoke_test.py
или локально при установленных зависимостях:
    DATABASE_URL=sqlite:///./smoke.db python3 smoke_test.py
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mktemp(suffix='.db')}")

from sqlalchemy import create_engine  # noqa: E402
from sqlmodel import Session, SQLModel  # noqa: E402

from app import pipeline, validators  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import (  # noqa: E402
    AtlasEntry,
    Claim,
    ClaimStatus,
    Short,
    ShortStage,
    SourceSpan,
)

PASS, FAIL = "\033[32mOK\033[0m", "\033[31mПРОВАЛ\033[0m"
results: list[tuple[bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((condition, name))
    print(f"  {PASS if condition else FAIL}  {name}{('  — ' + detail) if detail else ''}")


REAL_QUOTE = "אמר רבי יוחנן"


def seed(session: Session) -> tuple[AtlasEntry, Claim, Claim]:
    session.add(
        SourceSpan(
            corpus_snapshot="snap-1",
            ref="bavli:shabbat:31a:3",
            start=140,
            end=154,
            exact_text=REAL_QUOTE,
        )
    )
    entry = AtlasEntry(
        slug="chazaka-presumption",
        cluster_id=412,
        title_ru="Презумпция и владение",
        title_en="Presumption and possession",
        passport={"silhouette": 0.31, "size": 1873},
        experiment_lock="sha256:deadbeef",
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)

    good = Claim(
        atlas_entry_id=entry.id,
        kind="technical",
        text_ru="Кластер сгруппирован по типу ссылки, а не по теме.",
        status=ClaimStatus.verified,
    )
    halakhic = Claim(
        atlas_entry_id=entry.id,
        kind="halakhic",
        text_ru="Следует поступать так-то.",
        status=ClaimStatus.verified,
    )
    session.add(good)
    session.add(halakhic)
    session.commit()
    session.refresh(good)
    session.refresh(halakhic)
    return entry, good, halakhic


def main() -> int:
    engine = create_engine(settings().database_url)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        entry, good, halakhic = seed(session)

        print("\n1. Инвентарь и кулдаун")
        entry.status = ClaimStatus.verified
        session.add(entry)
        session.commit()
        short = pipeline.create_short(session, "ru", "tzoar-ru")
        check("ролик создаётся только из verified-записи", short is not None)
        check("ракурс зафиксирован, чтобы не повторяться", bool(short.angle))
        second = pipeline.create_short(session, "ru", "tzoar-ru")
        check("кулдаун не даёт взять ту же запись повторно", second is None,
              "21 день по POLICY.thresholds")

        print("\n2. Запрет fabricated_quote")
        v = validators.quote_spans(
            [{"ref": "bavli:shabbat:31a:3", "start": 140, "end": 154, "exact_text": "выдумка"}],
            lambda r, s, e: pipeline.build_context(session, short)["span_lookup"](r, s, e),
        )
        check("искажённая цитата блокируется", not v.ok, v.violations[0] if v.violations else "")
        v = validators.quote_spans(
            [{"ref": "bavli:shabbat:31a:3", "start": 140, "end": 154, "exact_text": REAL_QUOTE}],
            lambda r, s, e: pipeline.build_context(session, short)["span_lookup"](r, s, e),
        )
        check("точный спан проходит", v.ok)

        print("\n3. Запрет unverified_claim_in_volume")
        claims_by_id = {good.id: good}
        check("сценарий без утверждений блокируется",
              not validators.claims_verified([], claims_by_id).ok)
        check("несуществующее утверждение блокируется",
              not validators.claims_verified([9999], claims_by_id).ok)
        check("verified-утверждение проходит",
              validators.claims_verified([good.id], claims_by_id).ok)

        print("\n4. Запрет halakhic_ruling")
        check("галахическое постановление — жёсткий блок",
              not validators.claim_types(["halakhic"]).ok)
        vt = validators.claim_types(["attribution"])
        check("attribution маршрутизируется к эксперту",
              vt.metrics.get("risk") == "high", str(vt.metrics.get("needs_expert")))

        print("\n5. Запрет templated_mass_production (политика YouTube)")
        base = ("Мы ожидали что кластер соберётся вокруг понятия хазака но модель "
                "сгруппировала тексты по типу ссылки а не по смыслу и это меняет вывод")
        near = base.replace("хазака", "мигу")
        far = (
            "Ссылка на лист и сторону выглядит как одно короткое поле. Внутри неё "
            "спрятаны три разных способа адресации, и путать их нельзя. Первый "
            "указывает физический разворот рукописи. Второй отсылает к принятому "
            "печатному изданию, где границы страниц закреплены типографом, а не "
            "автором. Третий работает как указатель внутри уже разобранного "
            "фрагмента и без первых двух вообще не читается. Когда мы отдали такие "
            "поля поисковому индексу, он свёл вместе куски, у которых совпадала "
            "форма записи, но расходился предмет. Формально система отработала "
            "верно: она измеряла именно то, что ей дали. Ошибка была наша — мы "
            "приняли справочный слой за содержательный. Показываю, как выглядит "
            "разбор такой ссылки шаг за шагом, где именно ломается наивное "
            "прочтение и почему проверять это приходится глазами, а не метрикой "
            "качества поиска. Проверьте на своём корпусе."
        )
        vs = validators.template_similarity(near, [base])
        check("почти дублирующий сценарий блокируется", not vs.ok, str(vs.metrics))
        vs2 = validators.template_similarity(far, [base])
        check("содержательно другой сценарий проходит", vs2.ok, str(vs2.metrics))

        print("\n6. Запрет synthetic_persona и undisclosed_synthesis")
        check("сгенерированный ведущий блокируется",
              not validators.presenter_source({"presenter": "ai_avatar"}).ok)
        check("реальный автор с лип-синком проходит",
              validators.presenter_source(
                  {"presenter": "lipsync_on_author_broll", "broll_asset_id": "broll-01"}).ok)
        check("нераскрытый синтез блокируется", not validators.disclosure({}).ok)
        check("раскрытый синтез проходит",
              validators.disclosure({"altered_content_flag": True, "on_screen_badge": True}).ok)

        print("\n7. Бюджет слов вместо таймкодов")
        check("слишком короткий сценарий блокируется",
              not validators.word_budget("слишком мало слов", 60).ok)
        check("сценарий на минуту проходит",
              validators.word_budget(" ".join(["слово"] * 140), 60).ok)

        print("\n8. Полный прогон конвейера")
        short.script = far
        short.claim_ids = [good.id]
        short.quotes = [{"ref": "bavli:shabbat:31a:3", "start": 140, "end": 154,
                         "exact_text": REAL_QUOTE}]
        short.qc = {"meta": {"presenter": "lipsync_on_author_broll", "broll_asset_id": "broll-01",
                             "altered_content_flag": True, "on_screen_badge": True}}
        short.stage = ShortStage.scripted
        session.add(short)
        session.commit()
        short = pipeline.advance(session, short)
        check("корректный ролик проходит проверку политики",
              short.stage == ShortStage.policy_checked, f"стадия {short.stage}")

        short = pipeline.advance(session, short)
        check("без GPU конвейер честно блокируется, а не имитирует рендер",
              short.stage == ShortStage.blocked and "GPU" in (short.blocker or ""),
              short.blocker or "")

        print("\n9. Распространение ретракции")
        affected = pipeline.retract_claim(session, good.id, "источник прочитан неверно")
        check("отзыв утверждения находит все производные ролики", len(affected) == 1)
        check("затронутый ролик уходит человеку", affected[0].needs_human)
        check("утверждение помечено отозванным",
              session.get(Claim, good.id).status == ClaimStatus.retracted)
        check("отозванное утверждение больше не проходит валидацию",
              not validators.claims_verified([good.id],
                                             {good.id: session.get(Claim, good.id)}).ok)

    ok = sum(1 for r, _ in results if r)
    print(f"\n{ok}/{len(results)} проверок пройдено")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
