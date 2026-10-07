#!/bin/sh
set -e

echo "[entrypoint] Starting Telegram Bot API Server..."

telegram-bot-api \
    --api-id="${TELEGRAM_API_ID}" \
    --api-hash="${TELEGRAM_API_HASH}" \
    --http-port=7860 \
    --local \
    --dir=/var/lib/telegram-bot-api \
    --temp-dir=/tmp/telegram-bot-api \
    --verbosity=1 &

SERVER_PID=$!
echo "[entrypoint] Server PID: $SERVER_PID"

echo "[entrypoint] Waiting for server on port 7860..."
RETRIES=0
MAX_RETRIES=30
while [ $RETRIES -lt $MAX_RETRIES ]; do
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:7860/ 2>/dev/null || true)
    case "$HTTP_CODE" in
        200|404)
            echo "[entrypoint] Server is ready!"
            export USE_LOCAL_SERVER="true"
            export LOCAL_API_URL="http://localhost:7860"
            echo "[entrypoint] Starting bot..."
            exec python3 -u bot.py
            ;;
    esac
    RETRIES=$((RETRIES + 1))
    if ! kill -0 $SERVER_PID 2>/dev/null; then
        echo "[entrypoint] WARN: Server process exited — starting bot with cloud API fallback"
        export USE_LOCAL_SERVER="false"
        break
    fi
    sleep 1
done

echo "[entrypoint] WARN: Server not ready after ${MAX_RETRIES}s — starting bot with cloud API fallback"
export USE_LOCAL_SERVER="false"
echo "[entrypoint] Starting bot..."
exec python3 -u bot.py