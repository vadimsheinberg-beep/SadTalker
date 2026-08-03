"""Operator entry point.

    python -m pipeline scout                 rank what YouTube is pushing
    python -m pipeline inventory             report inventory readiness
    python -m pipeline propose  --limit 10   draft theme_names for review
    python -m pipeline review                send the batch to Telegram
    python -m pipeline drain                 apply operator replies
    python -m pipeline build --topic <slug>  script + gate, no rendering
    python -m pipeline render --package <f>  narration, deck, video
    python -m pipeline avatar --package <f>  talking head, composited

The split between ``build`` and ``render`` is deliberate: ``build`` is cheap and
runs the gate, so a package that will be refused never reaches the stage that
costs GPU time and TTS credits.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .atlas import approval_telegram, inventory, theme_proposer
from .atlas.claim_selector import NoVerifiedClaim, select_claim
from .atlas.rag_client import RagClient
from .config import SETTINGS
from .contracts import Channel, Package, PublicationBlocked
from .publish.gate import BlockList, build_description, check_package
from .scout import registry, trend_scout
from .scout.youtube_data import YouTubeDataClient
from .script.beluga import build as build_script


def _store(args: argparse.Namespace) -> inventory.JsonInventoryStore:
    return inventory.JsonInventoryStore(Path(args.inventory))


def _channels(args: argparse.Namespace) -> list[str]:
    """Resolve the watch list to channel ids.

    The file may hold handles, URLs, or raw ids; handles are resolved once and
    cached beside it. These are the channels the scout *reads* -- never TAMHA
    or Iahalom, which are where we publish.
    """
    path = Path(args.registry)
    try:
        entries = registry.load(path)
    except registry.RegistryError as exc:
        raise SystemExit(str(exc)) from exc

    channel_ids, problems = registry.resolve(
        entries,
        YouTubeDataClient(),
        registry.HandleCache(path.with_suffix(".resolved.json")),
    )
    for problem in problems:
        print(f"  registry: {problem}", file=sys.stderr)
    if not channel_ids:
        raise SystemExit(f"no usable channels in {path}")
    return channel_ids


def cmd_scout(args: argparse.Namespace) -> int:
    client = YouTubeDataClient()
    signals = client.signals(_channels(args), SETTINGS.scout.lookback_hours)
    topics = trend_scout.rank_topics(signals)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "slug": t.slug,
                        "title": t.title,
                        "score": t.score,
                        "channels": t.distinct_channels,
                        "keywords": list(t.keywords),
                        "evidence": [s.video_id for s in t.evidence],
                    }
                    for t in topics
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"{len(signals)} videos scanned, {len(topics)} topics\n")
        for rank, topic in enumerate(topics, 1):
            print(
                f"{rank:2d}. {topic.score:.3f}  {topic.slug}\n"
                f"     {topic.title}\n"
                f"     {topic.distinct_channels} channels · "
                f"{', '.join(topic.keywords[:5])}"
            )
    return 0


def cmd_inventory(args: argparse.Namespace) -> int:
    store = _store(args)
    print(approval_telegram.progress(store))
    blocked = [
        (cluster, cluster.blocking_reasons())
        for cluster in store.all()
        if not cluster.verified
    ]
    if args.verbose:
        for cluster, reasons in blocked[: args.limit]:
            print(f"  {cluster.cluster_id} [{cluster.status.value}]: "
                  f"{'; '.join(reasons) or 'ready — needs /ok'}")
    ready = sum(1 for _, reasons in blocked if not reasons)
    if ready:
        print(f"\n{ready} clusters are complete and waiting only on human /ok.")
    if inventory.verified_count(store) == 0:
        print(
            "\nNo verified clusters. Every package will be refused until at "
            "least one cluster is approved. This is the gate working, not a bug."
        )
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    store = _store(args)
    rag = RagClient()
    drafted: list[theme_proposer.Proposal] = []
    for cluster in theme_proposer.pending(store)[: args.limit]:
        sections = [
            theme_proposer.SectionText.from_hit(hit)
            for hit in rag.search(" ".join(cluster.section_ids[:8]), top_k=12)
            if hit.section_id in set(cluster.section_ids)
        ]
        try:
            proposal = theme_proposer.propose(cluster, sections)
        except theme_proposer.ProposalRejected as exc:
            print(f"  skip {cluster.cluster_id}: {exc}", file=sys.stderr)
            continue
        theme_proposer.stage(store, proposal)
        drafted.append(proposal)
        print(f"  drafted {cluster.cluster_id}: {proposal.theme_name or '(incoherent)'}")

    if args.out:
        Path(args.out).write_text(
            theme_proposer.dump_proposals(drafted), encoding="utf-8"
        )
    print(f"\n{len(drafted)} proposals staged as 'proposed'. None are verified.")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    store = _store(args)
    bot = approval_telegram.TelegramBot()
    waiting = theme_proposer.awaiting_human(store)[: args.limit]
    proposals = [
        theme_proposer.Proposal(
            cluster_id=cluster.cluster_id,
            coherent=True,
            theme_name=cluster.theme_name,
            text_ru=cluster.text_ru,
            text_en=cluster.text_en,
            sources=tuple(cluster.sources),
            confidence=0.0,
            reviewer_note=cluster.notes,
        )
        for cluster in waiting
    ]
    sent = approval_telegram.send_batch(bot, proposals)
    print(f"sent {sent} messages for {len(proposals)} clusters")
    return 0


def cmd_drain(args: argparse.Namespace) -> int:
    store = _store(args)
    bot = approval_telegram.TelegramBot()
    offset_file = Path(args.inventory).with_suffix(".offset")
    offset = int(offset_file.read_text()) if offset_file.exists() else 0
    next_offset, results = approval_telegram.drain(bot, store, args.approver, offset)
    offset_file.write_text(str(next_offset))
    for line in results:
        print(line)
    print(f"\n{approval_telegram.progress(store)}")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    store = _store(args)
    channel = Channel(args.channel)

    client = YouTubeDataClient()
    signals = client.signals(_channels(args), SETTINGS.scout.lookback_hours)
    topics = trend_scout.rank_topics(signals)
    if args.topic:
        topics = [t for t in topics if t.slug == args.topic] or topics
    if not topics:
        print("no topics found", file=sys.stderr)
        return 1
    topic = topics[0]

    try:
        match = select_claim(topic, store)
    except NoVerifiedClaim as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    generated = build_script(
        topic, match.claim, channel.language, duration_s=args.duration
    )
    package = Package(
        topic=topic,
        claim=match.claim,
        script=generated.script,
        channel=channel,
        target_duration_s=args.duration,
    )

    blocklist = (
        BlockList.from_lines(Path(args.blocklist).read_text(encoding="utf-8").splitlines())
        if args.blocklist and Path(args.blocklist).exists()
        else None
    )
    result = check_package(
        package, description=build_description(package), blocklist=blocklist
    )
    try:
        result.raise_if_blocked()
    except PublicationBlocked as exc:
        print(str(exc), file=sys.stderr)
        return 3

    out = Path(args.out or SETTINGS.workdir / f"{package.package_id}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(package.to_json(), encoding="utf-8")
    print(
        f"package {package.package_id} → {out}\n"
        f"  topic: {topic.slug} (score {topic.score:.3f})\n"
        f"  claim: {match.claim.cluster_id} '{match.claim.theme_name}' "
        f"(relevance {match.relevance:.2f})\n"
        f"  script: {generated.report.describe()} in {generated.attempts} attempt(s)"
    )
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    """Narration, slides, and the muxed presentation video."""
    from .render.compose import compose
    from .render.deck import build_slides, write_deck
    from .render.tts_elevenlabs import Lexicon, narrate

    package = Package.from_json(Path(args.package).read_text(encoding="utf-8"))
    outdir = Path(args.out or SETTINGS.workdir / package.package_id)
    outdir.mkdir(parents=True, exist_ok=True)

    lexicon = (
        Lexicon.load(Path(args.lexicon))
        if args.lexicon
        else Lexicon.for_language(package.script.language)
    )
    utterances = narrate(package.script, outdir / "audio", lexicon=lexicon)
    slides = write_deck(
        build_slides(
            package.script, package.claim, [u.duration_s for u in utterances]
        ),
        outdir / "deck",
    )
    composition = compose(slides, utterances, outdir)

    updated = Package(
        topic=package.topic,
        claim=package.claim,
        script=package.script,
        channel=package.channel,
        target_duration_s=package.target_duration_s,
        assets={
            **package.assets,
            "video": str(composition.video_path),
            "audio": str(composition.audio_path),
        },
    )
    Path(args.package).write_text(updated.to_json(), encoding="utf-8")
    print(
        f"rendered {composition.duration_s:.1f}s → {composition.video_path}\n"
        f"  narration: {composition.audio_path}\n"
        f"  package updated with video/audio assets"
    )
    return 0


def cmd_avatar(args: argparse.Namespace) -> int:
    """Talking head from the rendered narration, composited over the slides."""
    from .render.avatar import AvatarRequest, SadTalkerEngine
    from .render.compose import overlay_avatar

    package = Package.from_json(Path(args.package).read_text(encoding="utf-8"))
    audio = package.assets.get("audio")
    video = package.assets.get("video")
    if not audio or not video:
        print(
            "package has no rendered assets — run `render` first", file=sys.stderr
        )
        return 1

    engine = SadTalkerEngine(cpu=args.cpu)
    problems = engine.preflight()
    if problems:
        print("; ".join(problems), file=sys.stderr)
        return 1

    outdir = Path(args.out or SETTINGS.workdir / package.package_id / "avatar")
    talking_head = engine.render(
        AvatarRequest(source=Path(args.source), audio=Path(audio), outdir=outdir)
    )
    final = overlay_avatar(
        Path(video), talking_head, outdir / "final.mp4", corner=args.corner
    )
    print(f"avatar: {talking_head}\nfinal:  {final}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline", description=__doc__)
    parser.add_argument(
        "--inventory",
        default=str(SETTINGS.workdir / "inventory.json"),
        help="path to the cluster inventory store",
    )
    parser.add_argument(
        "--registry",
        default=str(SETTINGS.workdir / "channels.txt"),
        help="watched YouTube channel ids, one per line",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scout = sub.add_parser("scout", help="rank trending topics")
    scout.add_argument("--json", action="store_true")
    scout.set_defaults(func=cmd_scout)

    inv = sub.add_parser("inventory", help="inventory readiness report")
    inv.add_argument("--verbose", action="store_true")
    inv.add_argument("--limit", type=int, default=30)
    inv.set_defaults(func=cmd_inventory)

    propose = sub.add_parser("propose", help="draft theme_names for review")
    propose.add_argument("--limit", type=int, default=10)
    propose.add_argument("--out", default="")
    propose.set_defaults(func=cmd_propose)

    review = sub.add_parser("review", help="send proposals to Telegram")
    review.add_argument("--limit", type=int, default=10)
    review.set_defaults(func=cmd_review)

    drain = sub.add_parser("drain", help="apply operator replies")
    drain.add_argument("--approver", default="operator")
    drain.set_defaults(func=cmd_drain)

    build = sub.add_parser("build", help="scout → claim → script → gate")
    build.add_argument("--channel", choices=[c.value for c in Channel], required=True)
    build.add_argument("--topic", default="")
    build.add_argument("--duration", type=int, default=60)
    build.add_argument("--blocklist", default="")
    build.add_argument("--out", default="")
    build.set_defaults(func=cmd_build)

    render = sub.add_parser("render", help="narration, slides, video")
    render.add_argument("--package", required=True)
    render.add_argument("--lexicon", default="", help="pronunciation overrides JSON")
    render.add_argument("--out", default="")
    render.set_defaults(func=cmd_render)

    avatar = sub.add_parser("avatar", help="talking head over the slides")
    avatar.add_argument("--package", required=True)
    avatar.add_argument("--source", required=True, help="presenter still or frame")
    avatar.add_argument("--corner", default="br", choices=["br", "bl", "tr", "tl"])
    avatar.add_argument("--cpu", action="store_true")
    avatar.add_argument("--out", default="")
    avatar.set_defaults(func=cmd_avatar)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
