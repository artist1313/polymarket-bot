"""
Polymarket Signals Bot — с AI анализом через Claude
=====================================================
Мониторит Polymarket, анализирует рынки через Claude AI,
отправляет чёткие сигналы: КУПИТЬ YES / КУПИТЬ NO / ЖДАТЬ

УСТАНОВКА:
    pip install python-telegram-bot requests schedule

НАСТРОЙКА:
    Заполни секцию CONFIG ниже и запускай: python bot.py
"""

import requests
import schedule
import time
import logging
from datetime import datetime
from telegram import Bot
from telegram.constants import ParseMode
import asyncio

# ============================================================
# ⚙️ CONFIG
# ============================================================
TELEGRAM_TOKEN   = "8715431400:AAHsu9bZ78D8a42ErFdrtstLdz0BzqScPA8"
CHANNEL_ID       = "@polyreg"
OPENROUTER_KEY   = "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"
SITE_URL         = "https://polyreg.icu"
SITE_NAME        = "Polyreg"

# ============================================================
# ⚙️ ПАРАМЕТРЫ СИГНАЛОВ
# ============================================================
SIGNALS_PER_DAY       = 4      # сколько сигналов в день
CHECK_INTERVAL_MIN    = 15     # как часто проверять рынки
ODDS_CHANGE_THRESHOLD = 4.0    # минимальное движение odds для анализа (%)
MIN_VOLUME            = 50000  # минимальный объём рынка в USDC
SIGNAL_HOURS          = [9, 12, 16, 20]  # часы отправки сигналов

# ============================================================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# Хранит предыдущие значения odds
prev_odds: dict = {}
# Счётчик сигналов за день
signals_today = 0
last_signal_date = None


# ============================================================
# POLYMARKET API
# ============================================================

def fetch_markets(limit: int = 30) -> list:
    """Получает топ активных рынков по объёму"""
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": limit,
                "order": "volume24hr",
                "ascending": "false"
            },
            timeout=15
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"Polymarket API error: {e}")
        return []


def fetch_price(token_id: str) -> float | None:
    """Получает текущую цену (odds) токена"""
    try:
        r = requests.get(
            "https://clob.polymarket.com/last-trade-price",
            params={"token_id": token_id},
            timeout=10
        )
        r.raise_for_status()
        price = float(r.json().get("price", 0)) * 100
        return round(price, 1)
    except Exception:
        return None


# ============================================================
# CLAUDE AI АНАЛИЗ
# ============================================================

def ai_analyze(question: str, current_odds: float, change: float, volume: float) -> dict | None:
    """
    Отправляет данные рынка в OpenRouter и получает:
    - action: BUY_YES / BUY_NO / WAIT
    - reason: 1-2 предложения почему
    - target: целевые odds (если есть)
    """
    import json
    direction = "surged" if change > 0 else "dropped"
    prompt = f"""You are a prediction markets analyst. Give a short trading signal.

Market: {question}
Current odds (YES): {current_odds}%
Change in last hours: {direction} by {abs(change):.1f}%
Volume: ${volume:,.0f}

Reply STRICTLY in JSON format (no markdown, no extra text):
{{
  "action": "BUY_YES" | "BUY_NO" | "WAIT",
  "reason": "1-2 sentences — why the market is mispriced or why to wait",
  "target": number or null
}}

Rules:
- BUY_YES if market underestimates the event (odds too low)
- BUY_NO if market overestimates the event (odds too high)
- WAIT if odds are fair or risk/reward is unfavorable
- reason — max 20 words, specific and to the point in English
- target — expected odds after correction, or null"""

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "anthropic/claude-3-haiku",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=20
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        log.error(f"OpenRouter API error: {e}")
        return None


# ============================================================
# ФОРМАТИРОВАНИЕ СООБЩЕНИЙ
# ============================================================

ACTION_LABELS = {
    "BUY_YES": "⚡️ BUY YES",
    "BUY_NO":  "🔻 BUY NO",
    "WAIT":    "⏸ WAIT",
}

ACTION_EMOJI = {
    "BUY_YES": "📈",
    "BUY_NO":  "📉",
    "WAIT":    "🔍",
}

def format_signal(signal_num: int, question: str, current_odds: float,
                  change: float, volume: float, ai: dict, slug: str = "") -> str:
    """Форматирует финальное сообщение сигнала"""

    action     = ai.get("action", "WAIT")
    reason     = ai.get("reason", "")
    target     = ai.get("target")

    badge      = ACTION_LABELS.get(action, "⏸ WAIT")
    emoji      = ACTION_EMOJI.get(action, "🔍")
    arrow      = "▲" if change > 0 else "▼"
    vol_str    = f"${volume/1000:.0f}K" if volume < 1_000_000 else f"${volume/1_000_000:.1f}M"
    target_str = f" · target {target}%" if target else ""

    # Укорачиваем вопрос если длинный
    q = question if len(question) <= 60 else question[:57] + "..."

    msg = (
        f"{emoji} *Signal #{signal_num}* — {badge}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"*{q}*\n\n"
        f"*{current_odds}%* {arrow} {change:+.1f}%  |  {vol_str}\n\n"
        f"_{reason}_\n\n"
        f"*→ {badge}{target_str}*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"[📊 View on Polymarket](https://polymarket.com/event/{slug})"
    )
    return msg


# ============================================================
# ЛОГИКА ОТБОРА СИГНАЛОВ
# ============================================================

def find_best_signal(markets: list) -> dict | None:
    """
    Ищет лучший рынок для сигнала:
    1. Фильтр по объёму
    2. Проверяет изменение odds
    3. Отправляет в Claude топ-кандидата
    """
    candidates = []

    for market in markets:
        volume = float(market.get("volume", 0))
        if volume < MIN_VOLUME:
            continue

        tokens = market.get("tokens", [])
        for token in tokens:
            if token.get("outcome", "").lower() != "yes":
                continue

            token_id = token.get("token_id", "")
            if not token_id:
                continue

            price = fetch_price(token_id)
            if price is None:
                continue

            key = f"{market.get('id')}_{token_id}"
            if key in prev_odds:
                change = price - prev_odds[key]
                if abs(change) >= ODDS_CHANGE_THRESHOLD:
                    candidates.append({
                        "question": market.get("question", ""),
                        "current_odds": price,
                        "change": change,
                        "volume": volume,
                        "key": key,
                        "slug": market.get("slug", ""),
                    })
            prev_odds[key] = price
            time.sleep(0.2)

    if not candidates:
        return None

    # Берём кандидата с наибольшим движением
    candidates.sort(key=lambda x: abs(x["change"]), reverse=True)
    return candidates[0]


# ============================================================
# ОТПРАВКА В TELEGRAM
# ============================================================

async def send(bot: Bot, text: str):
    try:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
        log.info("✅ Сообщение отправлено")
    except Exception as e:
        log.error(f"Telegram error: {e}")


# ============================================================
# ГЛАВНЫЕ ЗАДАЧИ
# ============================================================

async def job_signal():
    """Генерирует и отправляет один сигнал"""
    global signals_today, last_signal_date

    today = datetime.now().date()
    if last_signal_date != today:
        signals_today = 0
        last_signal_date = today

    if signals_today >= SIGNALS_PER_DAY:
        log.info(f"Лимит сигналов на сегодня достигнут ({SIGNALS_PER_DAY})")
        return

    log.info("🔍 Ищем лучший рынок для сигнала...")
    markets = fetch_markets(limit=40)
    if not markets:
        return

    candidate = find_best_signal(markets)
    if not candidate:
        log.info("Подходящих движений не найдено")
        return

    log.info(f"Анализируем: {candidate['question'][:50]}...")
    ai = ai_analyze(
        question=candidate["question"],
        current_odds=candidate["current_odds"],
        change=candidate["change"],
        volume=candidate["volume"]
    )

    if not ai:
        return

    signals_today += 1
    text = format_signal(
        signal_num=signals_today,
        question=candidate["question"],
        current_odds=candidate["current_odds"],
        change=candidate["change"],
        volume=candidate["volume"],
        ai=ai,
        slug=candidate.get("slug", "")
    )

    bot = Bot(token=TELEGRAM_TOKEN)
    await send(bot, text)
    log.info(f"Сигнал #{signals_today} отправлен. Action: {ai.get('action')}")


def run_signal():
    asyncio.run(job_signal())


# ============================================================
# ЗАПУСК
# ============================================================

def main():
    log.info("🤖 Polymarket Signals Bot запущен")
    log.info(f"📡 Канал: {CHANNEL_ID}")
    log.info(f"⏰ Сигналы в: {SIGNAL_HOURS}")
    log.info(f"📊 Максимум в день: {SIGNALS_PER_DAY}")

    # Первый прогон сразу
    run_signal()

    # Расписание по часам
    for hour in SIGNAL_HOURS:
        schedule.every().day.at(f"{hour:02d}:00").do(run_signal)

    log.info("✅ Расписание настроено. Бот работает...")
    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
