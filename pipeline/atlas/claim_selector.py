"""Pick the one verified Bavli thought that best fits a trending topic.

The path is deliberately short. The topic text goes to the RAG; each hit's
``id`` is a section id; the section id resolves to a cluster; unverified
clusters are dropped; the best remaining cluster wins. There is no fallback
that relaxes the verified requirement -- when nothing verified matches, the
correct outcome is to produce no video.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ..config import SETTINGS, AtlasPolicy
from ..contracts import AtlasClaim, TopicCandidate
from .inventory import InventoryStore
from .rag_client import RagClient, RagHit


class NoVerifiedClaim(RuntimeError):
    """No verified cluster matched the topic well enough.

    Callers must let this propagate. Shipping the topic without an atlas claim,
    or with an unverified one, is the failure mode this whole design exists to
    prevent.
    """


@dataclass(frozen=True)
class ClaimMatch:
    claim: AtlasClaim
    relevance: float
    matched_sections: tuple[str, ...]


def select_claim(
    topic: TopicCandidate,
    store: InventoryStore,
    rag: RagClient | None = None,
    policy: AtlasPolicy | None = None,
) -> ClaimMatch:
    policy = policy or SETTINGS.atlas
    rag = rag or RagClient(policy=policy)

    hits = rag.search(topic.query_text(), top_k=policy.top_k)
    match = best_match(hits, store, policy)
    if match is None:
        raise NoVerifiedClaim(
            f"no verified cluster above relevance {policy.min_relevance} for "
            f"topic '{topic.slug}' ({len(hits)} RAG hits inspected). "
            "Verify more inventory or drop this topic."
        )
    return match


def best_match(
    hits: list[RagHit], store: InventoryStore, policy: AtlasPolicy
) -> ClaimMatch | None:
    """Aggregate hits by cluster and return the strongest verified one.

    A cluster's relevance is its best hit, nudged upward when several of its
    sections surfaced independently: repeated hits mean the topic touches the
    cluster's substance rather than one incidental line.
    """
    scores: dict[str, list[RagHit]] = defaultdict(list)
    for hit in hits:
        cluster = store.cluster_for_section(hit.section_id)
        if cluster is None:
            continue
        if policy.require_verified and not cluster.verified:
            continue
        scores[cluster.cluster_id].append(hit)

    best: ClaimMatch | None = None
    for cluster_id, cluster_hits in scores.items():
        cluster = store.get(cluster_id)
        if cluster is None:
            continue
        top = max(hit.score for hit in cluster_hits)
        support = min(0.1, 0.025 * (len(cluster_hits) - 1))
        relevance = min(1.0, top + support)
        if relevance < policy.min_relevance:
            continue
        if best is None or relevance > best.relevance:
            best = ClaimMatch(
                claim=cluster.to_claim(relevance=relevance),
                relevance=relevance,
                matched_sections=tuple(hit.section_id for hit in cluster_hits),
            )
    return best


def explain_miss(
    hits: list[RagHit], store: InventoryStore, policy: AtlasPolicy | None = None
) -> str:
    """Diagnose why a topic found nothing, for the operator log.

    Distinguishing "the corpus has nothing to say" from "the corpus has plenty
    to say but nobody has verified it" is the difference between dropping a
    topic and adding a cluster to the labelling queue.
    """
    policy = policy or SETTINGS.atlas
    if not hits:
        return "RAG returned no hits at all -- check the service and the query."

    unknown = verified = unverified = 0
    best_unverified = 0.0
    for hit in hits:
        cluster = store.cluster_for_section(hit.section_id)
        if cluster is None:
            unknown += 1
        elif cluster.verified:
            verified += 1
        else:
            unverified += 1
            best_unverified = max(best_unverified, hit.score)

    if unverified and not verified:
        return (
            f"{unverified} of {len(hits)} hits map to clusters that exist but are "
            f"not verified (best score {best_unverified:.2f}). The corpus has an "
            "answer; the inventory has not been approved. Queue those clusters "
            "for labelling."
        )
    if verified:
        return (
            f"{verified} verified clusters matched but none reached "
            f"relevance {policy.min_relevance}. The topic is genuinely off-corpus."
        )
    return (
        f"{unknown} of {len(hits)} hits map to no known cluster -- the section "
        "index and the clustering output are out of sync."
    )
