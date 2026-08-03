"""Machine-assisted labelling of atlas clusters.

The blocker is not code, it is that all 119 clusters have an empty
``theme_name``, so nothing is verified and the gate blocks everything. Expert
labelling from scratch is slow; reviewing a well-formed proposal is fast.

So this module drafts. It never approves. Every proposal lands in
``PROPOSED``, and only :func:`pipeline.atlas.inventory.approve` -- driven by a
human -- reaches ``VERIFIED``. The model is also held to the supplied section
texts: it may quote them and it may not introduce a source that was not in
front of it, which is checked mechanically below rather than trusted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from ..contracts import ClusterStatus
from ..script.claude_client import ClaudeClient
from .inventory import Cluster, InventoryStore
from .rag_client import RagHit

SYSTEM = """\
You label clusters from a Babylonian Talmud (Bavli) corpus for an editorial \
atlas. You are drafting for expert review, not publishing.

Rules, in order of importance:
1. Use ONLY the section texts provided. Never introduce a tractate, folio, \
quote, or idea that is not in the supplied material.
2. Every source you cite must reuse a section_id from the supplied material \
verbatim.
3. If the supplied sections do not share a coherent theme, say so via \
"coherent": false and leave theme_name empty. A refusal is a useful answer.
4. Quotes must be exact substrings of the supplied section text.
5. The Russian and English texts must state the same claim. They are \
translations of one thought, not two essays.

The claim text is one or two sentences: a single idea a general audience can \
follow without knowing the corpus. No exhortation, no moralising, no framing \
of the reader's life. State what the source says.
"""

PROMPT = """\
Cluster id: {cluster_id}
Sections ({count}):

{sections}

Return JSON only, with this exact shape:
{{
  "coherent": true,
  "theme_name": "short noun phrase naming the shared theme, 2-6 words",
  "text_ru": "the claim in Russian, 1-2 sentences",
  "text_en": "the claim in English, 1-2 sentences",
  "sources": [
    {{"section_id": "...", "tractate": "...", "folio": "...", "quote": "exact substring"}}
  ],
  "confidence": 0.0,
  "reviewer_note": "what an expert should check first"
}}
"""


class ProposalRejected(RuntimeError):
    """The draft failed a mechanical check and was not written to the store."""


@dataclass(frozen=True)
class SectionText:
    section_id: str
    text: str
    tractate: str = ""
    folio: str = ""

    @classmethod
    def from_hit(cls, hit: RagHit) -> "SectionText":
        return cls(
            section_id=hit.section_id,
            text=hit.text,
            tractate=hit.tractate,
            folio=hit.folio,
        )


@dataclass(frozen=True)
class Proposal:
    cluster_id: str
    coherent: bool
    theme_name: str
    text_ru: str
    text_en: str
    sources: tuple[dict[str, str], ...]
    confidence: float
    reviewer_note: str

    def render(self) -> str:
        """Human-readable form for the approval message."""
        if not self.coherent:
            return (
                f"cluster {self.cluster_id}\n"
                f"NOT COHERENT — {self.reviewer_note}"
            )
        lines = [
            f"cluster {self.cluster_id}  (confidence {self.confidence:.2f})",
            f"theme: {self.theme_name}",
            "",
            f"RU: {self.text_ru}",
            f"EN: {self.text_en}",
            "",
            "sources:",
        ]
        lines += [
            f"  {s['tractate']} {s['folio']} [{s['section_id']}]\n    «{s['quote']}»"
            for s in self.sources
        ]
        lines += ["", f"check first: {self.reviewer_note}"]
        return "\n".join(lines)


def propose(
    cluster: Cluster,
    sections: Sequence[SectionText],
    claude: ClaudeClient | None = None,
) -> Proposal:
    if not sections:
        raise ProposalRejected(f"cluster {cluster.cluster_id} has no section texts")
    claude = claude or ClaudeClient()

    rendered = "\n\n".join(
        f"[{s.section_id}] {s.tractate} {s.folio}\n{s.text}" for s in sections
    )
    payload = claude.complete_json(
        PROMPT.format(
            cluster_id=cluster.cluster_id, count=len(sections), sections=rendered
        ),
        system=SYSTEM,
        temperature=0.2,
    )
    return _validate(cluster, sections, payload)


def _validate(
    cluster: Cluster, sections: Sequence[SectionText], payload: Any
) -> Proposal:
    """Check the draft against the material it was given.

    Hallucinated citations are the one failure that would quietly defeat the
    whole verification chain -- a reviewer reading a plausible quote attached to
    a real section id has no easy way to catch it. So it is checked here.
    """
    if not isinstance(payload, dict):
        raise ProposalRejected(f"expected a JSON object, got {type(payload).__name__}")

    coherent = bool(payload.get("coherent", False))
    if not coherent:
        return Proposal(
            cluster_id=cluster.cluster_id,
            coherent=False,
            theme_name="",
            text_ru="",
            text_en="",
            sources=(),
            confidence=0.0,
            reviewer_note=str(payload.get("reviewer_note", "no theme found")),
        )

    by_id = {section.section_id: section for section in sections}
    validated: list[dict[str, str]] = []
    for raw in payload.get("sources", []):
        section_id = str(raw.get("section_id", ""))
        source = by_id.get(section_id)
        if source is None:
            raise ProposalRejected(
                f"cluster {cluster.cluster_id}: cited section {section_id!r} was "
                "not in the supplied material"
            )
        quote = str(raw.get("quote", "")).strip()
        if not quote or _squash(quote) not in _squash(source.text):
            raise ProposalRejected(
                f"cluster {cluster.cluster_id}: quote for {section_id} is not an "
                f"exact substring of that section"
            )
        validated.append(
            {
                "section_id": section_id,
                "tractate": str(raw.get("tractate") or source.tractate),
                "folio": str(raw.get("folio") or source.folio),
                "quote": quote,
            }
        )

    if not validated:
        raise ProposalRejected(f"cluster {cluster.cluster_id}: no usable sources")

    for field in ("theme_name", "text_ru", "text_en"):
        if not str(payload.get(field, "")).strip():
            raise ProposalRejected(f"cluster {cluster.cluster_id}: {field} is empty")

    return Proposal(
        cluster_id=cluster.cluster_id,
        coherent=True,
        theme_name=str(payload["theme_name"]).strip(),
        text_ru=str(payload["text_ru"]).strip(),
        text_en=str(payload["text_en"]).strip(),
        sources=tuple(validated),
        confidence=float(payload.get("confidence", 0.0)),
        reviewer_note=str(payload.get("reviewer_note", "")),
    )


def stage(store: InventoryStore, proposal: Proposal) -> Cluster:
    """Write a proposal into the store as ``PROPOSED``.

    Never sets ``VERIFIED``: that transition belongs to a human.
    """
    cluster = store.get(proposal.cluster_id)
    if cluster is None:
        raise KeyError(f"unknown cluster: {proposal.cluster_id}")
    if not proposal.coherent:
        cluster.notes = proposal.reviewer_note
        store.save(cluster)
        return cluster
    cluster.theme_name = proposal.theme_name
    cluster.text_ru = proposal.text_ru
    cluster.text_en = proposal.text_en
    cluster.sources = [dict(source) for source in proposal.sources]
    cluster.status = ClusterStatus.PROPOSED
    cluster.notes = proposal.reviewer_note
    store.save(cluster)
    return cluster


def pending(store: InventoryStore) -> list[Cluster]:
    """Clusters still needing machine drafting, worst-first."""
    return [
        cluster
        for cluster in store.all()
        if cluster.status is ClusterStatus.DRAFT
    ]


def awaiting_human(store: InventoryStore) -> list[Cluster]:
    return [
        cluster
        for cluster in store.all()
        if cluster.status is ClusterStatus.PROPOSED
    ]


def _squash(text: str) -> str:
    return " ".join(text.split())


def dump_proposals(proposals: Sequence[Proposal]) -> str:
    return json.dumps(
        [
            {
                "cluster_id": p.cluster_id,
                "coherent": p.coherent,
                "theme_name": p.theme_name,
                "text_ru": p.text_ru,
                "text_en": p.text_en,
                "sources": list(p.sources),
                "confidence": p.confidence,
                "reviewer_note": p.reviewer_note,
            }
            for p in proposals
        ],
        ensure_ascii=False,
        indent=2,
    )
