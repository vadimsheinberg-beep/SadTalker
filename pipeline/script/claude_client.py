"""Minimal Anthropic Messages API client.

Dependency-free on purpose: the control plane containers are slim and this
needs exactly one endpoint. The API key lives in
``/opt/tzoar/deploy/secrets/claude.env``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..config import SECRETS, Secrets

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-5"


class ClaudeError(RuntimeError):
    pass


class ClaudeClient:
    def __init__(
        self,
        secrets: Secrets | None = None,
        model: str | None = None,
        timeout: int = 120,
    ) -> None:
        self._secrets = secrets or SECRETS
        self._model = model
        self._timeout = timeout

    @property
    def model(self) -> str:
        if self._model:
            return self._model
        return self._secrets.get("claude", "CLAUDE_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL

    def complete(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system

        request = urllib.request.Request(
            API_URL,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "x-api-key": self._secrets.require("claude", "ANTHROPIC_API_KEY"),
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise ClaudeError(f"Anthropic API {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ClaudeError(f"Anthropic API unreachable: {exc}") from exc

        blocks = [
            block.get("text", "")
            for block in body.get("content", [])
            if block.get("type") == "text"
        ]
        if not blocks:
            raise ClaudeError(f"no text in response: {body.get('stop_reason')}")
        return "".join(blocks).strip()

    def complete_json(self, prompt: str, system: str = "", **kwargs: Any) -> Any:
        """Complete and parse JSON, tolerating a fenced code block wrapper."""
        text = self.complete(prompt, system=system, **kwargs)
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
            if cleaned.rstrip().endswith("```"):
                cleaned = cleaned.rstrip()[: -3].rstrip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ClaudeError(f"expected JSON, got: {text[:300]}") from exc
