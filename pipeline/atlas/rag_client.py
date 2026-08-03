"""Client for the AlHaTorah RAG service.

The service is not ours -- it is reached at ``POST 127.0.0.1:8010/search`` with
a bearer token from ``/opt/az_rag/az_search.env``. What makes it usable here is
that its ``bavli_qwen3_4b_sections`` table is the same table the atlas
clustering ran over, so a hit's ``id`` is directly a section id belonging to a
known cluster. No fuzzy re-matching is needed or wanted.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Sequence

from ..config import SECRETS, SETTINGS, AtlasPolicy, Secrets


class RagUnavailable(RuntimeError):
    """The RAG service could not be reached or returned an unusable payload."""


@dataclass(frozen=True)
class RagHit:
    section_id: str
    score: float
    text: str
    tractate: str = ""
    folio: str = ""
    url: str = ""

    @classmethod
    def from_payload(cls, item: dict[str, Any]) -> "RagHit":
        metadata = item.get("metadata") or {}

        def pick(*names: str, default: str = "") -> str:
            for name in names:
                for source in (item, metadata):
                    value = source.get(name)
                    if value not in (None, ""):
                        return str(value)
            return default

        section_id = pick("id", "section_id", "doc_id")
        if not section_id:
            raise RagUnavailable(f"RAG hit without an id: {item!r}")
        return cls(
            section_id=section_id,
            score=float(item.get("score") or item.get("similarity") or 0.0),
            text=pick("text", "content", "chunk"),
            tractate=pick("tractate", "masechet", "book"),
            folio=pick("folio", "daf", "page"),
            url=pick("url", "link", "source_url"),
        )


class RagClient:
    def __init__(
        self,
        policy: AtlasPolicy | None = None,
        secrets: Secrets | None = None,
        timeout: int = 30,
    ) -> None:
        self._policy = policy or SETTINGS.atlas
        self._secrets = secrets or SECRETS
        self._timeout = timeout

    def _token(self) -> str:
        return self._secrets.require("rag", "AZ_SEARCH_API_KEY")

    def search(
        self,
        query: str,
        top_k: int | None = None,
        collections: Sequence[str] | None = None,
    ) -> list[RagHit]:
        body = json.dumps(
            {
                "query": query,
                "top_k": top_k or self._policy.top_k,
                "collections": list(collections or (self._policy.collection,)),
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self._policy.rag_url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RagUnavailable(f"RAG search failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RagUnavailable(f"RAG returned non-JSON: {exc}") from exc

        return self._hits(payload)

    def _hits(self, payload: dict[str, Any]) -> list[RagHit]:
        items = payload.get("results") or payload.get("hits") or payload.get("data")
        if items is None:
            raise RagUnavailable(f"unexpected RAG response shape: {sorted(payload)}")
        return [RagHit.from_payload(item) for item in items]

    def fetch_by_ids(
        self, section_ids: Sequence[str], collection: str | None = None
    ) -> list[RagHit]:
        """Retrieve specific sections by id rather than by similarity.

        The service's documented surface is ``POST /search``, and the payload
        key it accepts for an id filter is not known from here, so the likely
        shapes are tried in turn and the first that returns matching rows wins.

        Deliberately no semantic-search fallback: returning plausible passages
        that are not the requested sections is worse than failing, because the
        labelling step would then describe the wrong text and a human would
        approve it.
        """
        ids = [str(s) for s in section_ids if s]
        if not ids:
            return []
        wanted = set(ids)
        target = collection or self._policy.collection

        attempts: list[dict[str, Any]] = [
            {"ids": ids, "collections": [target]},
            {"filter": {"id": ids}, "collections": [target], "top_k": len(ids)},
            {"section_ids": ids, "collections": [target]},
            {"query": "", "ids": ids, "collections": [target], "top_k": len(ids)},
        ]

        tried: list[str] = []
        for body in attempts:
            try:
                hits = self._post(body)
            except RagUnavailable as exc:
                tried.append(f"{sorted(body)} -> {exc}")
                continue
            if any(hit.section_id in wanted for hit in hits):
                return hits
            tried.append(f"{sorted(body)} -> {len(hits)} rows, none matching")

        raise RagUnavailable(
            "no id-lookup payload shape was accepted by the RAG service. "
            f"Tried: {'; '.join(tried)}. Add the correct shape to "
            "RagClient.fetch_by_ids rather than falling back to search."
        )

    def _post(self, body: dict[str, Any]) -> list[RagHit]:
        request = urllib.request.Request(
            self._policy.rag_url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RagUnavailable(f"RAG request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RagUnavailable(f"RAG returned non-JSON: {exc}") from exc
        return self._hits(payload)
