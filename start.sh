#!/bin/sh
echo "Starting Telegram Local Bot API Server..."
telegram-bot-api \
    --api-id=${TELEGRAM_API_ID} \
    --api-hash=${TELEGRAM_API_HASH} \
    --http-port=8081 \
    --local \
    --dir=/tmp/tgapi_db &
    
echo "Waiting for Local Bot API Server..."
for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -s http://localhost:8081/ >/dev/null 2>&1; then
        echo "Local Bot API Server is ready!"
        break
    fi
    echo "Waiting... ($i)"
    sleep 1
done

echo "Starting bot..."
exec python3 -u bot.py 2>&1
