"""The correctness gate. Nothing reaches a channel without passing it.

Every check returns a :class:`Refusal` rather than a warning, and
:meth:`GateResult.raise_if_blocked` raises instead of returning a value the
caller might ignore. That asymmetry is intentional: the cost of shipping an
unsourced claim on a channel that trades on being checkable is much higher than
the cost of dropping a video.

The control plane already runs nine verified tools of its own (link gate,
package validator, queue guard, block-list, Signal markup, channel registry,
topic score, storyboard, analytics). This module does not reimplement them --
:func:`check_package` runs the checks that need the in-process objects, and
``external`` lets the deployed validators veto on top.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from ..config import SETTINGS, PacingPolicy
from ..contracts import (
    Channel,
    GateResult,
    Language,
    Package,
    Refusal,
)
from ..script import pacing
from ..script.beluga import verify_claim_intact

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

# Only these hosts may appear in a description. Everything else is a refusal,
# not a strip -- a link nobody intended is a signal that the description was
# assembled from something unreviewed.
ALLOWED_LINK_HOSTS: frozenset[str] = frozenset(
    {"alhatorah.org", "www.alhatorah.org", "sefaria.org", "www.sefaria.org"}
)

ExternalCheck = Callable[[Package], Sequence[Refusal]]


@dataclass(frozen=True)
class BlockList:
    """Terms that must not appear in narration or description."""

    terms: frozenset[str] = frozenset()

    @classmethod
    def from_lines(cls, lines: Iterable[str]) -> "BlockList":
        return cls(
            frozenset(
                line.strip().casefold()
                for line in lines
                if line.strip() and not line.strip().startswith("#")
            )
        )

    def hits(self, text: str) -> list[str]:
        folded = text.casefold()
        return sorted(term for term in self.terms if term in folded)


def check_package(
    package: Package,
    description: str = "",
    blocklist: BlockList | None = None,
    policy: PacingPolicy | None = None,
    external: Sequence[ExternalCheck] = (),
) -> GateResult:
    refusals: list[Refusal] = []
    refusals += _check_claim(package)
    refusals += _check_language(package)
    refusals += _check_pacing(package, policy)
    refusals += _check_links(description)
    refusals += _check_blocklist(package, description, blocklist)
    refusals += _check_assets(package)
    for check in external:
        refusals.extend(check(package))
    return GateResult(refusals=tuple(refusals))


def _check_claim(package: Package) -> list[Refusal]:
    """The load-bearing check: verified, sourced, and unaltered."""
    claim = package.claim
    refusals: list[Refusal] = []

    if not claim.verified:
        refusals.append(
            Refusal(
                "claim_unverified",
                f"cluster {claim.cluster_id} is not verified. A human must "
                "approve it in the inventory before it can be published.",
            )
        )
    if not claim.theme_name.strip():
        refusals.append(
            Refusal("claim_unnamed", f"cluster {claim.cluster_id} has no theme_name")
        )
    if not claim.sources:
        refusals.append(
            Refusal("claim_unsourced", f"cluster {claim.cluster_id} cites no sources")
        )
    for index, source in enumerate(claim.sources):
        missing = [
            name
            for name in ("section_id", "tractate", "folio", "quote")
            if not getattr(source, name, "").strip()
        ]
        if missing:
            refusals.append(
                Refusal(
                    "source_incomplete",
                    f"cluster {claim.cluster_id} source[{index}] missing "
                    f"{', '.join(missing)}",
                )
            )

    try:
        verify_claim_intact(package.script, claim)
    except ValueError as exc:
        refusals.append(Refusal("claim_altered", str(exc)))
    return refusals


def _check_language(package: Package) -> list[Refusal]:
    """The RU script must go to TAMHA and the EN script to Iahalom."""
    expected: Language = package.channel.language
    if package.script.language is not expected:
        return [
            Refusal(
                "language_mismatch",
                f"{package.channel.value} expects {expected.value}, script is "
                f"{package.script.language.value}",
            )
        ]
    if not package.claim.text.get(expected, "").strip():
        return [
            Refusal(
                "claim_language_missing",
                f"cluster {package.claim.cluster_id} has no {expected.value} text",
            )
        ]
    return []


def _check_pacing(package: Package, policy: PacingPolicy | None) -> list[Refusal]:
    policy = policy or SETTINGS.pacing
    report = pacing.measure(package.script, package.target_duration_s, policy)
    refusals: list[Refusal] = []
    if not report.in_band:
        refusals.append(Refusal("pacing_out_of_band", report.describe()))
    if report.long_beats:
        refusals.append(
            Refusal(
                "beat_too_long",
                f"beats {list(report.long_beats)} exceed "
                f"{pacing.MAX_BEAT_WORDS} words",
            )
        )
    return refusals


def _check_links(description: str) -> list[Refusal]:
    refusals: list[Refusal] = []
    for url in URL_RE.findall(description or ""):
        host = url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0].casefold()
        if host not in ALLOWED_LINK_HOSTS:
            refusals.append(
                Refusal("link_not_allowed", f"description links to {host} ({url})")
            )
    return refusals


def _check_blocklist(
    package: Package, description: str, blocklist: BlockList | None
) -> list[Refusal]:
    if not blocklist or not blocklist.terms:
        return []
    text = f"{package.script.text}\n{description}"
    return [
        Refusal("blocked_term", f"blocked term present: {term!r}")
        for term in blocklist.hits(text)
    ]


def _check_assets(package: Package) -> list[Refusal]:
    missing = [
        name for name in ("video", "audio") if not package.assets.get(name)
    ]
    return [
        Refusal("asset_missing", f"package has no {name} asset") for name in missing
    ]


def build_description(package: Package) -> str:
    """Description with the sources spelled out.

    Generated rather than written, so the citations in the description cannot
    drift from the citations the gate verified.
    """
    claim = package.claim
    is_ru = package.channel is Channel.TAMHA
    header = "Источники:" if is_ru else "Sources:"
    lines = [
        package.topic.title,
        "",
        claim.render(package.channel.language),
        "",
        header,
    ]
    for source in claim.sources:
        entry = f"— {source.tractate} {source.folio}"
        if source.url:
            entry += f" {source.url}"
        lines.append(entry)
    lines += [
        "",
        (
            f"Тема атласа: {claim.theme_name}"
            if is_ru
            else f"Atlas theme: {claim.theme_name}"
        ),
    ]
    return "\n".join(lines)
