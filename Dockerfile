FROM aiogram/telegram-bot-api:latest AS api-server

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    openssl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY --from=api-server /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY start.sh .
RUN chmod +x start.sh

ENV TELEGRAM_API_PORT=8081

EXPOSE 8081

CMD ["./start.sh"]
