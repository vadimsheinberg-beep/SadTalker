# Publication pipeline: trend scouting → verified claim → video

A conveyor where the machine finds topics, a human approves **inventory** rather
than individual videos, and no unverified claim reaches publication.

This package lives in the SadTalker repository because SadTalker is the avatar
engine at the end of the chain. It is written to be deployed to `/opt/tzoar`
alongside the existing control plane, and it reads its secrets from the env
files already on that server.

---

## The three contours and how they now fit together

The starting position was two unrelated production contours plus a third
concept that matched neither specification:

| | before | after |
|---|---|---|
| **A — Bavli atlas** (`/opt/tzoar`) | corpus clustering, verified claims, TAMHA (RU) + Iahalom (EN) | supplies the **verified claim**; keeps both channels |
| **B — Tiki / Beluga** | published to TAMHA + Iahalom | **publication retired**; its Beluga *style* is kept as the render format |
| **C — corporate collapses** | a concept with no spec | supplies the **topic**, chosen by reading what YouTube is amplifying |

So the merge is: **C decides what the video is about, A decides what it says
that is true, B decides what it looks and sounds like.** Contour B stops being
a publisher and becomes a renderer. That resolves the "куда девать третий
концепт" fork without inventing a fourth channel — C is not a channel, it is
the topic source feeding the two channels that already exist.

Concretely, one video is:

```
scout (C)        what is YouTube pushing right now, inside the collapse domain
   ↓  TopicCandidate
claim (A)        the verified Bavli cluster that best fits that topic
   ↓  AtlasClaim  ── refuses here if nothing verified matches
script (B)       Beluga-style beats written around the claim, claim inserted verbatim
   ↓  Script
gate             correctness checks; refuses rather than warns
   ↓  Package
render           ElevenLabs narration → slides → video → SadTalker avatar
```

---

## Why the claim cannot drift

This is the part everything else is arranged around.

1. A cluster reaches `verified` **only** through `inventory.approve()`, which
   refuses on an empty `theme_name`, a missing translation, or an incomplete
   citation. There is no other path to `verified`.
2. `AtlasClaim.__post_init__` refuses to construct a verified claim without
   sources — the invariant is on the type, not on a caller remembering.
3. `claim_selector` filters unverified clusters out **before** ranking. There
   is no relaxed-threshold fallback. Nothing verified matching means no video.
4. The generator never writes the claim. `beluga.assemble()` discards any
   claim-role beat the model produced and inserts the approved text verbatim.
5. The gate re-checks byte equality (`verify_claim_intact`) before publication.
   A paraphrase of an approved claim is not an approved claim.
6. The description is *generated* from the same sources the gate checked, so
   on-screen citations cannot drift from the ones that were verified.

The machine drafts, quotes, and ranks. It never approves.

---

## Current blocker: `inventory_verified: 0`

All 119 clusters sit in `draft` with an empty `theme_name`, so nothing is
verified and **every** package is refused. That is the gate working correctly,
not a bug to route around.

The bottleneck was never code — it is expert labelling. The path built here
reduces expert effort from authoring to reviewing:

```bash
python -m pipeline inventory            # what is blocking, per cluster
python -m pipeline propose --limit 10   # Claude drafts theme_name + RU/EN + citations
python -m pipeline review               # batch goes to @tamhu_bot
python -m pipeline drain                # replies applied
```

`propose` writes to `proposed`, never `verified`. Its output is checked
mechanically before it is stored: a cited `section_id` must be one that was in
the prompt, and every quote must be an exact substring of that section's text.
A hallucinated citation attached to a real section id is the one failure a
reviewer cannot easily catch by reading, so it is rejected before a human ever
sees it.

Approval verbs, in Telegram, batched:

```
/ok c17                    approve as drafted
/no c18 wrong tractate     reject with a reason
/theme c19 better name     correct — does NOT approve; c19 still needs /ok
/skip c20
```

Separating edit from approval is deliberate: a correction should never be
mistakable for a sign-off.

---

## Layout

```
pipeline/
  contracts.py          types + invariants shared by every stage
  config.py             env-file paths and policy dataclasses (no secret values)
  scout/
    youtube_data.py     Data API v3, 2-key rotation, quota failover
    trend_scout.py      signals → ranked TopicCandidate
  atlas/
    rag_client.py       POST 127.0.0.1:8010/search
    inventory.py        cluster store + the only path to `verified`
    claim_selector.py   topic → verified claim, or refuse
    theme_proposer.py   Claude-drafted labels, mechanically validated
    approval_telegram.py human approval over @tamhu_bot
  script/
    claude_client.py    Messages API, dependency-free
    pacing.py           wpm arithmetic and the 130–155 band
    beluga.py           beat generation with the claim welded in
  render/
    tts_elevenlabs.py   per-beat narration + pronunciation lexicon
    deck.py             SVG slides + rasterising
    pngtools.py         minimal PNG decode/crop (no imaging dependency)
    compose.py          slides + narration → mp4; avatar overlay
    avatar.py           SadTalker bridge
  publish/
    gate.py             refusals, description generation
  cli.py                operator entry point
  tests/                86 tests, fully offline
```

---

## How the scout decides (contour C)

The question is not "what is popular" but "what is YouTube *promoting*". Those
differ: a channel's subscribers reliably watch its uploads, whereas
recommendation traffic shows up as a video beating its own channel's median by
a wide margin. So:

- **outlier ratio** (weight 0.5) — views ÷ that channel's median. The primary
  signal, because it isolates algorithmic push from existing audience.
- **velocity** (0.3) — views per hour since publication.
- **spread** (0.2) — how many *independent* channels carry the topic.

Both raw signals are log-compressed. The gap between 2× and 4× baseline is
meaningful; the gap between 40× and 80× is one runaway video that would
otherwise dictate the whole content plan.

A topic carried by a single channel is dropped outright
(`min_distinct_channels`) — that is one publisher's news, not a platform trend.
Overlapping phrases (`bank` / `bank run` / `silicon valley bank`) are collapsed
so the top of the ranking is not one story wearing several hats.

Domain affinity multiplies the score, so an off-domain viral video cannot
outrank an on-domain one on reach alone. The seed vocabulary for
`corporate_collapse` is in `trend_scout.DOMAIN_PROFILES` and is RU+EN.

---

## Speech rate

117 of the 119 existing scripts run at ~104 wpm against a 130–155 norm. For a
fast-cut format that reads as dead air between jokes.

`pacing.measure()` turns "too slow" into an actionable number — *add 18 words or
retime to 52s* — and `beluga.build()` loops against it, feeding the deficit back
to the generator and regenerating until the script lands in band. It aims at the
nearest band edge, not the midpoint, so the fix is the smallest edit that works.

It also flags per-beat length independently: a script can average correctly and
still contain a 60-word beat, which is no longer a cut.

---

## Deployment

Secrets stay on the server as 0600 env files. This repository contains **no
credential values** — `config.py` knows only paths and variable names.

| file | variables used here |
|---|---|
| `/opt/tzoar/deploy/.env` | control plane |
| `/opt/tzoar/deploy/.env.ytdata` | `YT_DATA_API_KEY_1`, `YT_DATA_API_KEY_2` |
| `/opt/tzoar/deploy/.env.youtube-analytics` | own-channel analytics |
| `/opt/tzoar/deploy/secrets/claude.env` | `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` |
| `/opt/tzoar/deploy/secrets/telegram.env` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| `/opt/tzoar/deploy/secrets/elevenlabs.env` | `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID_RU`, `ELEVENLABS_VOICE_ID_EN` |
| `/opt/az_rag/az_search.env` | `AZ_SEARCH_API_KEY` |

The last two are new files to create. Overrides via process environment win
over file contents, so a container can change one variable without rewriting a
0600 file.

Also needed on the server:

- `librsvg2-bin` — preferred SVG rasteriser. Without it the Chromium fallback
  is used, which works but does less font shaping.
- `ffmpeg` / `ffprobe` — narration concatenation, muxing, and measuring beat
  durations.
- The channel registry: one YouTube channel id per line at
  `$TZOAR_WORKDIR/channels.txt`. Kept as a file so the watch list is reviewed
  and versioned rather than edited inline.

Run the tests with no network and no GPU:

```bash
python3 -m unittest discover -s pipeline/tests -t .
```

---

## Still open

**MuseTalk licence.** The project README and its HuggingFace model card
disagree, and the recorded policy currently assumes the permissive reading.
Nothing here depends on that assumption: SadTalker is the default engine and
its licence was changed to Apache 2.0 with the non-commercial restriction
removed (`README.md:42`, `LICENSE`), which is clean for monetised channels. If
MuseTalk is wanted for lip-sync quality, the ambiguity should be resolved in
writing first — `avatar.AvatarEngine` is a protocol precisely so that decision
can be made later without touching callers.

**Voice and likeness.** Before any avatar ships: written consent covering
commercial RU/EN use and synthetic generation, ~30 min of clean WAV (15–20 RU,
10–15 EN) including tractate names and Hebrew/Aramaic terms, and 15–20 min of
1080p/25fps locked-off green-screen footage. The pronunciation lexicon
(`tts_elevenlabs.Lexicon`) is ready to receive the term list — it is whole-word
and case-insensitive, so `Bava` in `Bava Metzia` is corrected while `Bavarian`
is untouched.

**Brand.** `deck.THEME` is placeholder tokens. Real TAMHA/Yahalom palette,
typefaces, intro card, end CTA and description template drop in there; nothing
else in the module hard-codes appearance.

**Server permissions.** The environment classifier blocks `docker compose up`,
`systemctl`, and running scripts over SSH, while file reads and writes pass.
This is not to be worked around — those steps should be requested explicitly.
