"""Сменные GPU-бэкенды. Провайдер меняется строкой в конфиге, не переписыванием.

Цены снимаются в момент запуска и пишутся в журнал — собственная таблица цен
надёжнее любого стороннего сравнения.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from .config import settings


@dataclass
class RenderRequest:
    kind: str            # tts | lipsync | asr | infinitetalk | post
    payload: dict


@dataclass
class RenderResult:
    ok: bool
    artifact_key: str = ""
    compute_s: float = 0.0
    queue_s: float = 0.0
    error: str = ""
    raw: dict | None = None


class Backend(Protocol):
    name: str

    def price_per_hour(self) -> float: ...
    def run(self, req: RenderRequest, timeout: float = 1800) -> RenderResult: ...
    def health(self) -> bool: ...


class HttpGpuNode:
    """Общий случай: GPU-нода поднята из docker-compose.gpu.yml и отвечает по HTTP.

    Подходит для Contabo cloud GPU, RunPod Pod, Vast.ai — везде, где мы сами
    владеем машиной. Отличается только базовым URL и ценой часа.
    """

    def __init__(self, name: str, base_url: str, price: float) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._price = price

    def price_per_hour(self) -> float:
        return self._price

    def health(self) -> bool:
        try:
            r = httpx.get(f"{self.base_url}/health", timeout=5)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    def run(self, req: RenderRequest, timeout: float = 1800) -> RenderResult:
        started = time.monotonic()
        try:
            r = httpx.post(f"{self.base_url}/{req.kind}", json=req.payload, timeout=timeout)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as exc:
            return RenderResult(ok=False, error=str(exc), compute_s=time.monotonic() - started)
        return RenderResult(
            ok=True,
            artifact_key=data.get("artifact_key", ""),
            compute_s=data.get("compute_s", time.monotonic() - started),
            queue_s=data.get("queue_s", 0.0),
            raw=data,
        )


class NullBackend:
    """Заглушка для control plane без GPU: конвейер доходит до стадии рендера
    и честно останавливается с блокером вместо тихой имитации работы."""

    name = "null"

    def price_per_hour(self) -> float:
        return 0.0

    def health(self) -> bool:
        return False

    def run(self, req: RenderRequest, timeout: float = 1800) -> RenderResult:
        return RenderResult(ok=False, error="GPU backend не настроен: задайте GPU_NODE_URL")


def get_backend() -> Backend:
    s = settings()
    if s.gpu_backend in {"contabo", "runpod", "vast", "local"}:
        node = HttpGpuNode(s.gpu_backend, s.gpu_node_url, s.gpu_price_per_hour)
        return node if node.health() else NullBackend()
    return NullBackend()


def cost_usd(price_per_hour: float, compute_s: float, queue_s: float = 0.0) -> float:
    return round(price_per_hour * (compute_s + queue_s) / 3600.0, 6)
