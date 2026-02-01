#!/usr/bin/env python3
"""
Лёгкий бот: слушает команду /digest в Telegram и запускает GitHub Actions workflow.
Дайджест собирается на серверах GitHub и приходит в чат пользователя.

Запускай этот скрипт там, где он может работать постоянно (компьютер, VPS, бесплатный хостинг).
Переменные окружения: TELEGRAM_TOKEN, GITHUB_TOKEN, GITHUB_REPO (логин/репо, например myuser/hr-news-agent).
"""

import os
import sys
import time
import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")  # например yourname/hr-news-agent

if not TELEGRAM_TOKEN:
    print("Задай TELEGRAM_TOKEN (токен бота Telegram)")
    sys.exit(1)
if not GITHUB_TOKEN:
    print("Задай GITHUB_TOKEN (Personal Access Token с правами repo или workflow)")
    sys.exit(1)
if not GITHUB_REPO or "/" not in GITHUB_REPO:
    print("Задай GITHUB_REPO в формате владелец/репозиторий, например: myuser/hr-news-agent")
    sys.exit(1)


def send_telegram(text: str, chat_id: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=15,
        )
        if not r.json().get("ok"):
            print("Telegram send error:", r.text[:300])
    except Exception as e:
        print("Telegram error:", e)


def trigger_workflow(chat_id: str) -> bool:
    """Запускает workflow HR Digest с input chat_id."""
    owner, repo = GITHUB_REPO.strip().split("/", 1)
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/digest.yml/dispatches"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    # ref — ветка, от которой запускать (обычно main или master)
    ref = os.environ.get("GITHUB_REF", "main")
    body = {"ref": ref, "inputs": {"chat_id": chat_id}}
    try:
        r = requests.post(url, headers=headers, json=body, timeout=15)
        if r.status_code == 204:
            return True
        print("GitHub API error:", r.status_code, r.text[:400])
        return False
    except Exception as e:
        print("GitHub trigger error:", e)
        return False


def main():
    get_updates_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    offset = None
    print("Триггер-бот запущен. Команда /digest или /дайджест запустит дайджест на GitHub Actions.")
    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(get_updates_url, params=params, timeout=35)
            data = r.json()
            if not data.get("ok"):
                time.sleep(5)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                text = (msg.get("text") or "").strip()
                chat_id = str(msg.get("chat", {}).get("id"))
                if text.lower().startswith("/digest") or text.strip().lower() == "/дайджест":
                    print("Команда /digest от chat_id:", chat_id)
                    send_telegram("Собираю дайджест… Запускаю на GitHub, через 1–2 минуты придёт сообщение.", chat_id)
                    if trigger_workflow(chat_id):
                        send_telegram("Дайджест запущен. Ожидайте сообщение в этом чате.", chat_id)
                    else:
                        send_telegram("Не удалось запустить дайджест. Проверьте GITHUB_TOKEN и GITHUB_REPO.", chat_id)
        except Exception as e:
            print("Poll error:", e)
            time.sleep(10)


if __name__ == "__main__":
    main()
