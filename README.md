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
| `COOKIES_FILE` | No | Netscape `cookies.txt` from a browser session on **instagram.com** — often needed on **cloud hosts** even for **public** reels (Instagram blocks datacenter IPs without a session) |

## Deployment

### Docker (any host)

```bash
docker build -t tg-video-bot .
docker run --env-file .env tg-video-bot
```

### Render

1. Push the repo to GitHub/GitLab.
2. **Background Worker** (recommended for polling) **or** **Web Service** (free tier often uses this).
3. Connect the repository and use the included **Dockerfile** (includes ffmpeg).
4. **Start command:** `python -m bot.main`
5. Add environment variable `BOT_TOKEN`.

**Web Service:** Bind the public HTTP server to **`0.0.0.0`** and the **`PORT`** env var (Render defaults to **10000** if unset). The bot exposes `GET /` → `ok` on that port; Telegram still uses **long polling**. Locally, leave **`PORT`** and **`RENDER`** unset so only polling runs.

After deploy, Render shows a **primary URL** (e.g. `https://your-service.onrender.com`). Open it in a browser — you should see plain text **`ok`**. That only confirms the health server; Telegram still reaches the bot via polling, not through that URL.

Note: Free tiers may sleep or spin down; the bot may be slow to respond until the instance wakes.

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
