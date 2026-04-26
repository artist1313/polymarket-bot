"""
Polymarket Signals Bot — AI-powered signals
"""

import requests
import schedule
import time
import logging
import json
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
REFERRAL_URL     = "https://polymarket.com/?r=artist1312"

# ============================================================
# ⚙️ SIGNAL SETTINGS
# ============================================================
SIGNALS_PER_DAY         = 4
MIN_BUY_SIGNALS_PER_DAY = 2
MIN_VOLUME              = 50000
SIGNAL_HOURS_GMT3       = [9, 12, 16, 20]
TIMEZONE                = pytz.timezone("Europe/Moscow")

sent_today: set = set()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

signals_today = 0
buy_signals_today = 0
last_signal_date = None


# ============================================================
# POLYMARKET API
# ============================================================

def fetch_markets(limit: int = 50) -> list:
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


def get_yes_price(market: dict) -> float | None:
    """Берёт цену YES прямо из данных рынка — outcomePrices"""
    try:
        outcomes = market.get("outcomes", "[]")
        prices_raw = market.get("outcomePrices", "[]")

        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(prices_raw, str):
            prices_raw = json.loads(prices_raw)

        for i, outcome in enumerate(outcomes):
            if str(outcome).lower() == "yes" and i < len(prices_raw):
                return round(float(prices_raw[i]) * 100, 1)
    except Exception as e:
        log.error(f"Price parse error: {e}")
    return None


# ============================================================
# AI ANALYSIS
# ============================================================

def ai_analyze(question: str, current_odds: float, volume: float) -> dict | None:
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
# FORMATTING
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

    action     = ai.get("action", "WAIT")
    reason     = ai.get("reason", "")
    target     = ai.get("target")
    badge      = ACTION_LABELS.get(action, "👀 WATCH")
    emoji      = ACTION_EMOJI.get(action, "🔍")
    vol_str    = f"${volume/1000:.0f}K" if volume < 1_000_000 else f"${volume/1_000_000:.1f}M"
    target_str = f" · target {target}%" if target else ""
    q = question if len(question) <= 60 else question[:57] + "..."

    return (
        f"{emoji} *Signal #{signal_num}* — {badge}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"*{q}*\n\n"
        f"*{current_odds}%* YES  |  {vol_str}\n\n"
        f"_{reason}_\n\n"
        f"*→ {badge}{target_str}*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"[📊 View on Polymarket](https://polymarket.com/event/{slug})\n"
        f"[🚀 Trade on Polymarket]({REFERRAL_URL})"
    )


# ============================================================
# SIGNAL SELECTION
# ============================================================

def find_candidates(markets: list, limit: int = 5) -> list:
    candidates = []

    for market in markets:
        volume = float(market.get("volume", 0))
        if volume < MIN_VOLUME:
            continue

        market_id = market.get("id", "")
        if market_id in sent_today:
            continue

        yes_price = get_yes_price(market)
        if yes_price is None:
            continue
        if yes_price > 95 or yes_price < 5:
            continue

        candidates.append({
            "question": market.get("question", ""),
            "current_odds": yes_price,
            "volume": volume,
            "market_id": market_id,
            "slug": market.get("slug", ""),
        })

    candidates.sort(key=lambda x: x["volume"], reverse=True)
    log.info(f"Candidates found: {len(candidates)}")
    return candidates[:limit]


# ============================================================
# TELEGRAM
# ============================================================

async def send(bot: Bot, text: str):
    try:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
        log.info("✅ Message sent")
    except Exception as e:
        log.error(f"Telegram error: {e}")


# ============================================================
# MAIN JOB
# ============================================================

async def job_signal():
    global signals_today, buy_signals_today, last_signal_date, sent_today

    today = datetime.now(TIMEZONE).date()
    if last_signal_date != today:
        signals_today = 0
        buy_signals_today = 0
        last_signal_date = today
        sent_today = set()
        log.info("New day — counters reset")

    if signals_today >= SIGNALS_PER_DAY:
        log.info(f"Daily limit reached ({SIGNALS_PER_DAY})")
        return

    bot = Bot(token=TELEGRAM_TOKEN)

    log.info("🔍 Scanning markets...")
    markets = fetch_markets(limit=50)
    if not markets:
        log.error("Polymarket API unavailable")
        return

    need_buy = buy_signals_today < MIN_BUY_SIGNALS_PER_DAY
    limit = 10 if need_buy else 5
    candidates = find_candidates(markets, limit=limit)

    if not candidates:
        log.info("No suitable markets found")
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

    chosen = None
    chosen_ai = None

    for c in candidates:
        log.info(f"Analyzing: {c['question'][:50]}...")
        ai = ai_analyze(
            question=c["question"],
            current_odds=c["current_odds"],
            volume=c["volume"]
        )
        sent_today.add(c["market_id"])

        if not ai:
            continue

        if ai.get("action") in ("BUY_YES", "BUY_NO"):
            chosen = c
            chosen_ai = ai
            log.info(f"BUY signal found: {ai.get('action')}")
            break

        if chosen is None and not need_buy:
            chosen = c
            chosen_ai = ai

    if chosen is None or (need_buy and chosen_ai.get("action") not in ("BUY_YES", "BUY_NO")):
        log.info("No BUY signal — sending wait message")
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
        slug=chosen.get("slug", "")
    )

    await send(bot, text)
    log.info(f"Signal #{signals_today} sent. Action: {chosen_ai.get('action')} | BUY today: {buy_signals_today}")


def run_signal():
    asyncio.run(job_signal())


# ============================================================
# START
# ============================================================

def main():
    log.info("🤖 Polymarket Signals Bot started")
    log.info(f"📡 Channel: {CHANNEL_ID}")
    log.info(f"⏰ Schedule GMT+3: {SIGNAL_HOURS_GMT3}")
    log.info(f"📊 Max signals per day: {SIGNALS_PER_DAY}")

    run_signal()

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
