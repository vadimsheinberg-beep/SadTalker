"""One self-contained diagnostic report, safe to copy off the server.

Everything needed to correct the registry field aliases and to see what is
still missing, gathered by a single command so the loop is one round trip
instead of five.

**Nothing secret leaves.** Env files are reported as variable *names* with
"set" or "unset" — never a value, never a prefix, never a length. The registry
probe prints field *names* and aggregate counts, not channel identities,
subscriber numbers or video titles. That is a deliberate constraint of this
module, not a convention: the report is meant to be pasted into a chat window
or pushed to a branch, and anything that could not survive that does not belong
in it.
"""

from __future__ import annotations

import json
import platform
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ENV_FILES, SETTINGS, parse_env_file
from .scout import channel_registry

# Variables each bundle is expected to carry. Presence only is ever reported.
EXPECTED_SECRETS: dict[str, tuple[str, ...]] = {
    "ytdata": ("YT_DATA_API_KEY_1", "YT_DATA_API_KEY_2"),
    "analytics": (
        "YT_ANALYTICS_CLIENT_ID",
        "YT_ANALYTICS_CLIENT_SECRET",
        "YT_ANALYTICS_REFRESH_TOKEN_RU",
        "YT_ANALYTICS_REFRESH_TOKEN_EN",
    ),
    "claude": ("ANTHROPIC_API_KEY",),
    "telegram": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
    "elevenlabs": (
        "ELEVENLABS_API_KEY",
        "ELEVENLABS_VOICE_ID_RU",
        "ELEVENLABS_VOICE_ID_EN",
    ),
    "rag": ("AZ_SEARCH_API_KEY",),
}


@dataclass
class Section:
    title: str
    lines: list[str]

    def render(self) -> str:
        body = "\n".join(f"  {line}" for line in self.lines) or "  (nothing)"
        return f"## {self.title}\n{body}"


def environment_section() -> Section:
    lines = [
        f"python           {sys.version.split()[0]}",
        f"platform         {platform.platform()}",
        f"executable       {sys.executable}",
    ]
    for tool in ("ffmpeg", "ffprobe", "rsvg-convert", "inkscape", "chromium"):
        found = shutil.which(tool)
        lines.append(f"{tool:<16} {found or 'NOT FOUND'}")
    return Section("Environment", lines)


def secrets_section() -> Section:
    """Which credentials exist. Values are never read into the report."""
    lines: list[str] = []
    for bundle, names in EXPECTED_SECRETS.items():
        path = ENV_FILES.get(bundle)
        if path is None:
            continue
        exists = path.exists()
        mode = ""
        if exists:
            try:
                mode = f" mode={oct(path.stat().st_mode & 0o777)}"
            except OSError:
                mode = ""
        lines.append(f"{bundle:<12} {path} {'present' if exists else 'MISSING'}{mode}")
        values = parse_env_file(path) if exists else {}
        for name in names:
            state = "set" if values.get(name) else "unset"
            lines.append(f"    {name:<32} {state}")
    return Section("Secrets (presence only — no values are read)", lines)


def registry_files_section(directory: Path) -> Section:
    lines = [f"directory        {directory}"]
    if not directory.exists():
        lines.append("DIRECTORY DOES NOT EXIST")
        return Section("Registry files", lines)
    for name in channel_registry.REGISTRY_FILES:
        path = directory / name
        if path.exists():
            lines.append(f"{name:<28} {path.stat().st_size:>10,} bytes")
        else:
            lines.append(f"{name:<28} MISSING")
    extra = sorted(
        p.name for p in directory.glob("*.json")
        if p.name not in channel_registry.REGISTRY_FILES
    )
    if extra:
        lines.append(f"other json here: {', '.join(extra[:10])}")
    return Section("Registry files", lines)


def schema_section(directory: Path) -> Section:
    """Field *names* found in each registry, so aliases can be corrected.

    This is the payload the whole report exists for. No values are emitted —
    only the keys — which is enough to fix ``channel_registry.ALIASES`` and
    cannot expose channel data.
    """
    lines: list[str] = []
    for name in channel_registry.REGISTRY_FILES:
        path = directory / name
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            lines.append(f"{name}: UNREADABLE — {exc}")
            continue

        lines.append(f"{name}:")
        if isinstance(payload, dict):
            lines.append(f"    top-level keys : {sorted(payload)[:12]}")
        else:
            lines.append(f"    top-level      : list[{len(payload)}]")

        records = channel_registry._records_in(payload)
        lines.append(f"    records found  : {len(records)}")
        if not records:
            lines.append("    NO RECORDS — _records_in() does not know this shape")
            continue

        first = records[0]
        lines.append(f"    channel fields : {sorted(first)}")
        for key, value in first.items():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                lines.append(f"    video list key : {key!r}")
                lines.append(f"    video fields   : {sorted(value[0])}")
                break
        else:
            lines.append("    video list     : none found on the first record")
    return Section("Registry schema (field names only)", lines)


def alias_match_section(directory: Path) -> Section:
    """Which alias tables actually resolved, named field by field.

    ``registry --doctor`` says *that* something is unresolved; this says
    *which*, so a fix is one edit rather than a search.
    """
    lines: list[str] = []
    for name in channel_registry.REGISTRY_FILES:
        path = directory / name
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            records = channel_registry._records_in(payload)
        except (json.JSONDecodeError, OSError):
            continue
        if not records:
            continue

        first = records[0]
        lines.append(f"{name}:")
        for field, names in channel_registry.ALIASES.items():
            hit = next((n for n in names if n in first), None)
            lines.append(
                f"    {field:<16} {'→ ' + hit if hit else 'UNMATCHED  tried: ' + ', '.join(names)}"
            )
        break  # one file is enough; they share a writer
    return Section("Alias resolution", lines)


def doctor_section(directory: Path) -> Section:
    problems: list[str] = []
    try:
        records = channel_registry.load_dir(directory, problems=problems)
    except channel_registry.RegistryError as exc:
        return Section("Doctor", [f"FAILED: {exc}"])
    lines = channel_registry.describe_registry(records).splitlines()
    lines += [f"PROBLEM: {p}" for p in problems]
    return Section("Doctor", lines)


def inventory_section() -> Section:
    from .atlas import approval_telegram, inventory

    path = SETTINGS.workdir / "inventory.json"
    if not path.exists():
        return Section(
            "Atlas inventory",
            [f"{path} not present — nothing verified, every package refused"],
        )
    store = inventory.JsonInventoryStore(path)
    lines = [approval_telegram.progress(store)]
    ready = [c for c in store.all() if not c.verified and not c.blocking_reasons()]
    if ready:
        lines.append(f"{len(ready)} clusters complete, waiting only on human /ok")
    return Section("Atlas inventory", lines)


def build_report(registry_dir: Path | None = None) -> str:
    directory = Path(registry_dir or channel_registry.REGISTRY_DIR)
    sections = [
        environment_section(),
        registry_files_section(directory),
        schema_section(directory),
        alias_match_section(directory),
        doctor_section(directory),
        secrets_section(),
        inventory_section(),
    ]
    header = [
        "# Tzoar pipeline — server diagnostic",
        "",
        f"generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "Safe to share: contains field names, counts and presence flags only.",
        "No credential values, channel identities or video titles are included.",
        "",
    ]
    return "\n".join(header) + "\n\n".join(section.render() for section in sections) + "\n"
