#!/bin/bash
set -e

echo "[entrypoint] Starting Telegram Bot API Server..."

telegram-bot-api \
    --api_id="${TELEGRAM_API_ID}" \
    --api_hash="${TELEGRAM_API_HASH}" \
    --http-port=8081 \
    --local \
    --dir=/var/lib/telegram-bot-api \
    --temp-dir=/tmp/telegram-bot-api \
    --verbosity=1 &

SERVER_PID=$!
echo "[entrypoint] Server PID: $SERVER_PID"

echo "[entrypoint] Waiting for server on port 8081..."
RETRIES=0
MAX_RETRIES=45
until curl -s -o /dev/null -w "%{http_code}" http://localhost:8081/ 2>/dev/null | grep -q "404\|200"; do
    RETRIES=$((RETRIES + 1))
    if [ $RETRIES -ge $MAX_RETRIES ]; then
        echo "[entrypoint] WARN: Server not ready after ${MAX_RETRIES}s — starting bot with cloud API fallback"
        export USE_LOCAL_SERVER="false"
        break
    fi
    if ! kill -0 $SERVER_PID 2>/dev/null; then
        echo "[entrypoint] WARN: Server process exited — starting bot with cloud API fallback"
        export USE_LOCAL_SERVER="false"
        break
    fi
    sleep 1
done

if curl -s -o /dev/null -w "%{http_code}" http://localhost:8081/ 2>/dev/null | grep -q "404\|200"; then
    echo "[entrypoint] Server is ready!"
    export USE_LOCAL_SERVER="true"
    export LOCAL_API_URL="http://localhost:8081"
fi

echo "[entrypoint] Starting bot..."
exec python3 -u bot.py
