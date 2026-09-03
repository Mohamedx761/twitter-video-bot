FROM aiogram/telegram-bot-api:latest AS tgapi

FROM python:3.12-alpine

RUN apk add --no-cache ffmpeg libstdc++

COPY --from=tgapi /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY cookies.txt .
COPY start.sh .
RUN chmod +x start.sh

RUN mkdir -p /tmp/tgapi_db

ENTRYPOINT ["./start.sh"]
