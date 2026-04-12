# Social video Telegram bot

Async Telegram bot (aiogram 3 + yt-dlp) that downloads videos from Instagram, TikTok, and X/Twitter and sends them in chat. Files over the Bot API limit are re-encoded with **ffmpeg** when possible, otherwise direct download links are returned.

## Prerequisites

- Python 3.11+
- [ffmpeg](https://ffmpeg.org/) and **ffprobe** on `PATH` (required for compression)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Local setup

1. Clone or copy this project and enter the folder.

2. Create a virtual environment (recommended):

   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   ```

   On Linux/macOS: `source .venv/bin/activate`

3. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Install **ffmpeg** (Windows: [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) or `choco install ffmpeg`; macOS: `brew install ffmpeg`; Linux: `sudo apt install ffmpeg`).

5. Copy `.env.example` to `.env` and set `BOT_TOKEN`.

6. Run:

   ```bash
   python -m bot.main
   ```

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `BOT_TOKEN` | Yes | Telegram bot token |
| `LOG_LEVEL` | No | Default `INFO` |
| `TEMP_DIR` | No | Download directory (default `./data/temp`) |
| `TELEGRAM_MAX_FILE_BYTES` | No | Max size to upload (~49MB default) |
| `COMPRESS_TARGET_BYTES` | No | Target size when re-encoding |
| `COOKIES_FILE` | No | Netscape cookies file for private Instagram / restricted content |

## Deployment

### Docker (any host)

```bash
docker build -t tg-video-bot .
docker run --env-file .env tg-video-bot
```

### Render (free tier)

1. Push the repo to GitHub/GitLab.
2. New **Background Worker** (not a Web Service).
3. Connect the repository.
4. **Docker** build: use the included `Dockerfile`, or **Native** with build command `pip install -r requirements.txt` and add **ffmpeg** via `apt` in a `render.yaml` or custom build script.
5. **Start command:** `python -m bot.main`
6. Add environment variable `BOT_TOKEN`.

Note: Render’s free workers may sleep; use a paid worker or an external cron ping if you need 24/7 uptime.

### Railway

1. New project → **Deploy from GitHub** (or upload).
2. **Variables:** add `BOT_TOKEN`.
3. If not using Docker, add **ffmpeg** via [Nixpacks config](https://docs.railway.app/deploy/builds) or switch to the provided **Dockerfile** (recommended).
4. **Start command:** `python -m bot.main` (Railway runs this from the repo root).

### Limits

- Telegram Bot API upload limit is **50MB**. Larger files are compressed when ffmpeg is available; if still too large, the bot sends **direct URLs** from yt-dlp metadata when possible.
- For **2GB uploads** you would need a local Bot API server or MTProto client; this project does not implement that.

## Project layout

- `bot/` — aiogram handlers and `main`
- `services/` — yt-dlp download + ffmpeg compression
- `platforms/` — URL detection and per-platform yt-dlp options
- `utils/` — config, logging, messaging helpers

## Legal

Only download content you are allowed to access. Respect platform Terms of Service and copyright.
