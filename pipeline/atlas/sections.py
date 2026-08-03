"""Getting the actual text of a cluster's sections.

The labelling step shows a model the sections a cluster is made of and asks it
to name the shared theme. That only works if the *right* sections arrive.

An earlier version fetched them by using the section ids as a semantic search
query -- ``rag.search(" ".join(section_ids))``. Section ids are opaque strings,
so the search returned unrelated passages, the ``id in cluster`` filter dropped
nearly all of them, and every cluster was skipped with "no section texts". The
labelling tool would have looked like it ran and produced nothing.

So retrieval is by id, explicitly, and a source that cannot answer by id says
so instead of returning something plausible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol, Sequence

from .rag_client import RagClient, RagHit, RagUnavailable
from .theme_proposer import SectionText


class SectionsUnavailable(RuntimeError):
    """The sections for a cluster could not be retrieved.

    Raised rather than returning an empty list: an empty list is
    indistinguishable from "this cluster has no sections", and silently
    skipping is exactly the failure this module exists to prevent.
    """


class SectionSource(Protocol):
    def fetch(self, section_ids: Sequence[str]) -> list[SectionText]: ...


@dataclass
class InlineSectionSource:
    """Sections already carried by the cluster record.

    Preferred when available: the atlas clustering ran over the same table, so
    if its export includes the text there is no reason to ask a search service
    for something we already have.
    """

    texts: dict[str, SectionText]

    @classmethod
    def from_records(cls, records: Iterable[dict]) -> "InlineSectionSource":
        texts: dict[str, SectionText] = {}
        for raw in records:
            section_id = str(
                raw.get("section_id") or raw.get("id") or raw.get("doc_id") or ""
            )
            body = str(raw.get("text") or raw.get("content") or raw.get("chunk") or "")
            if section_id and body:
                texts[section_id] = SectionText(
                    section_id=section_id,
                    text=body,
                    tractate=str(raw.get("tractate") or raw.get("masechet") or ""),
                    folio=str(raw.get("folio") or raw.get("daf") or ""),
                )
        return cls(texts)

    def fetch(self, section_ids: Sequence[str]) -> list[SectionText]:
        found = [self.texts[sid] for sid in section_ids if sid in self.texts]
        if not found:
            raise SectionsUnavailable(
                f"none of {len(section_ids)} section ids are present inline"
            )
        return found


@dataclass
class RagSectionSource:
    """Fetch section text from the RAG service by id.

    The service's documented surface is ``POST /search``, so an id lookup is
    attempted through the payload shapes such services usually accept. If none
    of them work the error names what was tried, because guessing again in
    silence is what caused the original defect.
    """

    client: RagClient
    collection: str = ""

    def fetch(self, section_ids: Sequence[str]) -> list[SectionText]:
        ids = [str(s) for s in section_ids if s]
        if not ids:
            raise SectionsUnavailable("cluster has no section ids")

        wanted = set(ids)
        try:
            hits = self.client.fetch_by_ids(ids, collection=self.collection or None)
        except RagUnavailable as exc:
            raise SectionsUnavailable(
                f"RAG could not return sections by id: {exc}"
            ) from exc

        matched = [hit for hit in hits if hit.section_id in wanted]
        if not matched:
            raise SectionsUnavailable(
                f"RAG returned {len(hits)} rows for {len(ids)} ids but none "
                "matched. The id lookup is not filtering server-side -- do not "
                "fall back to semantic search, fix the lookup."
            )
        missing = wanted - {hit.section_id for hit in matched}
        if missing:
            # Partial is usable, but the caller should know the model is seeing
            # less than the cluster actually contains.
            matched.sort(key=lambda hit: ids.index(hit.section_id))
        return [SectionText.from_hit(hit) for hit in matched]


def resolve(
    section_ids: Sequence[str],
    inline: InlineSectionSource | None,
    rag: RagSectionSource | None,
) -> tuple[list[SectionText], str]:
    """Try inline text first, then the RAG. Returns the texts and their origin."""
    errors: list[str] = []
    if inline is not None:
        try:
            return inline.fetch(section_ids), "inline"
        except SectionsUnavailable as exc:
            errors.append(f"inline: {exc}")
    if rag is not None:
        try:
            return rag.fetch(section_ids), "rag"
        except SectionsUnavailable as exc:
            errors.append(f"rag: {exc}")
    raise SectionsUnavailable("; ".join(errors) or "no section source configured")
