"""The approved inventory of atlas clusters.

This is the object the human actually signs off on. The operating model is that
a person approves *clusters* -- durable, reusable units of verified material --
and never approves individual videos. So the value of a cluster is entirely in
whether a qualified human looked at it, named it, and accepted its wording.

All 119 clusters currently sit in ``draft`` with an empty ``theme_name``, which
is why ``verified_count()`` returns 0 and why the gate blocks every package.
That is the intended behaviour, not a bug to work around.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Protocol

from ..contracts import AtlasClaim, ClaimSource, ClusterStatus, Language


@dataclass
class Cluster:
    """One atlas cluster and its approval state."""

    cluster_id: str
    section_ids: list[str] = field(default_factory=list)
    theme_name: str = ""
    status: ClusterStatus = ClusterStatus.DRAFT
    text_ru: str = ""
    text_en: str = ""
    sources: list[dict[str, str]] = field(default_factory=list)
    approved_by: str = ""
    approved_at: str = ""
    notes: str = ""

    @property
    def verified(self) -> bool:
        return self.status is ClusterStatus.VERIFIED

    def blocking_reasons(self) -> list[str]:
        """Why this cluster cannot be verified yet. Empty means it is ready."""
        reasons: list[str] = []
        if not self.theme_name.strip():
            reasons.append("theme_name is empty")
        if not self.text_ru.strip():
            reasons.append("text_ru is empty")
        if not self.text_en.strip():
            reasons.append("text_en is empty")
        if not self.sources:
            reasons.append("no sources cited")
        else:
            for index, source in enumerate(self.sources):
                missing = [
                    key
                    for key in ("section_id", "tractate", "folio", "quote")
                    if not str(source.get(key, "")).strip()
                ]
                if missing:
                    reasons.append(f"source[{index}] missing {', '.join(missing)}")
        return reasons

    def to_claim(self, relevance: float = 0.0) -> AtlasClaim:
        return AtlasClaim(
            cluster_id=self.cluster_id,
            theme_name=self.theme_name,
            text={Language.RU: self.text_ru, Language.EN: self.text_en},
            sources=tuple(
                ClaimSource(
                    section_id=str(source.get("section_id", "")),
                    tractate=str(source.get("tractate", "")),
                    folio=str(source.get("folio", "")),
                    quote=str(source.get("quote", "")),
                    url=str(source.get("url", "")),
                )
                for source in self.sources
            ),
            verified=self.verified,
            relevance=relevance,
        )


class InventoryStore(Protocol):
    """Storage seam.

    The deployed control plane keeps clusters in Postgres alongside the
    clustering output; this protocol is what a Postgres-backed implementation
    must satisfy so the rest of the pipeline never learns where rows live.
    """

    def all(self) -> Iterable[Cluster]: ...

    def get(self, cluster_id: str) -> Cluster | None: ...

    def cluster_for_section(self, section_id: str) -> Cluster | None: ...

    def save(self, cluster: Cluster) -> None: ...


class JsonInventoryStore:
    """File-backed store. Used for local runs, fixtures, and dry runs."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._clusters: dict[str, Cluster] = {}
        self._section_index: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        for item in raw.get("clusters", []):
            cluster = Cluster(
                cluster_id=str(item["cluster_id"]),
                section_ids=[str(s) for s in item.get("section_ids", [])],
                theme_name=item.get("theme_name", ""),
                status=ClusterStatus(item.get("status", "draft")),
                text_ru=item.get("text_ru", ""),
                text_en=item.get("text_en", ""),
                sources=list(item.get("sources", [])),
                approved_by=item.get("approved_by", ""),
                approved_at=item.get("approved_at", ""),
                notes=item.get("notes", ""),
            )
            self._index(cluster)

    def _index(self, cluster: Cluster) -> None:
        self._clusters[cluster.cluster_id] = cluster
        for section_id in cluster.section_ids:
            self._section_index[section_id] = cluster.cluster_id

    def all(self) -> Iterator[Cluster]:
        return iter(list(self._clusters.values()))

    def get(self, cluster_id: str) -> Cluster | None:
        return self._clusters.get(cluster_id)

    def cluster_for_section(self, section_id: str) -> Cluster | None:
        cluster_id = self._section_index.get(section_id)
        return self._clusters.get(cluster_id) if cluster_id else None

    def save(self, cluster: Cluster) -> None:
        self._index(cluster)
        self.flush()

    def flush(self) -> None:
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "clusters": [
                {
                    "cluster_id": c.cluster_id,
                    "section_ids": c.section_ids,
                    "theme_name": c.theme_name,
                    "status": c.status.value,
                    "text_ru": c.text_ru,
                    "text_en": c.text_en,
                    "sources": c.sources,
                    "approved_by": c.approved_by,
                    "approved_at": c.approved_at,
                    "notes": c.notes,
                }
                for c in self._clusters.values()
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def verified_count(store: InventoryStore) -> int:
    return sum(1 for cluster in store.all() if cluster.verified)


def approve(store: InventoryStore, cluster_id: str, approver: str) -> Cluster:
    """Promote a cluster to ``verified``.

    Refuses on any incomplete field. This is the only path to ``verified``, so
    the completeness check here is what the publication gate is relying on.
    """
    cluster = store.get(cluster_id)
    if cluster is None:
        raise KeyError(f"unknown cluster: {cluster_id}")
    reasons = cluster.blocking_reasons()
    if reasons:
        raise ValueError(
            f"cluster {cluster_id} is not ready for approval: {'; '.join(reasons)}"
        )
    cluster.status = ClusterStatus.VERIFIED
    cluster.approved_by = approver
    cluster.approved_at = datetime.now(timezone.utc).isoformat()
    store.save(cluster)
    return cluster


def reject(store: InventoryStore, cluster_id: str, approver: str, note: str) -> Cluster:
    cluster = store.get(cluster_id)
    if cluster is None:
        raise KeyError(f"unknown cluster: {cluster_id}")
    cluster.status = ClusterStatus.REJECTED
    cluster.approved_by = approver
    cluster.approved_at = datetime.now(timezone.utc).isoformat()
    cluster.notes = note
    store.save(cluster)
    return cluster
