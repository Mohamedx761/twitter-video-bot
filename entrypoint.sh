#!/bin/sh
set -e

LOCAL_PORT="8081"
CLEAN_API_ID=$(echo "${TELEGRAM_API_ID:-}" | tr -d ' "\r\n')
CLEAN_API_HASH=$(echo "${TELEGRAM_API_HASH:-}" | tr -d ' "\r\n')

USE_LOCAL="false"

if [ -n "${CLEAN_API_ID}" ] && [ -n "${CLEAN_API_HASH}" ]; then
    echo "[entrypoint] Checking telegram-bot-api binary..."
    if /usr/local/bin/telegram-bot-api --version; then
        echo "[entrypoint] telegram-bot-api binary verified. Starting supervisor on port ${LOCAL_PORT}..."
        
        start_server_loop() {
            set +e
            while true; do
                echo "[supervisor] Starting telegram-bot-api server on port ${LOCAL_PORT}..."
                /usr/local/bin/telegram-bot-api \
                    --api-id="${CLEAN_API_ID}" \
                    --api-hash="${CLEAN_API_HASH}" \
                    --http-port="${LOCAL_PORT}" \
                    --local \
                    --dir=/var/lib/telegram-bot-api \
                    --temp-dir=/tmp/telegram-bot-api \
                    --verbosity=1
                EXIT_CODE=$?
                echo "[supervisor] telegram-bot-api exited with code ${EXIT_CODE}. Respawning in 2s..."
                sleep 2
            done
        }

        start_server_loop &
        SUPERVISOR_PID=$!
        echo "[entrypoint] Supervisor PID: $SUPERVISOR_PID"

        echo "[entrypoint] Waiting for server on port ${LOCAL_PORT}..."
        RETRIES=0
        MAX_RETRIES=25
        while [ $RETRIES -lt $MAX_RETRIES ]; do
            HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:${LOCAL_PORT}/ 2>/dev/null || true)
            case "$HTTP_CODE" in
                200|404)
                    echo "[entrypoint] Local Bot API Server is ready!"
                    USE_LOCAL="true"
                    break
                    ;;
            esac
            RETRIES=$((RETRIES + 1))
            sleep 1
        done
        if [ "$USE_LOCAL" != "true" ]; then
            echo "[entrypoint] WARN: Local Bot API Server did not respond within timeout"
        fi
    else
        echo "[entrypoint] WARN: telegram-bot-api binary cannot execute on this system"
    fi
else
    echo "[entrypoint] TELEGRAM_API_ID or TELEGRAM_API_HASH not set — using Telegram Cloud API"
fi

if [ "$USE_LOCAL" = "true" ]; then
    export USE_LOCAL_SERVER="true"
    export LOCAL_API_URL="http://127.0.0.1:${LOCAL_PORT}"
    echo "[entrypoint] Starting bot with Local Bot API Server (2GB limit)..."
else
    export USE_LOCAL_SERVER="false"
    echo "[entrypoint] Starting bot with Telegram Cloud API (50MB limit + lossless video splitter fallback)..."
fi

exec python3 -u bot.py
