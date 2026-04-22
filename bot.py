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
SIGNALS_PER_DAY        = 4       # сколько сигналов в день всего
MIN_BUY_SIGNALS_PER_DAY = 2      # минимум BUY сигналов в день
MIN_VOLUME             = 50000   # минимальный объём рынка в USDC
SIGNAL_HOURS           = [9, 12, 16, 20]  # часы отправки сигналов

# Рынки которые уже получили сигнал сегодня (не повторяем)
sent_today: set = set()

# ============================================================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# Счётчик сигналов за день
signals_today = 0
buy_signals_today = 0
last_signal_date = None


# ============================================================
# POLYMARKET API
# ============================================================

def fetch_markets(limit: int = 50) -> list:
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

def ai_analyze(question: str, current_odds: float, volume: float) -> dict | None:
    """
    Отправляет данные рынка в OpenRouter и получает:
    - action: BUY_YES / BUY_NO / WAIT
    - reason: 1-2 предложения почему
    - target: целевые odds (если есть)
    """
    import json

    prompt = f"""You are a prediction markets analyst. Give a short trading signal.

Market: {question}
Current odds (YES): {current_odds}%
Volume: ${volume:,.0f}

Reply STRICTLY in JSON format (no markdown, no extra text):
{{
  "action": "BUY_YES" | "BUY_NO" | "WAIT",
  "reason": "1-2 sentences — why the market is mispriced or why to wait",
  "target": number or null
}}

Rules:
- BUY_YES if YES odds seem too low given the situation
- BUY_NO if YES odds seem too high given the situation
- WAIT if odds are fair or risk/reward is unfavorable
- reason — max 20 words, specific and to the point in English
- target — expected odds after correction, or null
- Do NOT output WAIT every time — give real signals"""

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
    "WAIT":    "👀 WATCH",
}

ACTION_EMOJI = {
    "BUY_YES": "📈",
    "BUY_NO":  "📉",
    "WAIT":    "🔍",
}

def format_signal(signal_num: int, question: str, current_odds: float,
                  volume: float, ai: dict, slug: str = "") -> str:
    """Форматирует финальное сообщение сигнала"""

    action     = ai.get("action", "WAIT")
    reason     = ai.get("reason", "")
    target     = ai.get("target")

    badge      = ACTION_LABELS.get(action, "👀 WATCH")
    emoji      = ACTION_EMOJI.get(action, "🔍")
    vol_str    = f"${volume/1000:.0f}K" if volume < 1_000_000 else f"${volume/1_000_000:.1f}M"
    target_str = f" · target {target}%" if target else ""

    # Укорачиваем вопрос если длинный
    q = question if len(question) <= 60 else question[:57] + "..."

    msg = (
        f"{emoji} *Signal #{signal_num}* — {badge}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"*{q}*\n\n"
        f"*{current_odds}%* YES  |  {vol_str}\n\n"
        f"_{reason}_\n\n"
        f"*→ {badge}{target_str}*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"[📊 View on Polymarket](https://polymarket.com/event/{slug})"
    )
    return msg


# ============================================================
# ЛОГИКА ОТБОРА СИГНАЛОВ
# ============================================================

def find_candidates(markets: list, limit: int = 5) -> list:
    """
    Собирает топ-N кандидатов по объёму.
    Возвращает список, не один рынок.
    """
    candidates = []

    for market in markets:
        volume = float(market.get("volume", 0))
        if volume < MIN_VOLUME:
            continue

        market_id = market.get("id", "")
        if market_id in sent_today:
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

            # Пропускаем экстремальные рынки (>95% или <5%)
            if price > 95 or price < 5:
                continue

            candidates.append({
                "question": market.get("question", ""),
                "current_odds": price,
                "volume": volume,
                "market_id": market_id,
                "slug": market.get("slug", ""),
            })
            break  # только YES токен

        time.sleep(0.2)

    candidates.sort(key=lambda x: x["volume"], reverse=True)
    log.info(f"Найдено кандидатов: {len(candidates)}")
    return candidates[:limit]


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
    """Генерирует и отправляет сигнал — ВСЕГДА пишет в канал по расписанию"""
    global signals_today, buy_signals_today, last_signal_date, sent_today

    today = datetime.now().date()
    if last_signal_date != today:
        signals_today = 0
        buy_signals_today = 0
        last_signal_date = today
        sent_today = set()
        log.info("Новый день — счётчики сброшены")

    if signals_today >= SIGNALS_PER_DAY:
        log.info(f"Лимит сигналов на сегодня достигнут ({SIGNALS_PER_DAY})")
        return

    bot = Bot(token=TELEGRAM_TOKEN)

    log.info("🔍 Ищем рынки для сигнала...")
    markets = fetch_markets(limit=50)
    if not markets:
        log.error("Polymarket API недоступен")
        await send(bot, "⚠️ *Нет данных*\n\nPolymarket временно недоступен. Следующая проверка по расписанию.")
        return

    # Берём больше кандидатов если нужен BUY сигнал
    need_buy = buy_signals_today < MIN_BUY_SIGNALS_PER_DAY
    limit = 10 if need_buy else 5
    candidates = find_candidates(markets, limit=limit)

    if not candidates:
        log.info("Нет подходящих рынков")
        signals_today += 1
        await send(bot, (
            "🔍 *Мониторинг рынков*\n"
            "━━━━━━━━━━━━━━━━\n"
            "Актуальных сигналов пока нет.\n\n"
            "_Рынки стабильны — ждём хорошей точки входа._\n\n"
            "⏰ Следующая проверка по расписанию."
        ))
        return

    chosen = None
    chosen_ai = None

    # Пробуем кандидатов — если нужен BUY, пропускаем WAIT
    for c in candidates:
        log.info(f"Анализируем: {c['question'][:50]}...")
        ai = ai_analyze(
            question=c["question"],
            current_odds=c["current_odds"],
            volume=c["volume"]
        )
        sent_today.add(c["market_id"])

        if not ai:
            continue

        action = ai.get("action")

        if action in ("BUY_YES", "BUY_NO"):
            chosen = c
            chosen_ai = ai
            log.info(f"Найден BUY сигнал: {action}")
            break

        # Запоминаем WAIT как запасной только если BUY уже выполнен
        if chosen is None and not need_buy:
            chosen = c
            chosen_ai = ai
            log.info("Запасной WATCH сигнал")

    # Если BUY не нашли и он нужен — отправляем "нет сигнала"
    if chosen is None or (need_buy and chosen_ai.get("action") not in ("BUY_YES", "BUY_NO")):
        log.info("BUY сигнал не найден — отправляем уведомление об ожидании")
        signals_today += 1
        await send(bot, (
            "🔍 *Мониторинг рынков*\n"
            "━━━━━━━━━━━━━━━━\n"
            "Актуальных сигналов на покупку пока нет.\n\n"
            "_Рынки не показывают чёткой неэффективности — лучше подождать._\n\n"
            "⏰ Следующая проверка по расписанию."
        ))
        return

    signals_today += 1
    if chosen_ai.get("action") in ("BUY_YES", "BUY_NO"):
        buy_signals_today += 1

    text = format_signal(
        signal_num=signals_today,
        question=chosen["question"],
        current_odds=chosen["current_odds"],
        volume=chosen["volume"],
        ai=chosen_ai,
        slug=chosen.get("slug", "")
    )

    await send(bot, text)
    log.info(f"Сигнал #{signals_today} отправлен. Action: {chosen_ai.get('action')} | BUY сегодня: {buy_signals_today}")


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

    # Первый прогон сразу при запуске
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
