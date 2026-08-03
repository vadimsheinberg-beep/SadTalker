"""Third concept: read the platform, decide what to make a video about.

The question this stage answers is not "what is popular" but "what is YouTube
currently pushing". Those differ. A channel's own subscribers reliably watch
its uploads; recommendation traffic shows up as a video beating its channel's
median by a wide margin. That ratio is the primary signal here, velocity is
secondary, and agreement across independent channels is the tiebreak that
separates a trend from one publisher's good week.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict
from typing import Iterable, Sequence

from ..config import SETTINGS, ScoutPolicy
from ..contracts import TopicCandidate, VideoSignal

# Deliberately small: these are the words that survive title-case marketing
# copy in both languages and would otherwise dominate every keyword count.
STOPWORDS: frozenset[str] = frozenset(
    """
    a an and are as at be been but by for from had has have how i if in into is it its
    of on or that the their there they this to was were what when which who why will
    with you your про как что это где чего чему кто когда почему который которая
    если или и но а на в во за из от до по для не ни же бы ли то так там тут вот
    его ее их наш ваш они она оно мы вы был была было были быть есть
    """.split()
)

_TOKEN_RE = re.compile(r"[^\w]+", re.UNICODE)

# Seed vocabulary per editorial domain. Keywords are scored, not filtered on,
# so a title only needs to touch the domain to be considered.
#
# The deployed registries are ai_science and academic_science (RU and EN),
# which is a different domain from the corporate-collapse concept the scout was
# first written against. Both are kept: `ScoutPolicy.domain` selects one, and
# picking the wrong one does not error -- it quietly flattens every score to
# the 0.25 floor, which is why `domain_affinity` is worth checking first when a
# ranking looks like noise.
DOMAIN_PROFILES: dict[str, tuple[str, ...]] = {
    "ai_science": (
        "ai", "artificial intelligence", "llm", "gpt", "neural", "model",
        "openai", "anthropic", "deepmind", "agent", "robotics", "chip", "gpu",
        "benchmark", "training", "alignment", "breakthrough", "research",
        "ии", "искусственный интеллект", "нейросеть", "нейросети", "модель",
        "обучение", "алгоритм", "робот", "чип", "прорыв", "исследование",
    ),
    "academic_science": (
        "study", "research", "paper", "physics", "biology", "chemistry",
        "mathematics", "astronomy", "neuroscience", "experiment", "discovery",
        "theory", "quantum", "evolution", "cosmology", "peer review",
        "наука", "исследование", "физика", "биология", "химия", "математика",
        "астрономия", "эксперимент", "открытие", "теория", "квант", "эволюция",
    ),
    "corporate_collapse": (
        "bankruptcy", "collapse", "fraud", "downfall", "scandal", "lawsuit",
        "layoffs", "shutdown", "delisted", "insolvency", "ponzi", "meltdown",
        "банкротство", "крах", "мошенничество", "провал", "скандал", "иск",
        "увольнения", "закрытие", "убытки", "пирамида", "падение",
    ),
}

# Which registry file feeds which domain vocabulary.
REGISTRY_DOMAINS: dict[str, str] = {
    "ai_science_en": "ai_science",
    "ai_science_ru": "ai_science",
    "academic_science_en": "academic_science",
    "academic_science_ru": "academic_science",
}


def normalise(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    return _TOKEN_RE.sub(" ", folded).strip()


def tokenize(text: str) -> list[str]:
    return [
        token
        for token in normalise(text).split()
        if len(token) > 2 and token not in STOPWORDS and not token.isdigit()
    ]


def keyphrases(title: str) -> set[str]:
    """Unigrams plus adjacent bigrams.

    Bigrams matter because ``silicon valley`` and ``bank run`` carry the topic
    while their halves do not.
    """
    tokens = tokenize(title)
    phrases = set(tokens)
    phrases.update(f"{a} {b}" for a, b in zip(tokens, tokens[1:]))
    return phrases


def domain_affinity(title: str, domain: str) -> float:
    """Fraction of domain seed terms present, capped at 1.0.

    Used as a multiplier so an off-domain viral video cannot outrank an
    on-domain one purely on velocity.
    """
    seeds = DOMAIN_PROFILES.get(domain, ())
    if not seeds:
        return 1.0
    text = normalise(title)
    hits = sum(1 for seed in seeds if seed in text)
    return min(1.0, 0.25 + 0.25 * hits)


def score_signal(signal: VideoSignal, domain: str, policy: ScoutPolicy) -> float:
    """Score one video in [0, ~1].

    Both raw signals are log-compressed: the difference between 2x and 4x
    baseline is meaningful, the difference between 40x and 80x is noise from a
    single runaway video that would otherwise swamp the ranking.
    """
    outlier = math.log1p(max(signal.outlier_ratio, 0.0)) / math.log(11)  # 10x -> 1.0
    velocity = math.log1p(max(signal.velocity, 0.0)) / math.log(10_001)  # 10k/h -> 1.0
    base = (
        policy.weight_outlier * min(outlier, 1.0)
        + policy.weight_velocity * min(velocity, 1.0)
    )
    return base * domain_affinity(signal.title, domain)


def rank_topics(
    signals: Iterable[VideoSignal],
    policy: ScoutPolicy | None = None,
    domains: dict[str, str] | None = None,
) -> list[TopicCandidate]:
    """Group signals into keyphrase topics and rank them.

    A topic is kept only when independent channels agree on it -- see
    ``ScoutPolicy.min_distinct_channels``. This is what stops the scout from
    turning one publisher's upload schedule into the week's content plan.

    ``domains`` maps channel id to domain vocabulary. Registry-sourced signals
    supply it from the file each channel came from, so an AI channel is scored
    against AI vocabulary and an academic one against academic vocabulary with
    nobody choosing a global setting. Channels absent from the map fall back to
    ``policy.domain``.
    """
    policy = policy or SETTINGS.scout
    domains = domains or {}
    eligible = [s for s in signals if s.views >= policy.min_views]

    by_phrase: dict[str, list[VideoSignal]] = defaultdict(list)
    for signal in eligible:
        for phrase in keyphrases(signal.title):
            by_phrase[phrase].append(signal)

    candidates: list[TopicCandidate] = []
    for phrase, members in by_phrase.items():
        channels = {member.channel_id for member in members}
        if len(channels) < policy.min_distinct_channels:
            continue
        scores = [
            score_signal(
                member, domains.get(member.channel_id, policy.domain), policy
            )
            for member in members
        ]
        # Spread rewards agreement across channels but saturates quickly: three
        # independent channels is strong evidence, ten is not three times better.
        spread = min(1.0, math.log1p(len(channels)) / math.log(5))
        score = (
            sum(sorted(scores, reverse=True)[:5]) / min(len(scores), 5)
            + policy.weight_spread * spread
        )
        evidence = tuple(
            sorted(members, key=lambda s: s.outlier_ratio, reverse=True)[:5]
        )
        candidates.append(
            TopicCandidate(
                slug=slugify(phrase),
                title=evidence[0].title,
                keywords=_expand_keywords(phrase, evidence),
                evidence=evidence,
                score=round(score, 4),
                domain=_dominant_domain(members, domains, policy.domain),
            )
        )

    candidates.sort(key=lambda c: (-c.score, c.slug))
    return _dedupe(candidates)[: policy.max_candidates]


def _dominant_domain(
    members: Sequence[VideoSignal], domains: dict[str, str], fallback: str
) -> str:
    """The domain most of a topic's channels belong to.

    Recorded on the candidate so the digest and the signals file report the
    vocabulary a topic was actually scored against. Reporting the policy
    default instead would mislabel every registry-sourced topic while the
    scoring underneath was correct -- the kind of discrepancy that costs an
    afternoon to notice.
    """
    counts: dict[str, int] = defaultdict(int)
    for member in members:
        domain = domains.get(member.channel_id)
        if domain:
            counts[domain] += 1
    if not counts:
        return fallback
    return max(sorted(counts), key=lambda name: counts[name])


def slugify(phrase: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalise(phrase)).strip("-") or "topic"


def _expand_keywords(
    phrase: str, evidence: Sequence[VideoSignal]
) -> tuple[str, ...]:
    """The topic phrase plus the terms its evidence titles most agree on."""
    counts: dict[str, int] = defaultdict(int)
    for signal in evidence:
        for token in set(tokenize(signal.title)):
            counts[token] += 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    extra = [token for token, _ in ranked if token not in phrase][:6]
    return (phrase, *extra)


def _dedupe(candidates: list[TopicCandidate]) -> list[TopicCandidate]:
    """Drop lower-scored topics whose evidence is already covered.

    ``bank`` and ``bank run`` surface as separate phrases over the same videos;
    without this the top of the ranking is one story wearing several hats.
    """
    kept: list[TopicCandidate] = []
    claimed: set[str] = set()
    for candidate in candidates:
        ids = {signal.video_id for signal in candidate.evidence}
        if ids and len(ids & claimed) / len(ids) > 0.6:
            continue
        claimed |= ids
        kept.append(candidate)
    return kept
