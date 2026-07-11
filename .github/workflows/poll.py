#!/usr/bin/env python3
"""
Забирает новые посты указанного X-аккаунта, переводит их на русский
и публикует в Telegram-группу/канал. Под каждым постом указывает,
сколько постов было опубликовано за текущие сутки (с 00:00 по времени ET).

Запускается по расписанию (например, каждые 5 минут) через GitHub Actions.
Состояние (id последнего обработанного твита + счётчик за сутки)
хранится в state.json и коммитится обратно в репозиторий воркфлоу.
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from deep_translator import GoogleTranslator

# ---------- Настройки ----------

X_USERNAME = os.environ.get("X_USERNAME", "elonmusk")
X_BEARER_TOKEN = os.environ["X_BEARER_TOKEN"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

STATE_FILE = Path(__file__).parent / "state.json"
ET_ZONE = ZoneInfo("America/New_York")

X_API_BASE = "https://api.x.com/2"

# Сколько твитов максимум забирать за один прогон (лимит API 5-100)
MAX_RESULTS = 20


# ---------- Вспомогательные функции ----------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"last_id": None, "day": None, "count": 0, "user_id": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def today_et() -> str:
    return datetime.now(ET_ZONE).strftime("%Y-%m-%d")


def get_user_id(username: str) -> str:
    """Резолвим username -> user_id один раз (и кэшируем в state)."""
    url = f"{X_API_BASE}/users/by/username/{username}"
    resp = requests.get(
        url, headers={"Authorization": f"Bearer {X_BEARER_TOKEN}"}, timeout=30
    )
    resp.raise_for_status()
    return resp.json()["data"]["id"]


def fetch_new_tweets(user_id: str, since_id: str | None) -> list[dict]:
    """
    Возвращает список новых твитов (от старых к новым), исключая
    ретвиты и ответы -- то есть только оригинальные посты и цитаты.
    """
    url = f"{X_API_BASE}/users/{user_id}/tweets"
    params = {
        "max_results": MAX_RESULTS,
        "exclude": "retweets,replies",
        "tweet.fields": "created_at,text",
    }
    if since_id:
        params["since_id"] = since_id

    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {X_BEARER_TOKEN}"},
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    tweets = data.get("data", [])
    # API отдаёт от новых к старым — разворачиваем, чтобы постить по порядку
    tweets.reverse()
    return tweets


def translate_to_ru(text: str) -> str:
    try:
        return GoogleTranslator(source="en", target="ru").translate(text)
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка перевода, отправляю оригинал: {exc}", file=sys.stderr)
        return text


def send_to_telegram(message: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
        },
        timeout=30,
    )
    if not resp.ok:
        print(f"Ошибка Telegram API: {resp.status_code} {resp.text}", file=sys.stderr)
    resp.raise_for_status()


def format_message(username: str, tweet_id: str, ru_text: str, count_today: int) -> str:
    link = f"https://x.com/{username}/status/{tweet_id}"
    return (
        f"{ru_text}\n\n"
        f"🔗 <a href=\"{link}\">оригинал</a>\n"
        f"📊 Постов за сутки (с 00:00 ET): <b>{count_today}</b>"
    )


# ---------- Основная логика ----------

def main() -> None:
    state = load_state()

    # Резолвим user_id один раз и кэшируем
    if not state.get("user_id"):
        state["user_id"] = get_user_id(X_USERNAME)
        save_state(state)

    # Сброс счётчика при смене суток по ET
    current_day = today_et()
    if state.get("day") != current_day:
        state["day"] = current_day
        state["count"] = 0

    tweets = fetch_new_tweets(state["user_id"], state.get("last_id"))

    if not tweets:
        print("Новых твитов нет.")
        save_state(state)  # на случай, если сбросили счётчик по дате
        return

    for tweet in tweets:
        state["count"] += 1
        ru_text = translate_to_ru(tweet["text"])
        message = format_message(
            X_USERNAME, tweet["id"], ru_text, state["count"]
        )
        send_to_telegram(message)
        state["last_id"] = tweet["id"]
        save_state(state)  # сохраняем после каждого поста, чтобы не потерять прогресс
        time.sleep(1)  # не спамим Telegram API

    print(f"Опубликовано новых постов: {len(tweets)}. Всего за сутки: {state['count']}")


if __name__ == "__main__":
    main()
