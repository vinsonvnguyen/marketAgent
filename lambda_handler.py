"""
lambda_handler.py
-----------------
AWS Lambda entry point. Triggered by EventBridge Scheduler (cron).
Deploy steps at the bottom of this file.

The scan logic lives entirely in agent.py — this file is just the
Lambda wrapper so you don't duplicate code.
"""

import json
import logging
from agent import run_scan

log = logging.getLogger()
log.setLevel(logging.INFO)


def handler(event, context):
    """
    Lambda entry point.
    EventBridge passes an event dict — we ignore it and just run the scan.
    """
    log.info(f"Lambda triggered. Event: {json.dumps(event)}")
    try:
        run_scan()
        return {"statusCode": 200, "body": "Scan complete."}
    except Exception as e:
        log.error(f"Scan failed: {e}", exc_info=True)
        return {"statusCode": 500, "body": str(e)}


"""
════════════════════════════════════════════════════════════
 DEPLOYMENT GUIDE — AWS Lambda + EventBridge (free tier)
════════════════════════════════════════════════════════════

Prerequisites
─────────────
  pip install awscli
  aws configure          ← enter your AWS Access Key + region

Step 1 — Package the code
─────────────────────────
  pip install -r requirements.txt -t package/
  cp agent.py lambda_handler.py package/
  cd package && zip -r ../market-agent.zip . && cd ..

Step 2 — Create the Lambda function
────────────────────────────────────
  aws lambda create-function \\
    --function-name market-news-agent \\
    --runtime python3.12 \\
    --role arn:aws:iam::YOUR_ACCOUNT_ID:role/lambda-basic-role \\
    --handler lambda_handler.handler \\
    --zip-file fileb://market-agent.zip \\
    --timeout 120 \\
    --memory-size 256

  (To update after code changes:)
  aws lambda update-function-code \\
    --function-name market-news-agent \\
    --zip-file fileb://market-agent.zip

Step 3 — Set environment variables
───────────────────────────────────
  aws lambda update-function-configuration \\
    --function-name market-news-agent \\
    --environment "Variables={
      ANTHROPIC_API_KEY=sk-ant-...,
      WATCHLIST=AAPL,NVDA,Fed rates,
      SENSITIVITY=medium,
      MIN_IMPACT=medium,
      SLACK_WEBHOOK_URL=https://hooks.slack.com/...,
      EMAIL_TO=you@example.com,
      SENDGRID_API_KEY=SG.xxx
    }"

Step 4 — Schedule with EventBridge (every 30 min, market hours)
────────────────────────────────────────────────────────────────
  # Run every 30 min Mon–Fri 9:30–16:00 ET (14:30–21:00 UTC in summer)
  aws events put-rule \\
    --name market-agent-schedule \\
    --schedule-expression "cron(0,30 14-21 ? * MON-FRI *)" \\
    --state ENABLED

  aws lambda add-permission \\
    --function-name market-news-agent \\
    --statement-id market-agent-trigger \\
    --action lambda:InvokeFunction \\
    --principal events.amazonaws.com \\
    --source-arn arn:aws:events:us-east-1:YOUR_ACCOUNT_ID:rule/market-agent-schedule

  aws events put-targets \\
    --rule market-agent-schedule \\
    --targets "Id=1,Arn=arn:aws:lambda:us-east-1:YOUR_ACCOUNT_ID:function:market-news-agent"

Cost estimate (AWS Lambda free tier)
─────────────────────────────────────
  Lambda free tier: 1M requests/month + 400,000 GB-seconds
  At 20 scans/day × 22 trading days = 440 invocations/month → FREE
  Only the Claude API + notification services cost money (see README.md)

════════════════════════════════════════════════════════════
 ALTERNATIVE — Railway.app (easier, ~$5/mo)
════════════════════════════════════════════════════════════

1. Push this folder to a GitHub repo
2. Go to railway.app → New Project → Deploy from GitHub
3. Set all environment variables in the Railway dashboard
4. Set START_CMD = python agent.py
5. Railway keeps it running 24/7 with the built-in scheduler

════════════════════════════════════════════════════════════
 ALTERNATIVE — GitHub Actions (completely free)
════════════════════════════════════════════════════════════

Create .github/workflows/scan.yml in your repo (see README.md).
GitHub Actions cron runs your script on their servers — free for
public repos and 2,000 min/month for private repos.
"""
