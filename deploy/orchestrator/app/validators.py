"""Точки принуждения для POLICY.yaml.

Каждый запрет в политике ссылается сюда через поле enforced_by. Запрет без
исполнителя не существует — если проверки нет, публикация не блокируется.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .config import policy


@dataclass
class Verdict:
    ok: bool = True
    violations: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def fail(self, rule: str) -> None:
        self.ok = False
        self.violations.append(rule)

    def merge(self, other: "Verdict") -> "Verdict":
        self.ok = self.ok and other.ok
        self.violations.extend(other.violations)
        self.metrics.update(other.metrics)
        return self


_WORD = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _ngrams(tokens: Sequence[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


def _cosine(a: str, b: str) -> float:
    ca, cb = Counter(_tokens(a)), Counter(_tokens(b))
    if not ca or not cb:
        return 0.0
    common = set(ca) & set(cb)
    num = sum(ca[t] * cb[t] for t in common)
    na = math.sqrt(sum(v * v for v in ca.values()))
    nb = math.sqrt(sum(v * v for v in cb.values()))
    return num / (na * nb) if na and nb else 0.0


# ─────────────────────────────── проверки ────────────────────────────────


def quote_spans(quotes: Iterable[dict], lookup) -> Verdict:
    """Каждая цитата обязана дословно совпасть со спаном в снимке корпуса.

    lookup(ref, start, end) -> str | None — точный текст из снимка.
    Несовпадение означает выдуманную или искажённую цитату: жёсткий блок.
    """
    v = Verdict()
    for q in quotes:
        actual = lookup(q.get("ref"), q.get("start"), q.get("end"))
        if actual is None:
            v.fail(f"fabricated_quote: спан не найден в корпусе — {q.get('ref')}")
        elif actual.strip() != str(q.get("exact_text", "")).strip():
            v.fail(f"fabricated_quote: текст не совпадает со спаном — {q.get('ref')}")
    return v


def claims_verified(claim_ids: Sequence[int], claims_by_id: dict) -> Verdict:
    """Объёмный контур не может создавать утверждения — только переупаковывать."""
    v = Verdict()
    if not claim_ids:
        v.fail("unverified_claim_in_volume: сценарий не ссылается ни на одно утверждение")
    for cid in claim_ids:
        claim = claims_by_id.get(cid)
        if claim is None:
            v.fail(f"unverified_claim_in_volume: утверждение {cid} отсутствует")
        elif getattr(claim.status, "value", claim.status) == "retracted":
            v.fail(f"retracted_claim: утверждение {cid} отозвано")
        elif getattr(claim.status, "value", claim.status) != "verified":
            v.fail(f"unverified_claim_in_volume: утверждение {cid} не прошло гейт")
    return v


def claim_types(kinds: Iterable[str]) -> Verdict:
    """Галахические постановления запрещены как формат. Остальное маршрутизируется."""
    v = Verdict()
    spec = policy()["claim_types"]
    needs_expert = []
    for kind in kinds:
        rule = spec.get(kind)
        if rule is None:
            v.fail(f"unknown_claim_type: {kind}")
            continue
        if rule.get("author") == "forbidden":
            v.fail(f"halakhic_ruling: тип утверждения {kind} запрещён")
        if rule.get("review") == "expert":
            needs_expert.append(kind)
    v.metrics["needs_expert"] = needs_expert
    v.metrics["risk"] = "high" if needs_expert else "low"
    return v


def template_similarity(script: str, previous: Sequence[str]) -> Verdict:
    """Исполнение политики YouTube об inauthentic content.

    Шаблонные сценарии с незначительными подстановками — основание для
    демонетизации. Проверяем до рендера, а не после публикации.
    """
    v = Verdict()
    th = policy()["thresholds"]
    if not previous:
        v.metrics.update(max_cosine=0.0, max_shared_5gram=0.0)
        return v

    toks = _tokens(script)
    grams = _ngrams(toks, 5)

    max_cos = max(_cosine(script, p) for p in previous)
    max_share = 0.0
    for p in previous:
        pg = _ngrams(_tokens(p), 5)
        if grams:
            max_share = max(max_share, len(grams & pg) / len(grams))

    v.metrics.update(max_cosine=round(max_cos, 3), max_shared_5gram=round(max_share, 3))
    if max_cos > th["template_similarity_cosine_max"]:
        v.fail(f"templated_mass_production: косинус {max_cos:.3f} выше порога")
    if max_share > th["template_shared_5gram_max"]:
        v.fail(f"templated_mass_production: общих 5-грамм {max_share:.3f} выше порога")
    return v


def word_budget(script: str, target_sec: float) -> Verdict:
    """Модель не управляет секундами, но управляет словами."""
    v = Verdict()
    lo, hi = policy()["thresholds"]["script_words_per_minute"]
    words = len(_tokens(script))
    v.metrics["words"] = words
    v.metrics["est_sec"] = round(words / ((lo + hi) / 2) * 60, 1)
    if words < lo * target_sec / 60 * 0.8:
        v.fail(f"script_too_short: {words} слов на {target_sec:.0f} с")
    if words > hi * target_sec / 60 * 1.2:
        v.fail(f"script_too_long: {words} слов на {target_sec:.0f} с")
    return v


def disclosure(meta: dict) -> Verdict:
    """Скрытый синтез — прямой путь к страйку. Проверяем оба канала раскрытия."""
    v = Verdict()
    d = policy()["disclosure"]
    if d["youtube_altered_content_flag"] and not meta.get("altered_content_flag"):
        v.fail("undisclosed_synthesis: не выставлен флаг altered content")
    if d["on_screen_badge_required"] and not meta.get("on_screen_badge"):
        v.fail("undisclosed_synthesis: отсутствует экранная пометка")
    return v


def presenter_source(meta: dict) -> Verdict:
    """Лип-синк по B-roll автора — синтетически изменённое выступление реального
    человека, а не «настоящая запись». Разрешён при трёх условиях сразу:
    согласие на использование видео, утверждение конкретного текста, раскрытие.
    Технической лазейки здесь нет и искать её не следует."""
    v = Verdict()
    rules = policy()["lip_sync_author_broll"]

    if meta.get("presenter") != "lipsync_on_author_broll":
        v.fail("synthetic_persona: ведущий не является реальным автором канала")
        return v

    if not meta.get("broll_asset_id"):
        v.fail("synthetic_persona: не указан исходный B-roll автора")
    if rules["requires_author_video_consent"] and not meta.get("author_video_consent"):
        v.fail("lip_sync_author_broll: нет согласия автора на использование B-roll")
    if rules["requires_author_text_approval"] and not meta.get("author_text_approval"):
        v.fail("lip_sync_author_broll: автор не утвердил произносимый текст")
    return v


def limitations_present(payload: dict) -> Verdict:
    """Ошибки модели скрывать запрещено — поля обязаны быть непустыми."""
    v = Verdict()
    for key in ("limitations", "what_failed"):
        if not str(payload.get(key, "")).strip():
            v.fail(f"hidden_model_error: поле {key} пустое")
    return v


def localization_divergence(ru: str, en: str) -> Verdict:
    """EN — адаптация, а не дословный перевод: структура крючка должна расходиться."""
    v = Verdict()
    ratio = len(_tokens(en)) / max(len(_tokens(ru)), 1)
    v.metrics["len_ratio_en_ru"] = round(ratio, 2)
    if not (0.7 <= ratio <= 1.45):
        v.fail(f"literal_translation: подозрительное соотношение длин {ratio:.2f}")
    return v


def qc_video(qc: dict) -> Verdict:
    v = Verdict()
    th = policy()["thresholds"]
    if qc.get("lipsync_confidence", 0) < th["lipsync_confidence_min"]:
        v.fail("qc: синхронизация ниже порога")
    if abs(qc.get("lufs", -99) - th["loudness_lufs"]) > th["loudness_tolerance"]:
        v.fail("qc: громкость вне допуска")
    return v


def run_all(short_payload: dict, ctx: dict) -> Verdict:
    """Полный прогон перед публикацией ролика объёмного контура."""
    v = Verdict()
    v.merge(claims_verified(short_payload.get("claim_ids", []), ctx["claims_by_id"]))
    v.merge(quote_spans(short_payload.get("quotes", []), ctx["span_lookup"]))
    v.merge(claim_types(ctx.get("claim_kinds", [])))
    v.merge(template_similarity(short_payload.get("script", ""), ctx.get("previous_scripts", [])))
    v.merge(word_budget(short_payload.get("script", ""), ctx.get("target_sec", 60)))
    v.merge(presenter_source(short_payload.get("meta", {})))
    v.merge(disclosure(short_payload.get("meta", {})))
    if short_payload.get("qc"):
        v.merge(qc_video(short_payload["qc"]))
    return v
