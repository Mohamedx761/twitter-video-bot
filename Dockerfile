FROM ubuntu:22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git libssl-dev zlib1g-dev \
    ca-certificates pkg-config wget \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 https://github.com/tdlib/telegram-bot-api.git /tmp/telegram-bot-api

WORKDIR /tmp/telegram-bot-api

RUN mkdir -p build && cd build \
    && cmake -DCMAKE_BUILD_TYPE=Release \
             -DCMAKE_INSTALL_PREFIX=/usr/local \
             .. \
    && cmake --build . --target install -j$(nproc)

RUN ls -la /usr/local/bin/telegram-bot-api

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

RUN chmod +x /usr/local/bin/telegram-bot-api

RUN useradd -m telegram-bot-api && \
    mkdir -p /var/lib/telegram-bot-api /tmp/telegram-bot-api && \
    chown -R telegram-bot-api:telegram-bot-api /var/lib/telegram-bot-api /tmp/telegram-bot-api

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY cookies.txt .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

EXPOSE 8081

ENTRYPOINT ["./entrypoint.sh"]
