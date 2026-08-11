from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from utils.config import Settings

logger = logging.getLogger(__name__)

# The Bot API caps uploads at ~50 MB, but the same bot token can upload up to
# 2000 MB over MTProto — the file bytes are sent as-is, no re-encoding. This
# module holds one lazily started Telethon client used only for those uploads;
# aiogram keeps handling updates via getUpdates, the two don't conflict.

_client: Any = None
_client_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _client_lock
    if _client_lock is None:
        _client_lock = asyncio.Lock()
    return _client_lock


async def _get_client(settings: Settings) -> Any:
    global _client
    from telethon import TelegramClient

    async with _get_lock():
        if _client is not None and _client.is_connected():
            return _client
        settings.mtproto_session_dir.mkdir(parents=True, exist_ok=True)
        session_path = settings.mtproto_session_dir / "mtproto_bot"
        client = TelegramClient(str(session_path), settings.api_id, settings.api_hash)
        await client.start(bot_token=settings.bot_token)
        _client = client
        logger.info("MTProto client connected (large uploads enabled)")
        return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:
            logger.debug("MTProto disconnect failed", exc_info=True)
        _client = None


async def _resolve_peer(client: Any, chat_id: int) -> Any:
    from telethon import utils as tl_utils
    from telethon.tl import types as tl_types

    try:
        return await client.get_input_entity(chat_id)
    except Exception:
        pass
    # Bot accounts may use access_hash=0 for peers the bot has already
    # interacted with — always true here, since the request arrived as a
    # message to this bot in that same chat.
    real_id, peer_type = tl_utils.resolve_id(chat_id)
    if peer_type is tl_types.PeerChannel:
        return tl_types.InputPeerChannel(real_id, access_hash=0)
    if peer_type is tl_types.PeerChat:
        return tl_types.InputPeerChat(real_id)
    return tl_types.InputPeerUser(real_id, access_hash=0)


def _probe_video(path: Path) -> tuple[int, int, int]:
    """(width, height, duration_s), zeros when ffprobe is unavailable."""
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-show_entries", "format=duration",
                "-of", "json",
                str(path),
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        data = json.loads(out)
        stream = (data.get("streams") or [{}])[0]
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        duration = int(float((data.get("format") or {}).get("duration") or 0))
        return width, height, duration
    except Exception:
        return 0, 0, 0


def _make_thumb(path: Path) -> Path | None:
    thumb = path.with_name(path.stem + "_thumb.jpg")
    cmd = [
        "ffmpeg", "-y",
        "-ss", "1",
        "-i", str(path),
        "-frames:v", "1",
        "-vf", "scale='min(320,iw)':-2",
        str(thumb),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return thumb if thumb.is_file() and thumb.stat().st_size > 0 else None


def _make_progress_callback(
    status_update: Callable[[str], Awaitable[None]] | None,
    total_bytes: int,
) -> Callable[[int, int], None] | None:
    if status_update is None:
        return None
    loop = asyncio.get_running_loop()
    total_mb = max(total_bytes // (1024 * 1024), 1)
    last_edit = 0.0

    def callback(sent: int, _total: int) -> None:
        nonlocal last_edit
        now = time.monotonic()
        if now - last_edit < 10:
            return
        last_edit = now
        pct = min(int(sent * 100 / max(total_bytes, 1)), 100)
        loop.create_task(status_update(f"Uploading {total_mb} MB... {pct}%"))

    return callback


async def send_large_video(
    settings: Settings,
    chat_id: int,
    path: Path,
    caption: str | None,
    status_update: Callable[[str], Awaitable[None]] | None = None,
    reply_to: int | None = None,
) -> bool:
    """Upload a video over MTProto, bypassing the Bot API's ~50 MB limit.

    Sends the file exactly as downloaded (no re-encoding). `reply_to` is the
    message id carrying the original link, so large videos are threaded under
    it like every other result; Telegram drops the reply header rather than
    failing if that message is gone. Returns False on any failure so the
    caller can fall back to splitting.
    """
    if not settings.mtproto_enabled:
        return False

    from telethon.tl.types import DocumentAttributeVideo

    thumb: Path | None = None
    try:
        client = await _get_client(settings)
        peer = await _resolve_peer(client, chat_id)

        width, height, duration = await asyncio.to_thread(_probe_video, path)
        thumb = await asyncio.to_thread(_make_thumb, path)

        size = path.stat().st_size
        if status_update is not None:
            await status_update(f"Uploading {max(size // (1024 * 1024), 1)} MB... 0%")

        await client.send_file(
            peer,
            str(path),
            caption=caption or None,
            attributes=[
                DocumentAttributeVideo(
                    duration=duration,
                    w=width,
                    h=height,
                    supports_streaming=True,
                )
            ],
            thumb=str(thumb) if thumb else None,
            progress_callback=_make_progress_callback(status_update, size),
            reply_to=reply_to,
        )
        logger.info("MTProto upload ok: %s (%d bytes)", path.name, size)
        return True
    except Exception:
        logger.exception("MTProto upload failed for %s", path)
        return False
    finally:
        if thumb is not None:
            try:
                thumb.unlink(missing_ok=True)
            except OSError:
                pass
