"""The watch list: which channels the scout reads to find topics.

Two different sets of channels exist in this system and confusing them breaks
the scout quietly rather than loudly:

* **Watch list** (this module) -- channels the scout *reads* to learn what
  YouTube is currently promoting in the corporate-collapse domain. These are
  other people's channels. Nothing is ever published to them.
* **Publication targets** (:class:`pipeline.contracts.Channel`) -- TAMHA and
  Iahalom, the two channels we *write* to. Their own analytics come from
  :mod:`pipeline.scout.youtube_analytics`.

Putting our own two channels in the watch list would make the scout rank our
own back catalogue as the week's trends.

The registry file accepts whatever form is convenient to paste -- a handle, a
full URL, or a raw ``UC...`` id -- because that is what a human actually has
when they are looking at a channel page.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters for typing
    from .youtube_data import YouTubeDataClient

_CHANNEL_ID_RE = re.compile(r"^UC[\w-]{22}$")
_HANDLE_RE = re.compile(r"^@[\w.\-]{3,30}$")


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Entry:
    """One line of the registry, before resolution."""

    raw: str
    handle: str = ""
    channel_id: str = ""
    note: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.channel_id)


def parse_entry(line: str) -> Entry | None:
    """Interpret one registry line. Returns ``None`` for blanks and comments."""
    text = line.strip()
    if not text or text.startswith("#"):
        return None

    note = ""
    if "#" in text:
        text, _, note = text.partition("#")
        text = text.strip()
        note = note.strip()

    token = text.split()[0] if text.split() else ""
    if not token:
        return None

    if _CHANNEL_ID_RE.match(token):
        return Entry(raw=token, channel_id=token, note=note)

    handle = _handle_from(token)
    if handle:
        return Entry(raw=token, handle=handle, note=note)

    raise RegistryError(
        f"cannot interpret registry line {line.strip()!r}. Use a UC... channel "
        "id, an @handle, or a youtube.com channel URL."
    )


def _handle_from(token: str) -> str:
    """Pull an @handle out of a bare handle or any youtube.com URL form."""
    if _HANDLE_RE.match(token):
        return token

    if "youtube.com" not in token:
        return ""

    path = token.split("youtube.com", 1)[1].split("?", 1)[0].strip("/")
    if not path:
        return ""

    first = path.split("/")[0]
    if first.startswith("@") and _HANDLE_RE.match(first):
        return first
    if first == "channel":
        parts = path.split("/")
        if len(parts) > 1 and _CHANNEL_ID_RE.match(parts[1]):
            return ""  # a /channel/UC... URL; caller re-parses the id directly
    return ""


def parse_registry(text: str) -> list[Entry]:
    entries: list[Entry] = []
    for line in text.splitlines():
        stripped = line.strip()
        # /channel/UC... URLs carry the id inline; take it without an API call.
        if "youtube.com/channel/" in stripped:
            candidate = stripped.split("youtube.com/channel/", 1)[1]
            candidate = candidate.split("?", 1)[0].split("/", 1)[0]
            if _CHANNEL_ID_RE.match(candidate):
                entries.append(Entry(raw=stripped, channel_id=candidate))
                continue
        entry = parse_entry(line)
        if entry is not None:
            entries.append(entry)
    return entries


def load(path: Path) -> list[Entry]:
    path = Path(path)
    if not path.exists():
        raise RegistryError(
            f"channel registry not found at {path}. One channel per line: "
            "an @handle, a channel URL, or a UC... id."
        )
    return parse_registry(path.read_text(encoding="utf-8"))


class HandleCache:
    """Persistent handle -> channel id map.

    Resolution costs a Data API call per handle, and the mapping never changes
    for a given handle, so it is written next to the registry and reused.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._map: dict[str, str] = {}
        if self.path.exists():
            try:
                self._map = dict(json.loads(self.path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, TypeError, ValueError):
                self._map = {}

    def get(self, handle: str) -> str:
        return self._map.get(handle.casefold(), "")

    def put(self, handle: str, channel_id: str) -> None:
        self._map[handle.casefold()] = channel_id

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._map, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def resolve(
    entries: Iterable[Entry],
    client: "YouTubeDataClient",
    cache: HandleCache | None = None,
) -> tuple[list[str], list[str]]:
    """Resolve entries to channel ids.

    Returns ``(channel_ids, problems)``. An unresolvable handle is reported
    rather than raised: one dead channel in the watch list should not stop the
    scout from reading the other twenty.
    """
    channel_ids: list[str] = []
    problems: list[str] = []
    seen: set[str] = set()

    for entry in entries:
        channel_id = entry.channel_id
        if not channel_id and entry.handle:
            if cache is not None:
                channel_id = cache.get(entry.handle)
            if not channel_id:
                try:
                    channel_id = client.channel_id_for_handle(entry.handle)
                except Exception as exc:  # noqa: BLE001 - reported, not raised
                    problems.append(f"{entry.raw}: {exc}")
                    continue
                if channel_id and cache is not None:
                    cache.put(entry.handle, channel_id)
        if not channel_id:
            problems.append(f"{entry.raw}: no channel found")
            continue
        if channel_id not in seen:
            seen.add(channel_id)
            channel_ids.append(channel_id)

    if cache is not None:
        cache.flush()
    return channel_ids, problems
