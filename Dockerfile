FROM ghcr.io/gramiojs/telegram-bot-api:latest AS server

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=server /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api
RUN chmod +x /usr/local/bin/telegram-bot-api

RUN mkdir -p /var/lib/telegram-bot-api /tmp/telegram-bot-api

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY cookies.txt .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

EXPOSE 8081

ENTRYPOINT ["./entrypoint.sh"]
