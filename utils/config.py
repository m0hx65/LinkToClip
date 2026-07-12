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
    enable_compression: bool
    cookies_file: Path | None
    tiktok_cookies_file: Path | None
    twitter_cookies_file: Path | None
    youtube_cookies_file: Path | None
    max_concurrent_downloads: int
    api_id: int | None
    api_hash: str | None
    mtproto_max_file_bytes: int
    mtproto_session_dir: Path

    @property
    def mtproto_enabled(self) -> bool:
        """Large files can be uploaded over MTProto (up to ~2 GB, no re-encoding)."""
        return self.api_id is not None and bool(self.api_hash)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is required")

    td = os.getenv("TEMP_DIR", "").strip()
    temp_dir = Path(td) if td else Path.cwd() / "data" / "temp"

    cookies = os.getenv("COOKIES_FILE", "").strip()
    cookies_path = Path(cookies) if cookies else None

    tt_cookies = os.getenv("TIKTOK_COOKIES_FILE", "").strip()
    tiktok_cookies_path = Path(tt_cookies) if tt_cookies else None

    tw_cookies = os.getenv("TWITTER_COOKIES_FILE", "").strip()
    twitter_cookies_path = Path(tw_cookies) if tw_cookies else None

    yt_cookies = os.getenv("YOUTUBE_COOKIES_FILE", "").strip()
    youtube_cookies_path = Path(yt_cookies) if yt_cookies else None

    api_id_raw = os.getenv("API_ID", "").strip()
    api_id = int(api_id_raw) if api_id_raw else None
    api_hash = os.getenv("API_HASH", "").strip() or None

    session_dir_raw = os.getenv("MTPROTO_SESSION_DIR", "").strip()
    mtproto_session_dir = Path(session_dir_raw) if session_dir_raw else Path.cwd() / "data"

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
        enable_compression=_env_bool("ENABLE_COMPRESSION", False),
        cookies_file=cookies_path,
        tiktok_cookies_file=tiktok_cookies_path,
        twitter_cookies_file=twitter_cookies_path,
        youtube_cookies_file=youtube_cookies_path,
        max_concurrent_downloads=max(int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "1")), 1),
        api_id=api_id,
        api_hash=api_hash,
        # MTProto bot upload ceiling is 2000 MB; leave a small margin.
        mtproto_max_file_bytes=int(
            os.getenv("MTPROTO_MAX_FILE_BYTES", str(1990 * 1024 * 1024))
        ),
        mtproto_session_dir=mtproto_session_dir,
    )
