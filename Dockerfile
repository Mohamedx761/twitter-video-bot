FROM python:3.12-alpine

RUN apk add --no-cache ffmpeg

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY cookies.txt .
COPY start.sh .
RUN chmod +x start.sh

ENTRYPOINT ["./start.sh"]
