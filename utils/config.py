from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    log_level: str
    temp_dir: Path
    telegram_max_file_bytes: int
    compress_target_bytes: int
    cookies_file: Path | None


def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is required")

    td = os.getenv("TEMP_DIR", "").strip()
    temp_dir = Path(td) if td else Path.cwd() / "data" / "temp"

    cookies = os.getenv("COOKIES_FILE", "").strip()
    cookies_path = Path(cookies) if cookies else None

    return Settings(
        bot_token=token,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        temp_dir=temp_dir,
        telegram_max_file_bytes=int(
            os.getenv("TELEGRAM_MAX_FILE_BYTES", str(49 * 1024 * 1024))
        ),
        compress_target_bytes=int(
            os.getenv("COMPRESS_TARGET_BYTES", str(46 * 1024 * 1024))
        ),
        cookies_file=cookies_path,
    )
