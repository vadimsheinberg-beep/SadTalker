#!/usr/bin/env python3
"""Получение refresh-токена для канала. Запускать НА СВОЁЙ МАШИНЕ, где есть браузер.

    pip install google-auth-oauthlib pyyaml
    YT_CLIENT_SECRET_RU=<секрет> python3 authorize.py ru

Скрипт откроет браузер, попросит войти под владельцем канала и напечатает
refresh-токен. Токен кладётся в .env на сервере и НИКОГДА не коммитится.

Важно: пока экран согласия в статусе Testing, refresh-токен протухает через
7 суток и конвейер будет останавливаться каждую неделю. Перед боевым запуском
переведите OAuth consent screen в статус In production.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

CONFIG = Path(__file__).with_name("channels.yaml")


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"ru", "en"}:
        print(__doc__)
        return 2

    channel = sys.argv[1]
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    ch = cfg["channels"][channel]

    secret = os.environ.get(ch["client_secret_env"])
    if not secret:
        print(f"задайте {ch['client_secret_env']} в окружении", file=sys.stderr)
        return 2

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": ch["client_id"],
                "client_secret": secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        },
        scopes=cfg["oauth"]["scopes"],
    )
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    print("\n" + "=" * 70)
    print(f"канал: {ch['name']}")
    print(f"добавьте в .env на сервере:\n\n{ch['refresh_token_env']}={creds.refresh_token}\n")
    print("=" * 70)
    print("Не коммитьте это значение и не присылайте в переписке.")
    if not ch["api_project_audited"]:
        print("\nВНИМАНИЕ: api_project_audited: false — до аудита Google все")
        print("загруженные видео будут принудительно приватными.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
