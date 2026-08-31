#!/bin/bash
set -e

echo "Starting Telegram Bot API server on port 8081..."
telegram-bot-api \
  --api-id="$TELEGRAM_API_ID" \
  --api-hash="$TELEGRAM_API_HASH" \
  --http-port=8081 \
  --local \
  --verbosity=1 &

API_PID=$!
echo "Bot API server PID: $API_PID"

sleep 5

if ! kill -0 $API_PID 2>/dev/null; then
    echo "ERROR: Bot API server failed to start!"
    exit 1
fi

echo "Bot API server started. Starting bot..."
exec python bot.py
