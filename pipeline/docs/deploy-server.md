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
- A way to get one 86 KB file onto the server — `git`, `scp` or `rsync`. See
  the three options in step 1; only the first needs GitHub access.

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

## 1. Get the code — one file, no repository

The whole repository is 72 MB (140 MB with history) and exists mostly to serve
the avatar stage. The pipeline itself is 400 KB of pure standard library, so it
ships as a **single executable file** built with Python's own `zipapp`:

```bash
python3 pipeline/build_pyz.py -o tzoar-pipeline.pyz
```

That produces an ~86 KB file that runs anywhere, needs no unpacking, no
`PYTHONPATH`, and no working directory:

```bash
./tzoar-pipeline.pyz registry --doctor --sample 10
```

Tests and docs are excluded from the archive; the data files (pronunciation
lexicons) are inside it and read through `importlib.resources`, so they work
from within the zip.

### Getting that file onto the server

Pick whichever matches how the server is set up.

**A. Build on the server from a shallow clone**, then delete the clone. Cheapest
if the server can reach GitHub at all:

```bash
cd /tmp
git clone --depth 1 -b claude/youtube-bavli-atlas-pipeline-h1qoqu \
    https://github.com/vadimsheinberg-beep/SadTalker.git
cd SadTalker && python3 pipeline/build_pyz.py -o /opt/tzoar/bin/tzoar-pipeline.pyz
cd /tmp && rm -rf SadTalker
```

`--depth 1` skips the 69 MB of history. Nothing of the repository survives.

**B. Build locally, copy one file.** If the server should not talk to GitHub at
all — you already have SSH file access:

```bash
# on your machine, in the repo
python3 pipeline/build_pyz.py -o tzoar-pipeline.pyz
scp tzoar-pipeline.pyz root@84.247.137.69:/opt/tzoar/bin/
```

**C. Copy the package directory** if you would rather see plain files than an
archive. `rsync` only what runs:

```bash
rsync -av --exclude=tests --exclude=docs --exclude=__pycache__ \
    pipeline/ root@84.247.137.69:/opt/tzoar/lib/pipeline/
```

Then run it with `cd /opt/tzoar/lib && python3 -m pipeline …`.

Updating later is the same operation as installing — rebuild and replace the
one file. There is no checkout anybody has to remember to `git pull`.

---

## 2. Confirm it runs at all

```bash
/opt/tzoar/bin/tzoar-pipeline.pyz --help
```

You should see the subcommand list: `scout, inventory, propose, review, drain,
daily, registry, build, render, avatar`.

The test suite is not inside the archive — it is a build-time check, not a
runtime one. To verify the interpreter on a machine that has the source
(option A before you delete the clone, or your own machine):

```bash
python3 -m unittest discover -s pipeline/tests -t .
```

Expect `OK` and around 164 tests in a few seconds. If this fails, stop here and
send the output; nothing below will be meaningful.

---

## 3. Run the doctor

```bash
/opt/tzoar/bin/tzoar-pipeline.pyz registry --doctor --sample 10
```

It reads the four registries, parses them, and reports what it found. It is
**read-only**: no file in `channel_registries/` is written, moved or locked.

If your registries are somewhere else:

```bash
/opt/tzoar/bin/tzoar-pipeline.pyz \
    --registry-dir /path/to/channel_registries registry --doctor --sample 10
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

Do not work around it. Run the diagnostic instead — one command that gathers
everything needed to fix it:

```bash
/opt/tzoar/bin/tzoar-pipeline.pyz report --out /tmp/tzoar-report.md
cat /tmp/tzoar-report.md
```

Paste that file back, or commit it to a branch. It reports the *names* of the
fields each registry actually uses, which alias tables matched and which did
not, the environment, and which credentials exist.

**It is built to be shareable.** Credentials are reported as `set` / `unset`
and their values are never read into it; channel titles, video titles and
channel ids are never included. Tests plant canary values in every one of those
places and assert none of them reach the output.

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
/opt/tzoar/bin/tzoar-pipeline.pyz daily --out-dir /tmp/daily-test --no-save
cat /tmp/daily-test/daily_digest.md
```

The first real run establishes the baseline and reports no new videos — there
is nothing to compare against yet. The second day is the first useful digest:

```bash
/opt/tzoar/bin/tzoar-pipeline.pyz daily
```

Defaults: artifacts to `/opt/tzoar/deploy/production/daily/`, snapshots to
`/opt/tzoar/deploy/production/daily/snapshots/`.

### In crontab

A single self-contained file is exactly what cron wants — an absolute path, no
working directory, no environment to set up. `crontab -e`:

```cron
# Tzoar daily digest. Runs after youtube-channel-monitor so it reads fresh
# metrics — check when that finishes and leave a margin.
30 7 * * * /opt/tzoar/bin/tzoar-pipeline.pyz daily >> /var/log/tzoar-daily.log 2>&1
```

Four things cron gets wrong unless you say otherwise:

- **`PATH` is minimal.** The archive's shebang is `/usr/bin/env python3`, which
  needs `python3` on the path. If cron cannot find it, either set `PATH` at the
  top of the crontab or call the interpreter explicitly:
  ```cron
  30 7 * * * /usr/bin/python3 /opt/tzoar/bin/tzoar-pipeline.pyz daily >> /var/log/tzoar-daily.log 2>&1
  ```
  This is the most common reason a crontab entry silently does nothing.
- **`%` is special in crontab** and must be escaped as `\%`. None of the
  commands here contain one — just do not add a `date +%F` without escaping it.
- **Output is mailed unless redirected.** The `>>` above keeps it in a log; the
  digest itself is a file, so the log is only for errors and the summary line.
- **The exit code matters.** `daily` returns 1 if no registry could be read.
  Worth alerting on if you have anything watching logs.

Ordering against the monitor is the one real constraint: if `daily` runs first,
it diffs yesterday's metrics against yesterday's metrics and reports nothing new.
Since cron has no dependency ordering, either leave a wide margin or chain them:

```cron
30 6 * * * /path/to/monitor && /opt/tzoar/bin/tzoar-pipeline.pyz daily >> /var/log/tzoar-daily.log 2>&1
```

If the monitor is a systemd unit (`youtube-channel-monitor.service`), prefer a
systemd timer with `After=` instead — that expresses the ordering properly:

```ini
# /etc/systemd/system/tzoar-daily.service
[Unit]
Description=Tzoar daily digest
After=youtube-channel-monitor.service

[Service]
Type=oneshot
ExecStart=/opt/tzoar/bin/tzoar-pipeline.pyz daily
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

Then `systemctl daemon-reload && systemctl enable --now tzoar-daily.timer`, and
history lands with the monitor's: `journalctl -u tzoar-daily.service`.

---

## 5. Writing a script from the digest

This is where the daily layer meets the script pipeline:

```bash
/opt/tzoar/bin/tzoar-pipeline.pyz build --channel tamha \
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
/opt/tzoar/bin/tzoar-pipeline.pyz inventory --verbose
```

---

## Command reference

| command | what it does | needs |
|---|---|---|
| `report --out f.md` | one shareable diagnostic | nothing (reports what is missing) |
| `registry --doctor` | verify the registry reader | registries (read-only) |
| `daily` | snapshot + the four artifacts | registries |
| `scout` | ranked topics to stdout | registries |
| `inventory` | what blocks each cluster | inventory store |
| `propose` | Claude drafts theme names | `claude.env`, RAG |
| `review` / `drain` | Telegram approval loop | `telegram.env` |
| `build` | claim + script + gate | inventory, `claude.env` |
| `render` | narration, slides, video | `elevenlabs.env`, ffmpeg, rsvg |
| `avatar` | talking head over slides | SadTalker checkpoints, GPU — **needs the full clone, not the .pyz** |
