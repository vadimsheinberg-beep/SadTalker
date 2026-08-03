# Running the pipeline on the server

Full walkthrough for the first run, ending with `registry --doctor` — the check
that must pass before any ranking is trustworthy.

Everything below runs as a normal shell command. Nothing needs `docker compose`
or `systemctl`, and nothing modifies the existing control plane, the registries,
or the monitor.

---

## 0. What this needs

- **Python 3.9 or newer.** Developed and tested on 3.11.
- **No third-party packages.** The pipeline imports only the standard library,
  so there is no `pip install`, no virtualenv and no lockfile to reconcile with
  whatever the control plane already uses.
- **Read access** to `/opt/tzoar/deploy/production/channel_registries/`.
- `git` to fetch the code.

Optional, and only for later stages — `registry --doctor` does not touch them:

- `ffmpeg` + `ffprobe` — narration and video assembly
- `librsvg2-bin` — slide rasterising (Chromium is the fallback)

Check the Python version first:

```bash
python3 --version
```

If that prints 3.8 or older, look for a newer interpreter before continuing:

```bash
ls /usr/bin/python3.* 2>/dev/null
```

and substitute it for `python3` in every command below.

---

## 1. Get the code

The pipeline lives on branch `claude/youtube-bavli-atlas-pipeline-h1qoqu` of the
SadTalker repository. Clone it somewhere that is *not* inside `deploy/`, so it
cannot be confused with deployed state:

```bash
mkdir -p /opt/tzoar/src
cd /opt/tzoar/src
git clone -b claude/youtube-bavli-atlas-pipeline-h1qoqu \
    https://github.com/vadimsheinberg-beep/SadTalker.git
cd SadTalker
```

Already cloned? Update instead:

```bash
cd /opt/tzoar/src/SadTalker
git fetch origin claude/youtube-bavli-atlas-pipeline-h1qoqu
git checkout claude/youtube-bavli-atlas-pipeline-h1qoqu
git pull origin claude/youtube-bavli-atlas-pipeline-h1qoqu
```

The repository is the full SadTalker tree; only the `pipeline/` package matters
for this. SadTalker's own model checkpoints are **not** needed for anything in
this document — they are required only for avatar rendering, much later.

---

## 2. Confirm it runs at all

`python -m pipeline` must be run **from the repository root** — that is what
puts the `pipeline` package on the import path.

```bash
cd /opt/tzoar/src/SadTalker
python3 -m pipeline --help
```

You should see the subcommand list: `scout, inventory, propose, review, drain,
daily, registry, build, render, avatar`.

Then run the test suite. It is fully offline — no network, no GPU, no
registries — so it is a clean answer to "does this interpreter run this code":

```bash
python3 -m unittest discover -s pipeline/tests -t .
```

Expect `OK` and around 158 tests in a few seconds. If this fails, stop here and
send the output; nothing below will be meaningful.

---

## 3. Run the doctor

```bash
cd /opt/tzoar/src/SadTalker
python3 -m pipeline registry --doctor --sample 10
```

It reads the four registries, parses them, and reports what it found. It is
**read-only**: no file in `channel_registries/` is written, moved or locked.

If your registries are somewhere else:

```bash
python3 -m pipeline --registry-dir /path/to/channel_registries registry --doctor --sample 10
```

### What healthy output looks like

```
channels parsed:      620
in >1 profile:        27
with subscriber count:  620
with stored videos:     620
with usable baseline:   618
usable video signals:  6100
scoutable (not excluded): 612

editorial status:
  EXCLUDE_QUALITY    2
  EXCLUDE_TOPIC      6
  REVIEW             8
  RIGHTS_CHECK       5
  UNCLASSIFIED       573
  WATCH_CORE         12
  WATCH_WEEKLY       14

first parsed channels:
  UCxxxx…  WATCH_CORE   subs=1200000  baseline=340000  videos=10  Channel name
  …
```

The numbers to check, in order of importance:

| line | expected | if wrong |
|---|---|---|
| `channels parsed` | ~620 | far lower → a registry failed to load; look for `PROBLEM:` lines |
| `in >1 profile` | 27 | 0 → the merge key is not the channel id field |
| `with subscriber count` | ~620 | **0 → the `subscribers` alias is wrong** |
| `with stored videos` | ~620 | **0 → the `videos` alias is wrong** |
| `with usable baseline` | ~620 | 0 → neither `median_top3` nor per-video views were found |
| status breakdown | 12 / 14 / 8 / 5 / 6 / 2 | all `UNCLASSIFIED` → the `status` alias is wrong |

### If it prints a WARNING

```
WARNING: no values found for subscribers, videos -- the field names in
ALIASES do not match these files. Correct them before trusting any ranking.
```

This is the case this command exists for. The registries were written by the
monitor, which could not be inspected from the development environment, so
every field is read through a list of plausible names. When none of them match,
the reader silently sees zeros — and zeros produce a ranking that *looks*
plausible and means nothing.

Do not work around it. Send me the output of:

```bash
python3 -c "
import json
d = json.load(open('/opt/tzoar/deploy/production/channel_registries/ai_science_en.json'))
print('TOP-LEVEL:', list(d)[:10] if isinstance(d, dict) else f'list[{len(d)}]')
rec = (d if isinstance(d, list) else d.get('channels') or list(d.values()))[0]
print('CHANNEL FIELDS:', list(rec))
for k in list(rec):
    if isinstance(rec[k], list) and rec[k] and isinstance(rec[k][0], dict):
        print('VIDEO FIELDS:', k, '->', list(rec[k][0]))
"
```

That prints field *names* only — no subscriber numbers, no ids, nothing
sensitive — and it is enough for me to correct the alias tables in one pass.

### If it errors instead

```
no registries loaded from /opt/… Expected: ai_science_en.json, …
```

Either the path is wrong (use `--registry-dir`) or the files are unreadable.
Check with:

```bash
ls -la /opt/tzoar/deploy/production/channel_registries/
```

---

## 4. Only after the doctor is clean

Once the numbers look right, the daily layer can run. Start with a dry run,
which writes the four artifacts but stores **no** snapshot, so it changes
nothing you have to undo:

```bash
python3 -m pipeline daily --out-dir /tmp/daily-test --no-save
cat /tmp/daily-test/daily_digest.md
```

The first real run establishes the baseline and reports no new videos — there
is nothing to compare against yet. The second day is the first useful digest:

```bash
python3 -m pipeline daily
```

Defaults: artifacts to `/opt/tzoar/deploy/production/daily/`, snapshots to
`/opt/tzoar/deploy/production/daily/snapshots/`.

### As a daily timer

Once you are happy with it, run it after the monitor finishes so it reads fresh
metrics. Both files, then `systemctl daemon-reload` and
`systemctl enable --now tzoar-daily.timer`:

```ini
# /etc/systemd/system/tzoar-daily.service
[Unit]
Description=Tzoar daily digest
After=youtube-channel-monitor.service

[Service]
Type=oneshot
WorkingDirectory=/opt/tzoar/src/SadTalker
ExecStart=/usr/bin/python3 -m pipeline daily
```

```ini
# /etc/systemd/system/tzoar-daily.timer
[Unit]
Description=Run the Tzoar daily digest

[Timer]
OnCalendar=*-*-* 07:30:00
Persistent=true

[Install]
WantedBy=timers.target
```

History, as with the monitor:

```bash
journalctl -u tzoar-daily.service
```

---

## 5. Writing a script from the digest

This is where the daily layer meets the script pipeline:

```bash
cd /opt/tzoar/src/SadTalker
python3 -m pipeline build --channel tamha \
    --from-signals /opt/tzoar/deploy/production/daily/daily_topic_signals.json \
    --topic <slug-from-the-digest>
```

Expect this to refuse today, with exit code 2:

```
refused: no verified cluster above relevance 0.55 for topic '…'
```

That is correct. All 119 atlas clusters are still `draft` with an empty
`theme_name`, so nothing is verified, so no claim can be sourced, so no video
is produced. The gate is doing its job. Unblocking it is the
`propose` → `review` → `drain` loop, which needs the Claude and Telegram
secrets in place.

Check inventory readiness at any time:

```bash
python3 -m pipeline inventory --verbose
```

---

## Command reference

| command | what it does | needs |
|---|---|---|
| `registry --doctor` | verify the registry reader | registries (read-only) |
| `daily` | snapshot + the four artifacts | registries |
| `scout` | ranked topics to stdout | registries |
| `inventory` | what blocks each cluster | inventory store |
| `propose` | Claude drafts theme names | `claude.env`, RAG |
| `review` / `drain` | Telegram approval loop | `telegram.env` |
| `build` | claim + script + gate | inventory, `claude.env` |
| `render` | narration, slides, video | `elevenlabs.env`, ffmpeg, rsvg |
| `avatar` | talking head over slides | SadTalker checkpoints, GPU |
