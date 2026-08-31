FROM aiogram/telegram-bot-api:latest AS api-server

FROM python:3.11-alpine

RUN apk add --no-cache openssl ca-certificates

COPY --from=api-server /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

WORKDIR /app

COPY requirements.txt .
RUN apk add --no-cache gcc musl-dev libffi-dev && \
    pip install --no-cache-dir -r requirements.txt && \
    apk del gcc musl-dev libffi-dev

COPY bot.py .
COPY start.sh .
RUN chmod +x start.sh

ENV TELEGRAM_API_PORT=8081

EXPOSE 8081

CMD ["./start.sh"]
