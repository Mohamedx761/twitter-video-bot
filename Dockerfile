FROM ghcr.io/jakbin/telegram-bot-api-binary:main AS server-src

FROM ubuntu:24.04

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python-is-python3 \
    ffmpeg \
    curl \
    ca-certificates \
    libssl3 \
    zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY --from=server-src /telegram-bot-api /usr/local/bin/telegram-bot-api
RUN chmod +x /usr/local/bin/telegram-bot-api

RUN mkdir -p /var/lib/telegram-bot-api /tmp/telegram-bot-api

WORKDIR /app

COPY requirements.txt .
RUN pip install --break-system-packages --no-cache-dir -r requirements.txt

COPY bot.py .
COPY cookies.txt .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

EXPOSE 10000 7860 8080

ENTRYPOINT ["./entrypoint.sh"]