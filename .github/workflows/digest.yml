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


def trigger_workflow(chat_id: str) -> tuple[bool, str]:
    """Запускает workflow HR Digest с input chat_id. Возвращает (успех, сообщение об ошибке)."""
    owner, repo = GITHUB_REPO.strip().split("/", 1)
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/digest.yml/dispatches"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    ref = os.environ.get("GITHUB_REF", "main")
    body_with_inputs = {"ref": ref, "inputs": {"chat_id": chat_id}}
    body_no_inputs = {"ref": ref}
    try:
        r = requests.post(url, headers=headers, json=body_with_inputs, timeout=15)
        if r.status_code == 204:
            return True, ""
        err = r.text[:500]
        # Если на GitHub старая версия workflow без inputs — запускаем без chat_id (дайджест уйдёт в CHAT_ID из секретов)
        if r.status_code == 422 and "Unexpected inputs" in err:
            print("Workflow без input chat_id, запускаю без inputs (дайджест в CHAT_ID из секретов)")
            r = requests.post(url, headers=headers, json=body_no_inputs, timeout=15)
            if r.status_code == 204:
                return True, ""
            err = r.text[:500]
        print("GitHub API error:", r.status_code, err)
        if r.status_code == 404:
            return False, "404: репозиторий или workflow не найден. Проверь GITHUB_REPO (логин/репо) и что ветка " + ref + " есть. Если ветка master — задай GITHUB_REF=master"
        if r.status_code == 401:
            return False, "401: неверный или истёкший GITHUB_TOKEN"
        if r.status_code == 403:
            return False, "403: у токена нет прав. Нужен scope repo или workflow (classic) / Actions: Write (fine-grained)"
        if r.status_code == 422:
            try:
                data = r.json()
                msg = data.get("message", "")
                errs = data.get("errors", [])
                if errs:
                    msg = msg + " " + str(errs[0])
                if "Reference not found" in msg or "ref" in msg.lower():
                    return False, f"422: Ветка «{ref}» не найдена. Если в репо ветка master, задай: export GITHUB_REF=master"
                return False, "422: " + (msg or err[:200])
            except Exception:
                pass
            return False, "422: Ошибка запроса (ветка ref или inputs). Проверь: в репо есть ветка main (или задай GITHUB_REF=master), файл .github/workflows/digest.yml загружен."
        return False, f"GitHub ответил {r.status_code}. В терминале полный текст ошибки."
    except Exception as e:
        print("GitHub trigger error:", e)
        return False, str(e)


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
                    send_telegram("Собираю дайджест… Через 1–2 минуты придёт сообщение.", chat_id)
                    ok, err_msg = trigger_workflow(chat_id)
                    if ok:
                        send_telegram("Дайджест запущен. Ожидайте сообщение в этом чате.", chat_id)
                    else:
                        send_telegram("Не удалось запустить дайджест. " + (err_msg or "Проверьте GITHUB_TOKEN и GITHUB_REPO."), chat_id)
        except Exception as e:
            print("Poll error:", e)
            time.sleep(10)


if __name__ == "__main__":
    main()
