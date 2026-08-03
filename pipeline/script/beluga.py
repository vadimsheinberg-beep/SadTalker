"""Beluga-style script generation with a verified claim welded in.

Two things must both be true of the output and they pull against each other.
The format is fast, funny and irreverent; the claim is a sourced statement from
a corpus that must survive the trip intact. The resolution used here is
structural: the claim gets its own beat, that beat's text is *not* written by
the model, and the model is asked to write the approach to it and the landing
after it. So the joke can be rewritten freely and the claim cannot drift.

Generation loops against the pacing band rather than trusting one shot.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from ..config import SETTINGS, PacingPolicy
from ..contracts import AtlasClaim, Beat, Language, Script, TopicCandidate
from . import pacing
from .claude_client import ClaudeClient

BEAT_ORDER = ("hook", "escalation", "turn", "claim", "payoff", "cta")

SYSTEM = """\
You write narration for short-form video in the "Beluga" style: fast cuts, \
present tense, short declarative sentences, dry escalation, comic timing built \
from rhythm rather than punchlines. The narrator is deadpan and never explains \
the joke.

Hard constraints:
- Every beat is one cut. Keep beats under 28 words; most should be under 15.
- No stage directions inside the narration text. Visuals go in the visual field.
- Never invent a fact about a real company, person, or case. You may describe \
what the source videos are about; you may not add detail they do not contain.
- The claim beat is supplied to you verbatim. Reproduce it exactly, character \
for character. Do not translate, trim, paraphrase, or punctuate it differently.
- Do not attribute the claim to a person, era, or authority beyond what the \
supplied text itself says. It arrives already sourced.
- The turn beat is the pivot from the modern story to the claim. Earn it in one \
line; do not announce that a lesson is coming.
"""

PROMPT = """\
Language: {language}
Topic: {title}
Why it is trending: {evidence}
Keywords: {keywords}

Target runtime: {duration}s. Total word budget: about {budget} words across all \
beats (this is the whole script, including the claim beat).

The claim beat text, to be reproduced exactly:
---
{claim_text}
---

Write the other beats around it. Return JSON only:
{{
  "title": "video title, under 70 characters",
  "beats": [
    {{"role": "hook", "text": "...", "visual": "..."}},
    {{"role": "escalation", "text": "...", "visual": "..."}},
    {{"role": "turn", "text": "...", "visual": "..."}},
    {{"role": "payoff", "text": "...", "visual": "..."}},
    {{"role": "cta", "text": "...", "visual": "..."}}
  ]
}}

Do not emit the claim beat -- it is inserted for you between turn and payoff. \
You may emit several hook or escalation beats if the pacing needs them.
"""

REVISION = """\
The previous draft was {problem}.

{report}

Rewrite it. Keep the same premise, structure, and voice; change only the \
length. Return the same JSON shape. The claim beat is still inserted for you \
and must not appear in your output.

Previous draft:
{previous}
"""


class ScriptGenerationFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class GeneratedScript:
    script: Script
    title: str
    report: pacing.PacingReport
    attempts: int


def build(
    topic: TopicCandidate,
    claim: AtlasClaim,
    language: Language,
    duration_s: int = 60,
    claude: ClaudeClient | None = None,
    policy: PacingPolicy | None = None,
    max_attempts: int = 3,
) -> GeneratedScript:
    """Generate a script and iterate until it lands inside the pacing band."""
    if not claim.verified:
        raise ScriptGenerationFailed(
            f"refusing to build a script around unverified cluster "
            f"{claim.cluster_id}"
        )
    claude = claude or ClaudeClient()
    policy = policy or SETTINGS.pacing
    claim_text = claim.render(language)

    prompt = PROMPT.format(
        language="Russian" if language is Language.RU else "English",
        title=topic.title,
        evidence=_evidence_line(topic),
        keywords=", ".join(topic.keywords),
        duration=duration_s,
        budget=pacing.target_words(duration_s, policy),
        claim_text=claim_text,
    )

    previous = ""
    report: pacing.PacingReport | None = None
    for attempt in range(1, max_attempts + 1):
        if previous and report is not None:
            prompt = REVISION.format(
                problem="too slow" if report.too_slow else "too long",
                report=report.describe(),
                previous=previous,
            )
        raw = claude.complete_json(prompt, system=SYSTEM, temperature=0.8)
        previous = json.dumps(raw, ensure_ascii=False)
        script = assemble(raw, topic, claim, language, claim_text)
        report = pacing.measure(script, duration_s, policy)
        if report.in_band and not report.long_beats:
            return GeneratedScript(
                script=script,
                title=str(raw.get("title", topic.title))[:70],
                report=report,
                attempts=attempt,
            )

    raise ScriptGenerationFailed(
        f"could not reach {policy.min_wpm:.0f}-{policy.max_wpm:.0f} wpm in "
        f"{max_attempts} attempts: {report.describe() if report else 'no draft'}"
    )


def assemble(
    payload: Any,
    topic: TopicCandidate,
    claim: AtlasClaim,
    language: Language,
    claim_text: str,
) -> Script:
    """Turn the model's JSON into a Script, inserting the claim beat ourselves.

    Any claim-role beat the model emitted is discarded rather than merged. The
    verbatim text is the only version that ships.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("beats"), list):
        raise ScriptGenerationFailed(f"malformed generator output: {payload!r}")

    written = [
        Beat(
            role=str(item.get("role", "escalation")).lower(),
            text=" ".join(str(item.get("text", "")).split()),
            visual=str(item.get("visual", "")),
        )
        for item in payload["beats"]
        if str(item.get("text", "")).strip()
    ]
    written = [beat for beat in written if beat.role != "claim"]
    if not written:
        raise ScriptGenerationFailed("generator returned no usable beats")

    claim_beat = Beat(
        role="claim",
        text=claim_text,
        visual=f"source card: {_source_line(claim)}",
    )

    before = [b for b in written if _rank(b.role) < _rank("claim")]
    after = [b for b in written if _rank(b.role) > _rank("claim")]
    if not after:
        # Nothing to land on: without a beat after the claim the video ends on
        # the source, which reads as a sermon rather than a payoff.
        raise ScriptGenerationFailed("generator produced no payoff after the claim")

    return Script(
        topic_slug=topic.slug,
        language=language,
        beats=tuple([*before, claim_beat, *after]),
        claim_cluster_id=claim.cluster_id,
    )


def verify_claim_intact(script: Script, claim: AtlasClaim) -> None:
    """Assert the shipped claim text is byte-identical to the approved text.

    Called by the gate. A paraphrase of a verified claim is not a verified
    claim -- the approval was of specific wording.
    """
    beats = script.beats_of("claim")
    if len(beats) != 1:
        raise ValueError(f"expected exactly one claim beat, found {len(beats)}")
    approved = claim.render(script.language)
    if beats[0].text != approved:
        raise ValueError(
            f"claim beat does not match approved text for cluster "
            f"{claim.cluster_id}"
        )


def _rank(role: str) -> int:
    try:
        return BEAT_ORDER.index(role)
    except ValueError:
        return BEAT_ORDER.index("escalation")


def _evidence_line(topic: TopicCandidate) -> str:
    parts: Sequence[str] = [
        f"'{signal.title}' ({signal.outlier_ratio:.1f}x its channel's median)"
        for signal in topic.evidence[:3]
    ]
    return "; ".join(parts) or "no evidence recorded"


def _source_line(claim: AtlasClaim) -> str:
    return "; ".join(
        f"{source.tractate} {source.folio}" for source in claim.sources
    )
