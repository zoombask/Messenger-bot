#!/bin/bash
# Render start script: builds .env from Render environment variables,
# then starts the Messenger webhook. Secrets are set in Render dashboard,
# never committed to git.
set -e
cd "$(dirname "$0")"

# bot.py lives one level up; copy it here so the deploy is self-contained
# (Render deploys this folder as the repo root).
if [ ! -f bot.py ] && [ -f ../bot.py ]; then
  cp ../bot.py ./bot.py
fi
if [ ! -f products.json ] && [ -f ../products.json ]; then
  cp ../products.json ./products.json
fi

# Generate .env from environment variables (Render dashboard -> Environment)
cat > .env <<EOF
TELEGRAM_TOKEN=${TELEGRAM_TOKEN:-}
TELEGRAM_ADMIN_ID=${TELEGRAM_ADMIN_ID:-}
GEMINI_KEY=${GEMINI_KEY:-}
TAVILY_KEY=${TAVILY_KEY:-}
SHEET_CSV_URL=${SHEET_CSV_URL:-}
MSGR_PAGE_TOKEN=${MSGR_PAGE_TOKEN:-}
MSGR_VERIFY_TOKEN=${MSGR_VERIFY_TOKEN:-}
MSGR_APP_SECRET=${MSGR_APP_SECRET:-}
EOF
chmod 600 .env

exec python3 messenger_webhook.py
