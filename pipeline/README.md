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
| `/opt/tzoar/deploy/.env.youtube-analytics` | `YT_ANALYTICS_CLIENT_ID`, `YT_ANALYTICS_CLIENT_SECRET`, `YT_ANALYTICS_REFRESH_TOKEN_RU`, `YT_ANALYTICS_REFRESH_TOKEN_EN` |
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
### The watch list comes from the deployed registries

`youtube-channel-monitor.service` already maintains the watch list in four
files, and that is authoritative:

```
/opt/tzoar/deploy/production/channel_registries/
├── ai_science_en.json      ├── academic_science_en.json
└── ai_science_ru.json      └── academic_science_ru.json
```

647 records, 620 unique channels, 27 appearing in more than one profile. The
monitor stores per channel: subscribers, last ten videos with view counts, last
publication date, 30-day activity, best result, median of the top three,
views-to-subscribers ratio, numeric-filter result, and last check time.

**The scout reads these files and does not call the Data API.** That is not a
preference. A `search.list` sweep across 620 channels costs ~62,000 quota units
a day against the 20,000 two keys provide — 3.1x over, failing daily. The
monitor has already paid for these numbers.

`--seed-list` still exists for the local file (`data/channels.example.txt`) and
live API calls, and warns above 50 channels. It is for experiments, not
production.

**Field names are read through alias tables.** The registries were written by a
tool that could not be inspected from the development environment. Before
trusting any ranking, run on the server:

```bash
python -m pipeline registry --doctor --sample 10
```

If it reports zero subscriber counts or zero stored videos, the aliases in
`channel_registry.ALIASES` do not match the real files and the ranking is
noise. Correct them from that output rather than guessing twice.

**Editorial status is honoured.** `EXCLUDE_TOPIC` and `EXCLUDE_QUALITY` are
never read. `RIGHTS_CHECK` still counts as a trend signal — reading a public
title tells us what YouTube promotes — but `usable_as_source` is false, so it
must not become source material until rights are settled. Channels below the
numeric filter are still watched: the filter says who is worth imitating, not
who is worth watching, and a quiet channel that suddenly spikes is exactly the
signal worth having.

### Two different sets of channels

Confusing these breaks the scout quietly rather than loudly:

| | what it is | where it lives |
|---|---|---|
| **Watch list** | channels the scout **reads** to find topics — other people's | the four registries |
| **Publication targets** | TAMHA + Iahalom, where we **publish** | `contracts.Channel` |

Our own two channels must **not** appear in the watch list — the scout would
rank our own back catalogue as this week's trends. Their own numbers come from
`scout/youtube_analytics.py`, which needs OAuth rather than an API key because
the Analytics API only exposes a channel's data to its owner.

### Single-file deployment

The package is stdlib-only, so it ships as one executable file rather than a
72 MB checkout:

```bash
python3 pipeline/build_pyz.py -o tzoar-pipeline.pyz   # ~86 KB
./tzoar-pipeline.pyz registry --doctor --sample 10
```

Convenient for cron: absolute path, no working directory, nothing to `git pull`.
The avatar stage is the exception — it shells out to SadTalker's `inference.py`
and needs the full clone plus checkpoints. Full walkthrough in
`docs/deploy-server.md`.

Run the tests with no network and no GPU:

```bash
python3 -m unittest discover -s pipeline/tests -t .
```

---

## The daily layer

`pipeline daily` snapshots the registries and writes four artifacts:

| file | what it holds |
|---|---|
| `daily_new_videos.json` | what competitors published since the last run, sorted by outlier ratio |
| `daily_topic_signals.json` | ranked topics with evidence and a freshness score |
| `status_changes.json` | editorial and metric movements worth a human look |
| `daily_digest.md` | the readable summary of the three above |

The monitor keeps only a *current* view — it overwrites metrics each run — so
the daily layer stores its own compact snapshot per day and diffs against it.
Snapshots are pruned to `DailyPolicy.keep_snapshots` (120 days).

Some behaviours worth knowing:

- **The first run reports nothing as new.** With no earlier snapshot, every
  video would look new; 620 channels did not all publish at once. The digest
  says it is a baseline and tomorrow is the first real report.
- **Missed uploads are surfaced, not hidden.** The registry keeps ten videos
  per channel. If *all* ten are new since the last run, more than ten were
  posted and some are already outside the window — the digest says so rather
  than silently under-reporting.
- **Freshness separates news from evergreen.** A topic's freshness is the share
  of its evidence published today. Without it the digest leads with the same
  permanently-popular subject every morning. `★` marks topics above
  `fresh_topic_threshold`.
- **Quiet channels are chased.** A `WATCH_CORE`/`WATCH_WEEKLY` channel silent
  for `quiet_days` is reported — usually a signal to re-check the editorial
  status.
- **One broken registry does not kill the run.** A daily job that dies because
  one profile was half-written produces no digest at all, which is worse than a
  digest missing one profile and saying so. Only total failure raises.

### Writing scripts from the digest

```bash
python -m pipeline daily
# read daily_digest.md, pick a slug
python -m pipeline build --channel tamha \
    --from-signals /opt/tzoar/deploy/production/daily/daily_topic_signals.json \
    --topic <slug>
```

`--from-signals` takes topics from the same ranked file a human read, rather
than re-deriving a ranking that may have moved since morning. The thing
reviewed is the thing that gets made.

The gate is unchanged by any of this: a topic with no verified atlas cluster
behind it produces no video, however well it scored.

### Known limit: cross-language topics only partly link

Topic grouping is keyword-based, so the same subject links across RU and EN
only when it carries a Latin token:

```
"GPT-5 и Nvidia: прорыв в агентах"   ∩  "The GPT-5 agent breakthrough…"  → {gpt, nvidia}
"Сверхпроводник при комнатной…"      ∩  "Room-temperature superconductor…" → {} 
```

Proper nouns (GPT-5, Nvidia, CERN) cross-link; fully translated concepts do
not. So "the same story trending on both language sides" is detected for
named things and missed for described ones. Closing that gap needs
translation or embeddings at the grouping step — worth doing, not done here.

The brief described the third concept as **corporate collapses**; the deployed
registries are **ai_science** and **academic_science**. Rather than pick one
globally and be silently wrong, each channel is scored against the vocabulary
of the registry it came from — `channel_registry.domain_map()` reads it off the
file, `rank_topics(domains=…)` applies it per channel.

That matters because the wrong-domain failure is invisible: every title drops
to the 0.25 affinity floor, velocity and outlier ratio still rank, and the
output is a plausible-looking list that ignores the subject entirely. Deriving
the domain means it cannot happen by omission.

`ScoutPolicy.domain` remains the fallback for channels outside the registries
(the `--seed-list` path). `corporate_collapse` vocabulary is kept, so if that
concept is still live it needs its own registry file and an entry in
`REGISTRY_DOMAINS` — a question worth settling, but no longer one that blocks
the scout from running correctly.

## Still open

**MuseTalk licence.** The project README and its HuggingFace model card
disagree, and the recorded policy currently assumes the permissive reading.
Nothing here depends on that assumption: SadTalker is the default engine and
its licence was changed to Apache 2.0 with the non-commercial restriction
removed (`README.md:42`, `LICENSE`), which is clean for monetised channels. If
MuseTalk is wanted for lip-sync quality, the ambiguity should be resolved in
writing first — `avatar.AvatarEngine` is a protocol precisely so that decision
can be made later without touching callers.

**Voice and likeness.** Permission granted; the signable record is at
`docs/consent-voice-likeness.md` and the recording specs at
`docs/recording-and-brand.md`. Still needed: the recordings themselves, and the
ElevenLabs voice ids on the server once the voices exist.

Starter pronunciation lexicons ship at `data/lexicon_{ru,en}.json` — 37 Bavli
tractates plus common terms, loaded per language by `Lexicon.for_language()`.
**They are unverified guesses** and need checking against real voice output.
RU values are Cyrillic respellings and EN values are hyphenated syllables,
because the Russian voice reads Cyrillic far more reliably than transliterated
Latin.

One calibration is outstanding: `pacing.estimate_beat_duration()` assumes
Russian runs 8% slower than English at equal word counts. That is a working
figure, not a measured one — correct it against real ElevenLabs output.

**Brand.** `deck.THEME` is placeholder tokens. Real TAMHA/Yahalom palette,
typefaces, intro card, end CTA and description template drop in there; nothing
else in the module hard-codes appearance. Note that fonts must be installed on
the server or rsvg substitutes silently, and that `gate.ALLOWED_LINK_HOSTS`
must be extended before any new link domain passes the gate.

**Server permissions.** The environment classifier blocks `docker compose up`,
`systemctl`, and running scripts over SSH, while file reads and writes pass.
This is not to be worked around — those steps should be requested explicitly.
