#!/bin/sh

echo "=== start.sh BEGIN ==="
echo "TELEGRAM_API_ID=$TELEGRAM_API_ID"
echo "TELEGRAM_API_HASH=$TELEGRAM_API_HASH"
echo "BOT_TOKEN=${BOT_TOKEN:0:10}..."

export TELEGRAM_HTTP_PORT=8081
export TELEGRAM_LOCAL=True
export TELEGRAM_VERBOSITY=1

echo "Starting Telegram Bot API server on port 8081..."
telegram-bot-api &
API_PID=$!
echo "Bot API server PID: $API_PID"

sleep 8

if kill -0 $API_PID 2>/dev/null; then
    echo "Bot API server is running"
else
    echo "ERROR: Bot API server died!"
fi

echo "Starting bot..."
exec python3 -u bot.py 2>&1
