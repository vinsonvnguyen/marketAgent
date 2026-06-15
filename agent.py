"""
Market News Agent
-----------------
Scans market news using Claude + web search and sends alerts via
Slack, email (SendGrid), or SMS (Twilio). Runs on a schedule.

Deploy options:
  - Local cron / scheduler   →  python agent.py
  - AWS Lambda               →  use lambda_handler.py
  - Railway / Render         →  set START_CMD=python agent.py
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

WATCHLIST = os.getenv("WATCHLIST", "AAPL,NVDA,TSLA,Fed rates,semiconductors").split(",")

SENSITIVITY = os.getenv("SENSITIVITY", "medium")
# high   = all news including minor developments
# medium = news likely to move stock 1%+ or affect sector
# low    = only major breaking / market-moving events

MIN_IMPACT = os.getenv("MIN_IMPACT", "medium")   # low | medium | high
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "30"))
MARKET_HOURS_ONLY = os.getenv("MARKET_HOURS_ONLY", "true").lower() == "true"
MARKET_TZ = ZoneInfo("America/New_York")

# Notification channels — set the ones you want in .env
SLACK_WEBHOOK_URL   = os.getenv("SLACK_WEBHOOK_URL", "")
SENDGRID_API_KEY    = os.getenv("SENDGRID_API_KEY", "")
EMAIL_FROM          = os.getenv("EMAIL_FROM", "")
EMAIL_TO            = os.getenv("EMAIL_TO", "")
TWILIO_ACCOUNT_SID  = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER  = os.getenv("TWILIO_FROM_NUMBER", "")
TWILIO_TO_NUMBER    = os.getenv("TWILIO_TO_NUMBER", "")
SMTP_HOST           = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT           = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER           = os.getenv("SMTP_USER", "")
SMTP_PASSWORD       = os.getenv("SMTP_PASSWORD", "")

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
    # Keep only the last 200 titles to avoid unbounded growth
    titles = list(seen)[-200:]
    with open(SEEN_FILE, "w") as f:
        json.dump({"titles": titles}, f)

# ─────────────────────────────────────────────
# MARKET HOURS CHECK
# ─────────────────────────────────────────────

def is_market_hours() -> bool:
    """Returns True if current time is within NYSE trading hours (Mon–Fri 9:30–16:00 ET)."""
    now = datetime.now(MARKET_TZ)
    if now.weekday() >= 5:           # Saturday=5, Sunday=6
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

    # Pull text blocks from response (web_search may add tool_result blocks)
    text = " ".join(b.text for b in response.content if b.type == "text")

    # Extract JSON array even if the model wraps it accidentally
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
# NOTIFICATIONS
# ─────────────────────────────────────────────

IMPACT_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}

def format_slack_blocks(alerts: list[dict]) -> dict:
    """Builds a rich Slack message using Block Kit."""
    ts = datetime.now(MARKET_TZ).strftime("%b %d %I:%M %p ET")
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📡 Market News Alert — {ts}"}},
        {"type": "divider"},
    ]
    for a in alerts:
        emoji = IMPACT_EMOJI.get(a.get("impact", "low"), "⚪")
        tickers = "  ".join(f"`{t}`" for t in a.get("tickers", []))
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{emoji} *{a['title']}*\n"
                    f"{a.get('summary', '')}\n"
                    f"{tickers}  _{a.get('category','').upper()}_"
                ),
            },
        })
        blocks.append({"type": "divider"})
    return {"blocks": blocks}


def send_slack(alerts: list[dict]):
    if not SLACK_WEBHOOK_URL:
        return
    payload = format_slack_blocks(alerts)
    r = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
    if r.status_code == 200:
        log.info("Slack notification sent.")
    else:
        log.error(f"Slack error {r.status_code}: {r.text}")


def format_html_email(alerts: list[dict]) -> str:
    rows = ""
    colors = {"high": "#E24B4A", "medium": "#EF9F27", "low": "#1D9E75"}
    for a in alerts:
        color = colors.get(a.get("impact", "low"), "#888")
        tickers = " ".join(f'<span style="background:#f0f0f0;padding:1px 6px;border-radius:4px;font-family:monospace">{t}</span>' for t in a.get("tickers", []))
        rows += f"""
        <tr>
          <td style="padding:14px 16px;border-bottom:1px solid #eee">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
              <span style="width:10px;height:10px;border-radius:50%;background:{color};display:inline-block"></span>
              <strong style="font-size:15px">{a['title']}</strong>
            </div>
            <p style="margin:4px 0 8px;color:#555;font-size:14px">{a.get('summary','')}</p>
            <div style="font-size:13px;color:#888">{tickers} &nbsp; {a.get('category','').upper()}</div>
          </td>
        </tr>"""

    ts = datetime.now(MARKET_TZ).strftime("%B %d, %Y %I:%M %p ET")
    return f"""
    <html><body style="font-family:sans-serif;max-width:640px;margin:0 auto">
      <div style="background:#111;color:#fff;padding:16px 20px;border-radius:8px 8px 0 0">
        <h2 style="margin:0;font-size:18px">📡 Market News Agent</h2>
        <p style="margin:4px 0 0;font-size:13px;opacity:.7">{ts}</p>
      </div>
      <table style="width:100%;border-collapse:collapse">{rows}</table>
      <div style="padding:12px 16px;font-size:12px;color:#999;border-top:1px solid #eee">
        Watchlist: {', '.join(WATCHLIST)} · Sensitivity: {SENSITIVITY}
      </div>
    </body></html>"""


def send_email_sendgrid(alerts: list[dict]):
    if not SENDGRID_API_KEY or not EMAIL_FROM or not EMAIL_TO:
        return
    subject = f"📡 {len(alerts)} Market Alert{'s' if len(alerts)>1 else ''} — {datetime.now(MARKET_TZ).strftime('%b %d %I:%M %p')}"
    payload = {
        "personalizations": [{"to": [{"email": EMAIL_TO}]}],
        "from": {"email": EMAIL_FROM},
        "subject": subject,
        "content": [{"type": "text/html", "value": format_html_email(alerts)}],
    }
    r = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=10,
    )
    if r.status_code in (200, 202):
        log.info("SendGrid email sent.")
    else:
        log.error(f"SendGrid error {r.status_code}: {r.text}")


def send_email_smtp(alerts: list[dict]):
    """Fallback: plain SMTP (Gmail app password, etc.)"""
    if not SMTP_USER or not SMTP_PASSWORD or not EMAIL_TO:
        return
    subject = f"📡 Market Alert — {len(alerts)} item{'s' if len(alerts)>1 else ''}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(format_html_email(alerts), "html"))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.sendmail(SMTP_USER, EMAIL_TO, msg.as_string())
        log.info("SMTP email sent.")
    except Exception as e:
        log.error(f"SMTP error: {e}")


def send_sms_twilio(alerts: list[dict]):
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, TWILIO_TO_NUMBER]):
        return
    # Keep SMS brief — top 2 alerts only
    lines = [f"📡 Market Agent ({datetime.now(MARKET_TZ).strftime('%I:%M %p ET')})"]
    for a in alerts[:2]:
        emoji = IMPACT_EMOJI.get(a.get("impact", "low"), "⚪")
        lines.append(f"{emoji} {a['title']}")
    body = "\n".join(lines)
    url  = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    r = requests.post(
        url,
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        data={"From": TWILIO_FROM_NUMBER, "To": TWILIO_TO_NUMBER, "Body": body},
        timeout=10,
    )
    if r.status_code == 201:
        log.info("Twilio SMS sent.")
    else:
        log.error(f"Twilio error {r.status_code}: {r.text}")


def send_notifications(alerts: list[dict]):
    """Dispatch to all configured channels."""
    if not alerts:
        return
    send_slack(alerts)
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

    seen    = load_seen()
    alerts  = scan_news(WATCHLIST, SENSITIVITY)
    alerts  = filter_by_impact(alerts, MIN_IMPACT)

    # Remove stories already seen
    new_alerts = [a for a in alerts if a.get("title") not in seen]

    if not new_alerts:
        log.info("No new alerts to send.")
        return

    log.info(f"Sending {len(new_alerts)} new alert(s).")
    send_notifications(new_alerts)

    # Mark these titles as seen
    seen.update(a["title"] for a in new_alerts)
    save_seen(seen)


# ─────────────────────────────────────────────
# SCHEDULER  (local / server mode)
# ─────────────────────────────────────────────

if __name__ == "__main__":
    log.info(f"Market agent starting — scanning every {SCAN_INTERVAL_MINUTES} min.")
    log.info(f"Watchlist: {WATCHLIST}")
    log.info(f"Sensitivity: {SENSITIVITY} | Min impact: {MIN_IMPACT}")

    # Run once immediately on startup
    run_scan()

    schedule.every(SCAN_INTERVAL_MINUTES).minutes.do(run_scan)
    while True:
        schedule.run_pending()
        time.sleep(30)
