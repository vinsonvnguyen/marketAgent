"""
Market News Agent
-----------------
Scans market news using Claude + web search and sends alerts via
Telegram, email (SendGrid/SMTP), or SMS (Twilio). Runs on a schedule.

Deploy options:
  - Local cron / scheduler   →  python agent.py
  - GitHub Actions           →  use market-agent.yml workflow
  - AWS Lambda               →  use lambda_handler.py

─────────────────────────────────────────────
SETUP — GitHub Secrets (Settings → Secrets):
─────────────────────────────────────────────
ANTHROPIC_API_KEY      → from console.anthropic.com
TELEGRAM_BOT_TOKEN     → from @BotFather on Telegram
TELEGRAM_CHAT_ID       → your personal chat ID (see README)
SENDGRID_API_KEY       → only if using email
EMAIL_FROM / EMAIL_TO  → only if using email
SMTP_USER / SMTP_PASSWORD → only if using Gmail SMTP
TWILIO_*               → only if using SMS

─────────────────────────────────────────────
SETUP — GitHub Variables (Settings → Variables):
─────────────────────────────────────────────
WATCHLIST        → AAPL,NVDA,Fed rates
SENSITIVITY      → medium
MIN_IMPACT       → medium
MARKET_HOURS_ONLY → true
"""

import os
import json
import time
import logging
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from zoneinfo import ZoneInfo

import anthropic
import requests
import schedule
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONFIGURATION  (edit these or use .env)
# ─────────────────────────────────────────────

WATCHLIST         = os.getenv("WATCHLIST", "AAPL,NVDA,TSLA,Fed rates,semiconductors,Crypto").split(",")
SENSITIVITY       = os.getenv("SENSITIVITY", "medium")
MIN_IMPACT        = os.getenv("MIN_IMPACT", "medium")
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "30"))
MARKET_HOURS_ONLY = os.getenv("MARKET_HOURS_ONLY", "true").lower() == "true"
MARKET_TZ         = ZoneInfo("America/New_York")

# ── Telegram ──────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")


# ─────────────────────────────────────────────
# DEDUPLICATION  (avoids re-alerting same story)
# ─────────────────────────────────────────────

SEEN_FILE = "seen_alerts.json"

def load_seen() -> set:
    try:
        with open(SEEN_FILE) as f:
            data = json.load(f)
            return set(data.get("titles", []))
    except FileNotFoundError:
        return set()

def save_seen(seen: set):
    titles = list(seen)[-200:]   # keep last 200 to avoid unbounded growth
    with open(SEEN_FILE, "w") as f:
        json.dump({"titles": titles}, f)

# ─────────────────────────────────────────────
# MARKET HOURS CHECK
# ─────────────────────────────────────────────

def is_market_hours() -> bool:
    """Returns True if current time is within NYSE trading hours (Mon–Fri 9:30–16:00 ET)."""
    now = datetime.now(MARKET_TZ)
    if now.weekday() >= 5:
        return False
    market_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now <= market_close

# ─────────────────────────────────────────────
# CLAUDE SCAN
# ─────────────────────────────────────────────

SENSITIVITY_DESC = {
    "high":   "all news including minor company updates, analyst notes, and macro commentary",
    "medium": "significant news that could move a stock 1%+ or materially affect sector sentiment",
    "low":    "only major breaking news, earnings surprises, or clear market-moving events",
}

SYSTEM_PROMPT = """You are a professional market intelligence agent.
Your job is to search for breaking financial and market news, analyze its relevance
to a given watchlist, and return structured alert data.

ALWAYS respond with ONLY a valid JSON array — no markdown, no code fences, no preamble.
If no relevant news is found, return an empty array: []
"""

def build_user_prompt(watchlist: list[str], sensitivity: str) -> str:
    items = ", ".join(watchlist)
    desc  = SENSITIVITY_DESC.get(sensitivity, SENSITIVITY_DESC["medium"])
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    return f"""Today is {today}. Search for the latest financial news relevant to this watchlist.

Watchlist: {items}
Sensitivity: {desc}

For each relevant news item, include:
{{
  "title": "concise headline (max 80 chars)",
  "summary": "2-3 sentences — what happened and why it matters to investors",
  "impact": "high" | "medium" | "low",
  "tickers": ["TICKER1"],
  "category": "earnings" | "macro" | "geopolitical" | "regulatory" | "product" | "analyst" | "other",
  "source": "publication name if known"
}}

Return 3–8 most important and recent items. Prioritize items from the last 24 hours.
Return ONLY the JSON array, nothing else."""


def scan_news(watchlist: list[str], sensitivity: str = "medium") -> list[dict]:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    log.info(f"Scanning news for: {watchlist}")
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": build_user_prompt(watchlist, sensitivity)}],
    )

    text = " ".join(b.text for b in response.content if b.type == "text")

    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        log.warning("No JSON array found in Claude response.")
        return []

    try:
        alerts = json.loads(text[start : end + 1])
        log.info(f"Found {len(alerts)} alerts.")
        return alerts
    except json.JSONDecodeError as e:
        log.error(f"JSON parse error: {e}\nRaw: {text}")
        return []

# ─────────────────────────────────────────────
# IMPACT FILTER
# ─────────────────────────────────────────────

IMPACT_RANK = {"low": 1, "medium": 2, "high": 3}

def filter_by_impact(alerts: list[dict], min_impact: str) -> list[dict]:
    threshold = IMPACT_RANK.get(min_impact, 2)
    return [a for a in alerts if IMPACT_RANK.get(a.get("impact", "low"), 1) >= threshold]

# ─────────────────────────────────────────────
# TELEGRAM NOTIFICATION
# ─────────────────────────────────────────────

IMPACT_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}
IMPACT_LABEL = {"high": "HIGH IMPACT", "medium": "Medium", "low": "Low"}

def format_telegram_message(alerts: list[dict]) -> str:
    """
    Formats alerts as a clean Telegram message using MarkdownV2.
    Telegram supports: *bold*, _italic_, `code`, and plain text.
    """
    ts = datetime.now(MARKET_TZ).strftime("%b %d %I:%M %p ET")
    lines = [f"📡 *Market News Alert* — {ts}\n"]

    for a in alerts:
        impact  = a.get("impact", "low")
        emoji   = IMPACT_EMOJI.get(impact, "⚪")
        label   = IMPACT_LABEL.get(impact, "")
        tickers = "  ".join(f"`{t}`" for t in a.get("tickers", []))
        category = a.get("category", "").upper()
        source   = a.get("source", "")

        lines.append(f"{emoji} *{escape_md(a['title'])}*")
        lines.append(escape_md(a.get("summary", "")))

        meta_parts = []
        if tickers:
            meta_parts.append(tickers)
        if category:
            meta_parts.append(f"_{category}_")
        if source:
            meta_parts.append(f"via {escape_md(source)}")
        if meta_parts:
            lines.append("  ".join(meta_parts))

        lines.append("")   # blank line between alerts

    return "\n".join(lines)


def escape_md(text: str) -> str:
    """Escape special characters required by Telegram MarkdownV2."""
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))


def send_telegram(alerts: list[dict]):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — skipping.")
        return

    # Telegram messages have a 4096 char limit — split if needed
    message = format_telegram_message(alerts)
    chunks  = [message[i:i+4000] for i in range(0, len(message), 4000)]

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    for chunk in chunks:
        payload = {
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       chunk,
            "parse_mode": "MarkdownV2",
        }
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            log.info("Telegram message sent.")
        else:
            log.error(f"Telegram error {r.status_code}: {r.text}")



# ─────────────────────────────────────────────
# DISPATCH ALL CHANNELS
# ─────────────────────────────────────────────

def send_notifications(alerts: list[dict]):
    if not alerts:
        return
    send_telegram(alerts)
    if SENDGRID_API_KEY:
        send_email_sendgrid(alerts)
    elif SMTP_USER:
        send_email_smtp(alerts)
    send_sms_twilio(alerts)

# ─────────────────────────────────────────────
# MAIN SCAN JOB
# ─────────────────────────────────────────────

def run_scan():
    if MARKET_HOURS_ONLY and not is_market_hours():
        log.info("Outside market hours — skipping scan.")
        return

    seen       = load_seen()
    alerts     = scan_news(WATCHLIST, SENSITIVITY)
    alerts     = filter_by_impact(alerts, MIN_IMPACT)
    new_alerts = [a for a in alerts if a.get("title") not in seen]

    if not new_alerts:
        log.info("No new alerts to send.")
        return

    log.info(f"Sending {len(new_alerts)} new alert(s).")
    send_notifications(new_alerts)

    seen.update(a["title"] for a in new_alerts)
    save_seen(seen)


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    log.info(f"Market agent starting — scanning every {SCAN_INTERVAL_MINUTES} min.")
    log.info(f"Watchlist: {WATCHLIST}")
    log.info(f"Sensitivity: {SENSITIVITY} | Min impact: {MIN_IMPACT}")

    run_scan()   # run once immediately on startup

    schedule.every(SCAN_INTERVAL_MINUTES).minutes.do(run_scan)
    while True:
        schedule.run_pending()
        time.sleep(30)