FROM ghcr.io/gramiojs/telegram-bot-api:latest AS server

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=server /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api
RUN chmod +x /usr/local/bin/telegram-bot-api

COPY --from=server /lib/ld-musl* /lib/
COPY --from=server /lib/ /musl-lib/
COPY --from=server /usr/lib/ /musl-usr-lib/

RUN mkdir -p /etc && \
    echo "/musl-lib" > /etc/ld-musl-x86_64.path && \
    echo "/musl-usr-lib" >> /etc/ld-musl-x86_64.path && \
    echo "/musl-lib" > /etc/ld-musl-aarch64.path && \
    echo "/musl-usr-lib" >> /etc/ld-musl-aarch64.path

RUN mkdir -p /var/lib/telegram-bot-api /tmp/telegram-bot-api

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY cookies.txt .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

EXPOSE 10000 7860 8080

ENTRYPOINT ["./entrypoint.sh"]