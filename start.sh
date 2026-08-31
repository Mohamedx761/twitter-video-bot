#!/bin/sh
set -e

echo "Starting Telegram Bot API server on port 8081..."
export TELEGRAM_HTTP_PORT=8081
export TELEGRAM_LOCAL=True
export TELEGRAM_VERBOSITY=1

telegram-bot-api &
API_PID=$!
echo "Bot API server PID: $API_PID"

sleep 5

if ! kill -0 $API_PID 2>/dev/null; then
    echo "ERROR: Bot API server failed to start!"
    exit 1
fi

echo "Bot API server started. Starting bot..."
exec python3 -u bot.py
