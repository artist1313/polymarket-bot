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
import pytz
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
REFERRAL_URL     = "https://polymarket.com/?r=artist1312"

# ============================================================
# ⚙️ SIGNAL SETTINGS
# ============================================================
SIGNALS_PER_DAY         = 4      # total signals per day
MIN_BUY_SIGNALS_PER_DAY = 2      # minimum BUY signals per day
MIN_VOLUME              = 50000  # minimum market volume in USDC
ODDS_CHANGE_THRESHOLD   = 4.0    # minimum price movement % to trigger signal
SIGNAL_HOURS_GMT3       = [9, 12, 16, 20]  # GMT+3 schedule
TIMEZONE                = pytz.timezone("Europe/Moscow")  # GMT+3

# Markets that already received a signal today
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


def fetch_price_change(token_id: str) -> dict | None:
    """
    Gets price history from CLOB API over last 2 hours.
    Falls back to current price only if history unavailable.
    """
    try:
        import time as _time
        now = int(_time.time())
        two_hours_ago = now - 7200

        r = requests.get(
            "https://clob.polymarket.com/prices-history",
            params={
                "market": token_id,
                "startTs": two_hours_ago,
                "endTs": now,
                "fidelity": 60
            },
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        history = data.get("history", [])

        if len(history) >= 2:
            current = round(float(history[-1].get("p", 0)) * 100, 1)
            old_price = round(float(history[0].get("p", 0)) * 100, 1)
            change = round(current - old_price, 1)
            if current > 0:
                return {"price": current, "change": change}

        # Fallback to current price
        current = fetch_price(token_id)
        return {"price": current, "change": 0.0} if current else None

    except Exception as e:
        log.error(f"CLOB price history error: {e}")
        current = fetch_price(token_id)
        return {"price": current, "change": 0.0} if current else None


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

    prompt = f"""You are an aggressive prediction markets trader. Find trading opportunities.

Market: {question}
Current odds (YES): {current_odds}%
Volume: ${volume:,.0f}

Reply STRICTLY in JSON format (no markdown, no extra text):
{{
  "action": "BUY_YES" | "BUY_NO" | "WAIT",
  "reason": "1 sentence max 15 words explaining the edge",
  "target": number or null
}}

Rules:
- You MUST give BUY_YES or BUY_NO in most cases
- BUY_YES if there is ANY reason YES is underpriced
- BUY_NO if there is ANY reason YES is overpriced  
- WAIT only if odds are perfectly fair with zero edge
- target — always provide a realistic target odds number
- reason — specific and confident in English"""

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
                  volume: float, ai: dict, slug: str = "", change: float = 0.0) -> str:
    """Formats the signal message"""

    action     = ai.get("action", "WAIT")
    reason     = ai.get("reason", "")
    target     = ai.get("target")

    badge      = ACTION_LABELS.get(action, "👀 WATCH")
    emoji      = ACTION_EMOJI.get(action, "🔍")
    vol_str    = f"${volume/1000:.0f}K" if volume < 1_000_000 else f"${volume/1_000_000:.1f}M"
    target_str = f" · target {target}%" if target else ""
    arrow      = f"▲ +{change:.1f}%" if change > 0 else (f"▼ {change:.1f}%" if change < 0 else "")
    change_str = f"  {arrow}" if arrow else ""

    q = question if len(question) <= 60 else question[:57] + "..."

    msg = (
        f"{emoji} *Signal #{signal_num}* — {badge}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"*{q}*\n\n"
        f"*{current_odds}%* YES{change_str}  |  {vol_str}\n\n"
        f"_{reason}_\n\n"
        f"*→ {badge}{target_str}*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"[📊 View on Polymarket](https://polymarket.com/event/{slug})\n"
        f"[🚀 Trade on Polymarket]({REFERRAL_URL})"
    )
    return msg


# ============================================================
# ЛОГИКА ОТБОРА СИГНАЛОВ
# ============================================================

def find_candidates(markets: list, limit: int = 5) -> list:
    """
    Берёт цену прямо из данных рынка (outcomesPrices) — без лишних API запросов.
    Надёжно работает всегда.
    """
    candidates = []

    for market in markets:
        volume = float(market.get("volume", 0))
        if volume < MIN_VOLUME:
            continue

        market_id = market.get("id", "")
        if market_id in sent_today:
            continue

        # Берём цену YES прямо из данных рынка
        try:
            outcomes = market.get("outcomes", "[]")
            prices_raw = market.get("outcomePrices", "[]")
            if isinstance(outcomes, str):
                import json
                outcomes = json.loads(outcomes)
            if isinstance(prices_raw, str):
                import json
                prices_raw = json.loads(prices_raw)

            yes_price = None
            for i, outcome in enumerate(outcomes):
                if str(outcome).lower() == "yes" and i < len(prices_raw):
                    yes_price = round(float(prices_raw[i]) * 100, 1)
                    break

            if yes_price is None:
                continue
            if yes_price > 95 or yes_price < 5:
                continue

        except Exception as e:
            log.error(f"Price parse error: {e}")
            continue

        candidates.append({
            "question": market.get("question", ""),
            "current_odds": yes_price,
            "change": 0.0,
            "volume": volume,
            "market_id": market_id,
            "slug": market.get("slug", ""),
        })

    candidates.sort(key=lambda x: x["volume"], reverse=True)
    log.info(f"Candidates found: {len(candidates)}")
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

    today = datetime.now(TIMEZONE).date()
    if last_signal_date != today:
        signals_today = 0
        buy_signals_today = 0
        last_signal_date = today
        sent_today = set()
        log.info("New day — counters reset")

    if signals_today >= SIGNALS_PER_DAY:
        log.info(f"Лимит сигналов на сегодня достигнут ({SIGNALS_PER_DAY})")
        return

    bot = Bot(token=TELEGRAM_TOKEN)

    log.info("🔍 Ищем рынки для сигнала...")
    markets = fetch_markets(limit=50)
    if not markets:
        log.error("Polymarket API unavailable")
        return

    need_buy = buy_signals_today < MIN_BUY_SIGNALS_PER_DAY
    limit = 10 if need_buy else 5
    candidates = find_candidates(markets, limit=limit)

    if not candidates:
        log.info("No suitable markets found")
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
        log.info("BUY signal not found — sending wait notification")
        signals_today += 1
        await send(bot, (
            "🔍 *Market Scan*\n"
            "━━━━━━━━━━━━━━━━\n"
            "No buy signals at this time.\n\n"
            "_Markets show no clear inefficiency — better to wait._\n\n"
            f"⏰ Next check on schedule.\n"
            f"[🚀 Trade on Polymarket]({REFERRAL_URL})"
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
        slug=chosen.get("slug", ""),
        change=chosen.get("change", 0.0)
    )

    await send(bot, text)
    log.info(f"Сигнал #{signals_today} отправлен. Action: {chosen_ai.get('action')} | BUY сегодня: {buy_signals_today}")


def run_signal():
    asyncio.run(job_signal())


# ============================================================
# ЗАПУСК
# ============================================================

def main():
    log.info("🤖 Polymarket Signals Bot started")
    log.info(f"📡 Channel: {CHANNEL_ID}")
    log.info(f"⏰ Schedule GMT+3: {SIGNAL_HOURS_GMT3}")
    log.info(f"📊 Max signals per day: {SIGNALS_PER_DAY}")

    # First run immediately on startup
    run_signal()

    # Schedule in UTC (GMT+3 minus 3 hours)
    for hour in SIGNAL_HOURS_GMT3:
        utc_hour = (hour - 3) % 24
        schedule.every().day.at(f"{utc_hour:02d}:00").do(run_signal)
        log.info(f"Scheduled: {hour:02d}:00 GMT+3 = {utc_hour:02d}:00 UTC")

    log.info("✅ Schedule set. Bot is running...")
    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
