#!/usr/bin/env python3
"""
Забирает новые посты указанного X-аккаунта (включая репосты и цитаты
других пользователей), переводит их на русский и публикует в Telegram-
группу/канал. Под каждым постом указывает, сколько постов было
опубликовано за текущие сутки (с 00:00 по времени ET).

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
MAX_RESULTS = 50


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


def fetch_new_tweets(user_id: str, since_id: str | None) -> tuple[list[dict], dict, dict]:
    """
    Возвращает (tweets, tweets_by_id, users_by_id):
      - tweets: список новых твитов (от старых к новым), включая репосты
        и цитаты; исключены только ответы (replies).
      - tweets_by_id: словарь id -> твит для всех "включённых" (referenced)
        твитов -- то есть оригиналов, которые репостнули/процитировали.
      - users_by_id: словарь id -> username для авторов этих оригиналов.
    """
    url = f"{X_API_BASE}/users/{user_id}/tweets"
    params = {
        "max_results": MAX_RESULTS,
        "exclude": "replies",  # репосты (retweets) больше НЕ исключаем
        "tweet.fields": "created_at,text,referenced_tweets",
        "expansions": "referenced_tweets.id,referenced_tweets.id.author_id",
        "user.fields": "username",
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
    tweets.reverse()  # API отдаёт от новых к старым — разворачиваем

    includes = data.get("includes", {})
    tweets_by_id = {t["id"]: t for t in includes.get("tweets", [])}
    users_by_id = {u["id"]: u["username"] for u in includes.get("users", [])}

    return tweets, tweets_by_id, users_by_id


def translate_to_ru(text: str) -> str:
    if not text:
        return text
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
            # Отключаем автопревью: мы сами вставляем переведённый текст
            # цитируемого/репостнутого твита, второй (непереведённый)
            # превью-блок от Telegram не нужен.
            "disable_web_page_preview": "true",
        },
        timeout=30,
    )
    if not resp.ok:
        print(f"Ошибка Telegram API: {resp.status_code} {resp.text}", file=sys.stderr)
    resp.raise_for_status()


def build_message(
    tweet: dict,
    tweets_by_id: dict,
    users_by_id: dict,
    count_today: int,
) -> str:
    """
    Формирует текст сообщения для Telegram в зависимости от типа поста:
    обычный пост, репост (retweet) или цитата (quote).
    """
    ref_list = tweet.get("referenced_tweets") or []
    ref_type = ref_list[0]["type"] if ref_list else None
    ref_id = ref_list[0]["id"] if ref_list else None
    ref_tweet = tweets_by_id.get(ref_id) if ref_id else None
    ref_author = users_by_id.get(ref_tweet.get("author_id")) if ref_tweet else None

    if ref_type == "retweeted" and ref_tweet:
        # Классический репост: показываем переведённый оригинал автора,
        # а не собственный текст твита ("RT @user: ...")
        original_text = ref_tweet.get("text", "")
        ru_text = translate_to_ru(original_text)
        header = f"🔁 <b>Репост от @{ref_author or '?'}</b>"
        body = f"{header}\n\n{ru_text}"
        link = (
            f"https://x.com/{ref_author}/status/{ref_id}"
            if ref_author
            else f"https://x.com/{X_USERNAME}/status/{tweet['id']}"
        )

    elif ref_type == "quoted" and ref_tweet:
        # Цитата: переводим и комментарий Маска, и текст процитированного поста
        own_ru = translate_to_ru(tweet.get("text", ""))
        quoted_ru = translate_to_ru(ref_tweet.get("text", ""))
        body = (
            f"{own_ru}\n\n"
            f"💬 <b>Цитата поста @{ref_author or '?'}:</b>\n{quoted_ru}"
        )
        link = f"https://x.com/{X_USERNAME}/status/{tweet['id']}"

    else:
        # Обычный оригинальный пост
        body = translate_to_ru(tweet.get("text", ""))
        link = f"https://x.com/{X_USERNAME}/status/{tweet['id']}"

    return (
        f"{body}\n\n"
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

    tweets, tweets_by_id, users_by_id = fetch_new_tweets(
        state["user_id"], state.get("last_id")
    )

    if not tweets:
        print("Новых твитов нет.")
        save_state(state)  # на случай, если сбросили счётчик по дате
        return

    for tweet in tweets:
        state["count"] += 1
        message = build_message(tweet, tweets_by_id, users_by_id, state["count"])
        send_to_telegram(message)
        state["last_id"] = tweet["id"]
        save_state(state)  # сохраняем после каждого поста, чтобы не потерять прогресс
        time.sleep(1)  # не спамим Telegram API

    print(f"Опубликовано новых постов: {len(tweets)}. Всего за сутки: {state['count']}")


if __name__ == "__main__":
    main()
