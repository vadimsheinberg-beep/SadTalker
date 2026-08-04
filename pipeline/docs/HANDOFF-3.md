# HANDOFF-3 — Tzoar pipeline

Written to be read by the next session. It lives in the repository on purpose:
HANDOFF-2 was at a macOS path and the remote session could not open it, which
cost a round trip. Anything the next session must know belongs here.

Branch: `claude/youtube-bavli-atlas-pipeline-h1qoqu`
State: 186 tests, all offline. Nothing has run against the real server yet.

---

## 1. What was decided

**Three contours became one conveyor.** The starting position was two unrelated
production contours plus a third concept matching neither spec. The resolution:

> **C decides what the video is about. A decides what in it is true.
> B decides what it looks and sounds like.**

- **C — trend scouting.** Reads what YouTube is currently *promoting* (not
  merely what is popular) and produces ranked topics.
- **A — Bavli atlas.** Supplies one verified, sourced claim per video.
- **B — Beluga format.** Stops being a publisher, becomes the renderer.

The two channels are unchanged: **TAMHA** (RU) and **Iahalom** (EN). C is not a
third channel — it is the topic source feeding both.

**Humans approve inventory, not videos.** A person approves *clusters* —
durable, reusable verified material — and never signs off on individual
videos. That is the whole point of the design.

---

## 2. The invariant everything else serves

No unverified claim reaches publication. Enforced at six independent points, so
defeating one does not defeat the rule:

1. `inventory.approve()` is the only path to `verified`, and refuses on empty
   `theme_name`, missing translation, or incomplete citation.
2. `AtlasClaim.__post_init__` refuses to construct as verified without sources.
3. `claim_selector` filters unverified clusters *before* ranking. No
   relaxed-threshold fallback — nothing verified means no video.
4. `beluga.assemble()` discards any model-authored claim beat and inserts the
   approved text verbatim.
5. The gate re-checks byte equality before publication. A paraphrase of an
   approved claim is not an approved claim.
6. Descriptions are generated from the same sources the gate verified.

Plus: `import_clusters` never sets `verified`, even when the export claims it.

**Do not add a convenience path around any of these.** Each exists because the
obvious shortcut is silently wrong.

---

## 3. What is built

| stage | command | state |
|---|---|---|
| trend scouting | `scout` | reads deployed registries, no API calls |
| daily layer | `daily` | four artifacts + snapshots, verified on simulation |
| claim selection | (internal) | verified-only, refuses cleanly |
| script | `build` | Beluga beats, pacing loop, claim welded in |
| presentation | `render` | SVG slides → PNG → mp4, **verified end to end** |
| avatar | `avatar` | SadTalker bridge, untested (needs GPU + checkpoints) |
| inventory loop | `propose`/`review`/`drain` | built, never run against real data |
| diagnostics | `report`, `registry --doctor` | canary-tested for leaks |

Verified for real: 7 slides → 34.1s H.264 1920×1080 25fps + AAC, citation
legible in the extracted frame.

**Deployment:** stdlib only, no third-party packages. Ships as one 86 KB
zipapp (`pipeline/build_pyz.py`) rather than the 72 MB repo. Full walkthrough
in `docs/deploy-server.md`.

---

## 4. What is blocked, and on what

**a) The registry schema is unknown.** The four registries under
`/opt/tzoar/deploy/production/channel_registries/` were written by
`youtube-channel-monitor` and could not be inspected — the dev environment has
no route to the server. Every field is read through alias tables in
`channel_registry.ALIASES`.

> Run `tzoar-pipeline.pyz report --out /tmp/tzoar-report.md` on the server and
> read it. If it shows `UNMATCHED` or zero subscriber counts, **correct the
> aliases before trusting any ranking** — wrong aliases produce zeros, and
> zeros produce a plausible-looking ranking that means nothing.

**b) `inventory_verified: 0` — the main blocker.** All 119 atlas clusters are
`draft` with empty `theme_name`. Nothing is verified, so the gate refuses every
package. That is correct behaviour, not a bug.

Two unknowns block clearing it: the shape of the atlas cluster export (needed
for `inventory --import-from`), and whether the RAG can return sections by id
(see §5, defect 7).

**c) Secrets not yet on the server:** `elevenlabs.env`, and the Claude/Telegram
files must be confirmed present. `report` shows presence without reading values.

---

## 5. Defects found and fixed — do not reintroduce these

Each was a *silent* failure. None raised an error; all produced plausible
output. They are listed because the obvious implementation is the broken one.

1. **Chromium rasteriser cropped every claim slide.** Headless Chromium lays
   out in a viewport 87px shorter than the requested window while screenshotting
   the full height, silently cutting the source citation — the one element that
   must stay legible. Now measured at runtime and cropped back to the artboard.
2. **Scout would have exceeded Data API quota 3.1×.** A `search.list` sweep over
   620 channels costs ~62,000 units/day against 20,000 from two keys. The
   monitor already stores those numbers; the scout reads them and makes no API
   calls.
3. **Domain mismatch.** The brief said corporate collapses; the registries are
   `ai_science` / `academic_science`. Resolved by *deriving* the domain per
   channel from its source registry, so a wrong global setting cannot happen by
   omission. A wrong domain does not error — every title drops to the 0.25
   affinity floor and the ranking still looks plausible.
4. **Topic domain label came from the policy default,** mislabelling every
   registry-sourced topic while the scoring underneath was correct.
5. **`propose` used section ids as a semantic search query.** Opaque ids →
   unrelated passages → id filter drops them → every cluster skipped with "no
   section texts". The tool that exists to clear the main blocker would have
   reported a successful run and drafted nothing. Now fetched by id, with
   **deliberately no semantic fallback**: wrong-but-plausible text would be
   described by the model and approved by a human.
6. **Lexicon read its JSON via `__file__`,** which has no filesystem path inside
   a zipapp — pronunciation would have silently resolved to zero replacements.
7. **A commented-out example URL parsed as a real watched channel.**
8. **One unreadable registry aborted the whole daily run.** A cron job that dies
   on a half-written profile produces no digest at all.
9. **`--doctor` was documented three times but never registered as a flag.**
10. **A guessed channel handle was wrong** (`@CompanyMan` vs `@companyman114`),
    which is why the watch list now prefers raw `UC…` ids.

---

## 6. Known limits (accepted, not bugs)

- **Cross-language topics only partly link.** Grouping is keyword-based, so RU
  and EN connect through Latin tokens only: `GPT-5`/`Nvidia` link,
  `сверхпроводник`/`superconductor` do not. Named things are caught, described
  ones are missed. Closing it needs translation or embeddings at the grouping
  step.
- **No RU channels in the watch list.** Searching found individual RU videos on
  the beat but no channel covering it regularly, and seeding from one viral
  video would watch a channel that may never post on the topic again. The
  operator must supply 3–5.
- **RU/EN pacing factor is a working figure.** `estimate_beat_duration()`
  assumes Russian runs 8% slower at equal word counts. Recalibrate against real
  ElevenLabs output.
- **The registry keeps ten videos per channel.** If a channel posts more than
  ten between runs, some are already outside the window; the digest reports
  this rather than under-counting silently.

---

## 7. Open decisions

- **`corporate_collapse` — still live?** The vocabulary is kept. If the concept
  is active it needs its own registry file and an entry in `REGISTRY_DOMAINS`.
- **MuseTalk licence.** README and HuggingFace disagree; the recorded policy
  assumes the permissive reading. Nothing depends on it — SadTalker is the
  default and is Apache 2.0 with the non-commercial restriction removed
  (`README.md:42`). `AvatarEngine` is a protocol so this can be settled later.
- **The next layer after the digest.** `daily_topic_signals.json` already is the
  topic-signal output. Whether `status_changes.json` should also drive automatic
  editorial status transitions is unresolved — currently it reports, never acts.

---

## 8. Outstanding security actions

Three credentials were exposed in chat during this work. All should be treated
as compromised regardless of whether they were used:

- **Two YouTube OAuth refresh tokens** (RU and EN analytics). Revoke at
  <https://myaccount.google.com/permissions> and reissue. Refresh tokens do not
  expire on their own, and rotating the client secret does **not** invalidate
  them.
- **A probable root password** for `84.247.137.69`. Change it.

Standing rules: secrets live on the server as 0600 env files, never in the
repository, never in chat. `report` was built so diagnostics can be shared
without any of them — it reports `set`/`unset` and never reads a value.

---

## 9. Environment constraints for the next session

- **There is no route to the server** from the Claude Code environment. The
  egress gateway rejects CONNECT to `84.247.137.69` on every port tried (22,
  443, 27522); there is no ssh client. This is the organization's network
  policy, chosen when the environment was created, and it must not be worked
  around. See <https://code.claude.com/docs/en/claude-code-on-the-web>.
- **The working channel is the repository.** Code goes out through it; the
  server's `report` output comes back through it (commit to a branch) or by
  paste. This session confirmed both directions work.
- **Do not put handoff documents on a local Mac path.** The remote session
  cannot read them. That is why this file is here.
