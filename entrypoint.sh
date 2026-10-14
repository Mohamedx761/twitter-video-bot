#!/bin/sh
set -e

LOCAL_PORT="8081"

if [ -n "${TELEGRAM_API_ID}" ] && [ -n "${TELEGRAM_API_HASH}" ]; then
    echo "[entrypoint] Starting Telegram Bot API Server on port ${LOCAL_PORT}..."
    telegram-bot-api \
        --api-id="${TELEGRAM_API_ID}" \
        --api-hash="${TELEGRAM_API_HASH}" \
        --http-port="${LOCAL_PORT}" \
        --local \
        --dir=/var/lib/telegram-bot-api \
        --temp-dir=/tmp/telegram-bot-api \
        --verbosity=1 &

    SERVER_PID=$!
    echo "[entrypoint] Server PID: $SERVER_PID"

    echo "[entrypoint] Waiting for server on port ${LOCAL_PORT}..."
    RETRIES=0
    MAX_RETRIES=30
    while [ $RETRIES -lt $MAX_RETRIES ]; do
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:${LOCAL_PORT}/ 2>/dev/null || true)
        case "$HTTP_CODE" in
            200|404)
                echo "[entrypoint] Local Bot API Server is ready!"
                export USE_LOCAL_SERVER="true"
                export LOCAL_API_URL="http://localhost:${LOCAL_PORT}"
                echo "[entrypoint] Starting bot..."
                exec python3 -u bot.py
                ;;
        esac
        RETRIES=$((RETRIES + 1))
        if ! kill -0 $SERVER_PID 2>/dev/null; then
            echo "[entrypoint] WARN: Server process exited — falling back to cloud API"
            break
        fi
        sleep 1
    done
else
    echo "[entrypoint] TELEGRAM_API_ID or TELEGRAM_API_HASH not set — using Telegram Cloud API fallback"
fi

export USE_LOCAL_SERVER="false"
echo "[entrypoint] Starting bot with Cloud API..."
exec python3 -u bot.py