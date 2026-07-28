"""Загрузка на YouTube с обязательным раскрытием синтеза.

Два факта определяют устройство модуля:

1. Все API-проекты, созданные после 28.07.2020, до прохождения аудита Google
   загружают видео принудительно приватными. Поэтому загрузка запрещена, пока
   в channels.yaml не выставлен api_project_audited — иначе конвейер молча
   зальёт сотни приватных роликов и создаст видимость работы.

2. Раскрытие изменённого/синтетического контента ставится программно через
   status.containsSyntheticMedia (поле в API с 30.10.2024). Пример A/S-контента
   в документации Google — «видео, где реальный человек выглядит так, будто
   сказал то, чего не говорил», то есть ровно наш лип-синк по B-roll автора.
   Флаг обязателен и не отключается конфигом.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

CONFIG = Path(__file__).with_name("channels.yaml")


class PublishBlocked(RuntimeError):
    """Публикация остановлена политикой, а не технической ошибкой."""


@dataclass
class UploadRequest:
    channel: str          # ru | en
    video_path: Path
    title: str
    description: str
    tags: list[str]
    short_id: int
    policy_passed: bool
    disclosure_line: str


def config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def preflight(req: UploadRequest, cfg: dict | None = None) -> list[str]:
    """Все причины, по которым загружать нельзя. Пустой список — можно."""
    cfg = cfg or config()
    ch = cfg["channels"].get(req.channel)
    problems: list[str] = []

    if ch is None:
        return [f"канал {req.channel} не описан в channels.yaml"]

    if not req.policy_passed:
        problems.append("ролик не прошёл валидаторы политики")

    if not ch["api_project_audited"] and not ch["allow_private_uploads"]:
        problems.append(
            "API-проект не прошёл аудит Google: видео будет заблокировано как приватное. "
            "Пройдите аудит либо явно разрешите allow_private_uploads для теста"
        )

    secret = os.environ.get(ch["client_secret_env"], "")
    token = os.environ.get(ch["refresh_token_env"], "")
    if not secret:
        problems.append(f"не задан {ch['client_secret_env']}")
    if not token:
        problems.append(f"не задан {ch['refresh_token_env']} — выполните authorize.py")

    if not req.video_path.exists():
        problems.append(f"файл не найден: {req.video_path}")

    if req.disclosure_line and req.disclosure_line not in req.description:
        problems.append("в описании отсутствует строка раскрытия синтеза")

    return problems


def build_body(req: UploadRequest, cfg: dict | None = None) -> dict:
    """Тело videos.insert. Раскрытие проставляется здесь и не зависит от вызывающего."""
    cfg = cfg or config()
    d = cfg["upload_defaults"]
    ch = cfg["channels"][req.channel]

    description = req.description
    if req.disclosure_line and req.disclosure_line not in description:
        description = f"{description}\n\n{req.disclosure_line}"

    return {
        "snippet": {
            "title": req.title[:100],
            "description": description[:5000],
            "tags": req.tags[:15],
            "categoryId": d["category_id"],
            "defaultLanguage": ch["language"],
            "defaultAudioLanguage": ch["language"],
        },
        "status": {
            "privacyStatus": d["privacy_status"],
            "selfDeclaredMadeForKids": d["self_declared_made_for_kids"],
            # Раскрытие изменённого/синтетического контента. Не конфигурируется.
            "containsSyntheticMedia": True,
        },
    }


def upload(req: UploadRequest) -> dict:
    cfg = config()
    problems = preflight(req, cfg)
    if problems:
        raise PublishBlocked("; ".join(problems))

    # Импорт внутри функции: control plane должен работать и без google-клиента
    from google.auth.transport.requests import Request  # noqa: F401
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    ch = cfg["channels"][req.channel]
    creds = Credentials(
        token=None,
        refresh_token=os.environ[ch["refresh_token_env"]],
        client_id=ch["client_id"],
        client_secret=os.environ[ch["client_secret_env"]],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=cfg["oauth"]["scopes"],
    )
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    media = MediaFileUpload(str(req.video_path), chunksize=8 << 20, resumable=True)
    request = youtube.videos().insert(
        part="snippet,status", body=build_body(req, cfg), media_body=media
    )

    response = None
    while response is None:
        _, response = request.next_chunk()
    return response
