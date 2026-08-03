"""Human approval of inventory over Telegram (@tamhu_bot).

The operating model is that a person approves clusters, not videos. This is the
surface where that happens: proposals go out one message at a time, replies
come back as commands, and the store moves only on an explicit human verb.

Commands (one per line, batched replies are fine):

    /ok <cluster_id>                approve as proposed
    /no <cluster_id> <reason>       reject with a reason
    /theme <cluster_id> <text>      correct the theme name
    /ru <cluster_id> <text>         correct the Russian claim
    /en <cluster_id> <text>         correct the English claim
    /skip <cluster_id>              leave for later

An edit does not approve. After ``/theme`` the cluster is still ``PROPOSED``
and still needs its own ``/ok``, so a correction can never be mistaken for a
sign-off.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from ..config import SECRETS, Secrets
from ..contracts import ClusterStatus
from .inventory import Cluster, InventoryStore, approve, reject
from .theme_proposer import Proposal

API_ROOT = "https://api.telegram.org"
_COMMAND_RE = re.compile(
    r"^/(ok|no|theme|ru|en|skip)\s+(\S+)\s*(.*)$", re.IGNORECASE | re.UNICODE
)


class TelegramError(RuntimeError):
    pass


@dataclass(frozen=True)
class Command:
    verb: str
    cluster_id: str
    argument: str


def parse_commands(text: str) -> list[Command]:
    """Parse an operator reply. Unrecognised lines are ignored, not guessed at."""
    commands: list[Command] = []
    for line in (text or "").splitlines():
        match = _COMMAND_RE.match(line.strip())
        if match:
            verb, cluster_id, argument = match.groups()
            commands.append(
                Command(verb.lower(), cluster_id.strip(), argument.strip())
            )
    return commands


def apply_command(
    store: InventoryStore, command: Command, approver: str
) -> tuple[bool, str]:
    """Apply one command. Returns ``(changed, message)`` for the reply."""
    cluster = store.get(command.cluster_id)
    if cluster is None:
        return False, f"{command.cluster_id}: unknown cluster"

    if command.verb == "ok":
        try:
            approve(store, command.cluster_id, approver)
        except ValueError as exc:
            return False, f"{command.cluster_id}: cannot approve — {exc}"
        return True, f"{command.cluster_id}: verified ✅"

    if command.verb == "no":
        if not command.argument:
            return False, f"{command.cluster_id}: /no needs a reason"
        reject(store, command.cluster_id, approver, command.argument)
        return True, f"{command.cluster_id}: rejected"

    if command.verb == "skip":
        return False, f"{command.cluster_id}: left for later"

    if not command.argument:
        return False, f"{command.cluster_id}: /{command.verb} needs text"

    field = {"theme": "theme_name", "ru": "text_ru", "en": "text_en"}[command.verb]
    setattr(cluster, field, command.argument)
    # An edit keeps the cluster in review; approval stays a separate act.
    cluster.status = ClusterStatus.PROPOSED
    store.save(cluster)
    return True, f"{command.cluster_id}: {field} updated — still needs /ok"


def apply_reply(
    store: InventoryStore, text: str, approver: str
) -> list[str]:
    return [
        apply_command(store, command, approver)[1]
        for command in parse_commands(text)
    ]


class TelegramBot:
    def __init__(self, secrets: Secrets | None = None, timeout: int = 30) -> None:
        self._secrets = secrets or SECRETS
        self._timeout = timeout

    def _call(self, method: str, **params: Any) -> dict[str, Any]:
        token = self._secrets.require("telegram", "TELEGRAM_BOT_TOKEN")
        url = f"{API_ROOT}/bot{token}/{method}"
        body = urllib.parse.urlencode(params).encode("utf-8")
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, data=body), timeout=self._timeout
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise TelegramError(f"telegram {method} failed: {exc}") from exc
        if not payload.get("ok"):
            raise TelegramError(f"telegram {method}: {payload.get('description')}")
        return payload

    @property
    def chat_id(self) -> str:
        """The operator chat.

        Telegram will not deliver to a chat that has never messaged the bot, so
        if this is unset the fix is to open t.me/tamhu_bot and press Start once.
        """
        return self._secrets.require("telegram", "TELEGRAM_CHAT_ID")

    def send(self, text: str) -> int:
        payload = self._call(
            "sendMessage",
            chat_id=self.chat_id,
            text=text[:4000],
            disable_web_page_preview="true",
        )
        return int(payload["result"]["message_id"])

    def updates(self, offset: int = 0) -> list[dict[str, Any]]:
        payload = self._call("getUpdates", offset=offset, timeout=0)
        return payload.get("result", [])


def send_batch(bot: TelegramBot, proposals: Sequence[Proposal]) -> int:
    """Send a labelling batch. Returns the number of messages delivered."""
    if not proposals:
        return 0
    bot.send(
        f"📋 Inventory review — {len(proposals)} clusters.\n"
        "Reply per cluster: /ok <id> · /no <id> reason · "
        "/theme|/ru|/en <id> text · /skip <id>\n"
        "An edit does not approve; each cluster still needs its own /ok."
    )
    for proposal in proposals:
        bot.send(proposal.render())
    return len(proposals) + 1


def drain(
    bot: TelegramBot, store: InventoryStore, approver: str, offset: int = 0
) -> tuple[int, list[str]]:
    """Read pending operator replies and apply them.

    Returns the next ``offset`` to poll from and the per-command results, so a
    caller can persist the offset and avoid reprocessing the same reply.
    """
    results: list[str] = []
    next_offset = offset
    for update in bot.updates(offset=offset):
        next_offset = max(next_offset, int(update.get("update_id", 0)) + 1)
        text = (update.get("message") or {}).get("text", "")
        results.extend(apply_reply(store, text, approver))
    if results:
        bot.send("\n".join(results[:40]))
    return next_offset, results


def progress(store: InventoryStore) -> str:
    counts: dict[str, int] = {status.value: 0 for status in ClusterStatus}
    clusters: Iterable[Cluster] = store.all()
    total = 0
    for cluster in clusters:
        counts[cluster.status.value] += 1
        total += 1
    return (
        f"inventory: {counts['verified']}/{total} verified · "
        f"{counts['proposed']} awaiting review · "
        f"{counts['draft']} not yet drafted · "
        f"{counts['rejected']} rejected"
    )
