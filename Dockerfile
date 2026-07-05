FROM python:3.12-slim

LABEL org.opencontainers.image.title="LinkToClip" \
      org.opencontainers.image.description="Telegram bot that downloads videos & photos from Instagram, TikTok, X, and YouTube" \
      org.opencontainers.image.source="https://github.com/m0hx65/LinkToClip" \
      org.opencontainers.image.licenses="MIT"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["python", "-m", "bot.main"]
