# 📡 Market News Agent

AI-powered market scanner built on Claude + web search. Monitors your stock watchlist,
filters news by impact, deduplicates seen stories, and sends alerts to Slack, email, or SMS.

## Quick start (5 minutes)

```bash
git clone https://github.com/you/market-agent
cd market-agent
pip install -r requirements.txt
cp .env.example .env          # fill in your keys
python agent.py               # runs and scans every 30 min
testing push
```

---

## Notification setup

### Slack (recommended — free, instant)
1. Go to https://api.slack.com/messaging/webhooks
2. Create an app → Enable Incoming Webhooks → Add to Workspace
3. Copy the webhook URL into `.env` as `SLACK_WEBHOOK_URL`

### Email via Gmail (free)
1. Enable 2FA on your Google account
2. Go to https://myaccount.google.com/apppasswords → create an App Password
3. Set `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO` in `.env`

### Email via SendGrid (free up to 100/day)
1. Sign up at sendgrid.com → API Keys → Create Key
2. Set `SENDGRID_API_KEY`, `EMAIL_FROM`, `EMAIL_TO` in `.env`

### SMS via Twilio (~$1 free credit, then $0.01/text)
1. Sign up at twilio.com → get a phone number
2. Set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `TWILIO_TO_NUMBER`

---

## Deployment options

### Option A — GitHub Actions (FREE, no server needed) ⭐ Recommended

1. Push this repo to GitHub (can be private)
2. Go to Settings → Secrets and variables → Actions
3. Add secrets: `ANTHROPIC_API_KEY`, `SLACK_WEBHOOK_URL`, etc.
4. Add variables: `WATCHLIST`, `SENSITIVITY`, `MIN_IMPACT`
5. The workflow in `.github/workflows/scan.yml` runs automatically Mon–Fri

**Cost: $0** — GitHub gives 2,000 free minutes/month for private repos (public = unlimited)

### Option B — AWS Lambda + EventBridge (free tier)

See deployment steps inside `lambda_handler.py`.

**Cost: ~$0** — Lambda free tier covers ~440 invocations/month easily

### Option C — Railway.app ($5/mo)

1. Push to GitHub
2. railway.app → New Project → Deploy from GitHub
3. Set env vars in the Railway dashboard
4. Set start command: `python agent.py`

**Cost: ~$5/month** — simplest option, zero DevOps

### Option D — Local machine / always-on server

Just run `python agent.py`. Uses the built-in `schedule` library to loop every N minutes.
Works great on a Raspberry Pi or any cheap VPS.

---

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | required | Your Anthropic API key |
| `WATCHLIST` | `AAPL,NVDA,...` | Comma-separated tickers and topics |
| `SENSITIVITY` | `medium` | `high` / `medium` / `low` — what Claude looks for |
| `MIN_IMPACT` | `medium` | Minimum impact level to trigger an alert |
| `SCAN_INTERVAL_MINUTES` | `30` | How often to scan (local mode only) |
| `MARKET_HOURS_ONLY` | `true` | Skip scans outside NYSE hours |

---

## Estimated monthly cost

| Scan frequency | Claude API | Web search | Total |
|---|---|---|---|
| Every 30 min, market hours | ~$1.20 | ~$1.20 | **~$2.50** |
| Every 15 min, market hours | ~$2.40 | ~$2.40 | **~$5.00** |
| Every hour, 24/7 | ~$1.50 | ~$1.50 | **~$3.00** |

Switch to `claude-haiku-4-5-20251001` in `agent.py` to cut Claude costs by ~70%.

---

## File structure

```
market-agent/
├── agent.py              # Core agent — scan, filter, notify
├── lambda_handler.py     # AWS Lambda wrapper + deploy guide
├── requirements.txt
├── .env.example          # Copy to .env and fill in your keys
├── .github/
│   └── workflows/
│       └── scan.yml      # GitHub Actions scheduler
└── README.md
```
