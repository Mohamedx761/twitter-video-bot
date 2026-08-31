FROM aiogram/telegram-bot-api:latest

USER root

RUN apk add --no-cache python3 py3-pip py3-certifi py3-openssl

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --break-system-packages --no-cache-dir -r requirements.txt

COPY bot.py .
COPY start.sh .
RUN chmod +x start.sh

ENV TELEGRAM_API_PORT=8081

EXPOSE 8081

ENTRYPOINT ["./start.sh"]
