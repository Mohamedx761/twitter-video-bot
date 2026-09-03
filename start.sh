#!/bin/sh
echo "Starting Telegram Local Bot API Server..."
telegram-bot-api \
    --api_id=${API_ID} \
    --api_hash=${API_HASH} \
    --http-port=8081 \
    --local \
    --db_directory=/tmp/tgapi_db \
    --max_webhook_connections=40 &
sleep 3
echo "Starting bot..."
exec python3 -u bot.py 2>&1
