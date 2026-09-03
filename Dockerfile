FROM aiogram/telegram-bot-api:latest

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY cookies.txt .
COPY start.sh .
RUN chmod +x start.sh

RUN mkdir -p /tmp/tgapi_db

ENTRYPOINT ["./start.sh"]
