## Cursor Cloud specific instructions

### Overview

Single-process async Telegram bot (aiogram 3 + yt-dlp) that downloads videos from Instagram, TikTok, and X/Twitter. No database, no docker-compose, no web frontend.

### Running the bot

```bash
source .venv/bin/activate
python -m bot.main
```

Requires `BOT_TOKEN` env var (Telegram bot token from @BotFather). Without it the process exits with `RuntimeError: BOT_TOKEN is required`.

### Linting

No linter is configured in the repo. Use `ruff check .` (installed in the venv). Pre-existing E402 warnings in `bot/health_server.py` are benign (docstring before imports).

### Testing

No test framework or test files exist in the repo. Core logic can be verified with inline Python assertions against the `platforms.detector`, `utils.urltools`, and `utils.messaging` modules.

### Key dependencies

- **Python 3.12** (system), **ffmpeg/ffprobe** (system, for video compression)
- Python packages: `aiogram`, `yt-dlp`, `python-dotenv` (see `requirements.txt`)

### Environment variables

See `README.md` → "Environment variables" table. Only `BOT_TOKEN` is required.
