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


CLUSTER_ALIASES: dict[str, tuple[str, ...]] = {
    "cluster_id": ("cluster_id", "clusterId", "id", "cluster", "label"),
    "section_ids": (
        "section_ids", "sections", "members", "section_id_list", "ids", "doc_ids",
    ),
    "theme_name": ("theme_name", "theme", "name", "title"),
    "status": ("status", "state"),
    "text_ru": ("text_ru", "ru", "claim_ru", "summary_ru"),
    "text_en": ("text_en", "en", "claim_en", "summary_en"),
    "notes": ("notes", "note", "comment"),
}


def _pick(record: dict, names: tuple[str, ...]) -> object:
    for name in names:
        if name in record and record[name] not in (None, ""):
            return record[name]
    return None


def import_clusters(store: InventoryStore, payload: object) -> tuple[int, list[str]]:
    """Load clusters from an atlas export into the store.

    Field names are read through aliases for the same reason the channel
    registry is: the export was written elsewhere and its exact schema is not
    known here.

    Import never sets ``verified``. Even if the source claims a cluster is
    approved, it arrives as ``draft`` or ``proposed`` and a human has to pass
    it through :func:`approve`, which re-checks completeness. Trusting an
    imported ``verified`` flag would let an unreviewed claim reach publication
    through a file nobody inspected.

    Existing clusters keep any human decision already recorded against them:
    re-importing refreshes section ids without discarding an approval.
    """
    records: list[dict]
    if isinstance(payload, list):
        records = [r for r in payload if isinstance(r, dict)]
    elif isinstance(payload, dict):
        for key in ("clusters", "items", "records", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                records = [r for r in value if isinstance(r, dict)]
                break
        else:
            records = [
                {**value, "cluster_id": value.get("cluster_id", key)}
                for key, value in payload.items()
                if isinstance(value, dict)
            ]
    else:
        raise ValueError(f"cannot import clusters from {type(payload).__name__}")

    imported = 0
    problems: list[str] = []
    for record in records:
        cluster_id = _pick(record, CLUSTER_ALIASES["cluster_id"])
        if cluster_id is None:
            problems.append(f"record without a cluster id: {sorted(record)[:6]}")
            continue

        raw_sections = _pick(record, CLUSTER_ALIASES["section_ids"]) or []
        if isinstance(raw_sections, (str, int)):
            raw_sections = [raw_sections]
        section_ids = [str(s) for s in raw_sections if s not in (None, "")]
        if not section_ids:
            problems.append(f"cluster {cluster_id}: no section ids found")

        existing = store.get(str(cluster_id))
        if existing is not None:
            # Refresh membership; never undo a human decision.
            existing.section_ids = section_ids or existing.section_ids
            store.save(existing)
            imported += 1
            continue

        store.save(
            Cluster(
                cluster_id=str(cluster_id),
                section_ids=section_ids,
                theme_name=str(_pick(record, CLUSTER_ALIASES["theme_name"]) or ""),
                text_ru=str(_pick(record, CLUSTER_ALIASES["text_ru"]) or ""),
                text_en=str(_pick(record, CLUSTER_ALIASES["text_en"]) or ""),
                notes=str(_pick(record, CLUSTER_ALIASES["notes"]) or ""),
                status=ClusterStatus.DRAFT,
            )
        )
        imported += 1

    return imported, problems


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
