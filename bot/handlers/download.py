from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, Message

from services.compressor import compress_video
from services.downloader import DownloadError, download_media, get_direct_urls
from utils.config import Settings
from utils.messaging import edit_or_replace_status
from utils.urltools import normalize_http_url

logger = logging.getLogger(__name__)

router = Router(name="download")
_DOWNLOAD_SEMAPHORES: dict[int, asyncio.Semaphore] = {}


def _get_download_semaphore(limit: int) -> asyncio.Semaphore:
    sem = _DOWNLOAD_SEMAPHORES.get(limit)
    if sem is None:
        sem = asyncio.Semaphore(limit)
        _DOWNLOAD_SEMAPHORES[limit] = sem
    return sem


_URL_RE = re.compile(
    r"https?://[^\s<>\"]+|www\.[^\s<>\"]+",
    re.I,
)
_BARE_HOST = re.compile(
    r"\b(?:instagram|tiktok|twitter|x|youtube)\.com/[^\s<>\"]+|\byoutu\.be/[^\s<>\"]+",
    re.I,
)


def _extract_url(text: str) -> str | None:
    text = text.strip()
    m = _URL_RE.search(text)
    if m:
        return normalize_http_url(m.group(0).rstrip(").,]"))
    m2 = _BARE_HOST.search(text)
    if m2:
        return normalize_http_url("https://" + m2.group(0).rstrip(").,]"))
    return None


async def _safe_unlink(path: Path | None) -> None:
    if not path:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("Could not delete %s: %s", path, e)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Send a video link from <b>Instagram</b> (reel/story/post), <b>TikTok</b>, "
        "<b>X/Twitter</b>, or <b>YouTube</b>.\nI'll download and send the video here."
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await cmd_start(message)


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(
    message: Message,
    settings: Settings,
) -> None:
    url = _extract_url(message.text or "")
    if not url:
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_VIDEO)
    status = await message.reply("Downloading...")

    work_paths: list[Path] = []
    compressed_paths: list[Path] = []
    semaphore = _get_download_semaphore(settings.max_concurrent_downloads)

    try:
        if semaphore.locked():
            await edit_or_replace_status(
                status,
                "Another download is in progress. Your request is queued now...",
            )

        async with semaphore:
            result = await download_media(url, settings)
            work_paths = [p for p in result.paths if p and p.is_file()]
            if not work_paths and result.path and result.path.is_file():
                work_paths = [result.path]

            if not work_paths:
                direct_urls = await get_direct_urls(url, settings)
                lines = [
                    "Could not download a video file.",
                    "You can try opening this link in a browser:",
                ]
                for u in direct_urls[:3]:
                    lines.append(u)
                if not direct_urls:
                    lines.append("(No direct URL available.)")
                await edit_or_replace_status(status, "\n".join(lines))
                return

            logger.info("Download ok files=%s title=%s", len(work_paths), result.title)

            send_paths: list[Path] = []
            oversize_paths: list[Path] = []
            for source_path in work_paths:
                send_path = source_path
                size = source_path.stat().st_size

                if size > settings.telegram_max_file_bytes and settings.enable_compression:
                    compressed_path = source_path.with_name(source_path.stem + "_compressed.mp4")
                    ok = await compress_video(
                        source_path,
                        compressed_path,
                        settings.compress_target_bytes,
                    )
                    if ok and compressed_path.is_file():
                        new_size = compressed_path.stat().st_size
                        if new_size < size:
                            send_path = compressed_path
                            size = new_size
                            compressed_paths.append(compressed_path)
                            logger.info("Compressed to %s bytes (%s)", size, source_path.name)

                if size <= settings.telegram_max_file_bytes:
                    send_paths.append(send_path)
                else:
                    oversize_paths.append(send_path)

            caption = (result.title or "")[:1024]
            sent = 0
            for idx, send_path in enumerate(send_paths):
                await message.bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_VIDEO)
                vid = FSInputFile(send_path)
                await message.answer_video(
                    vid,
                    caption=(caption or None) if idx == 0 else None,
                    supports_streaming=True,
                    parse_mode=None,
                )
                sent += 1

            if sent and not oversize_paths:
                await status.delete()
            else:
                direct_urls = await get_direct_urls(url, settings)
                if sent:
                    parts = [
                        f"Sent {sent} video(s).",
                        f"{len(oversize_paths)} file(s) are still too large for Telegram "
                        f"(limit ~{settings.telegram_max_file_bytes // (1024*1024)} MB).",
                        "Download links:",
                    ]
                else:
                    largest = max((p.stat().st_size for p in oversize_paths), default=0)
                    parts = [
                        f"File is too large for Telegram ({largest // (1024*1024)} MB, "
                        f"limit ~{settings.telegram_max_file_bytes // (1024*1024)} MB).",
                        "Download links:",
                    ]

                for u in direct_urls[:5]:
                    parts.append(u)
                if not direct_urls:
                    parts.append(
                        "No stable direct URL. Try a shorter clip or download on a PC with yt-dlp."
                    )
                await edit_or_replace_status(status, "\n".join(parts))

    except DownloadError as e:
        logger.warning("DownloadError: %s", e)
        await edit_or_replace_status(status, str(e))
    except Exception:
        logger.exception("Handler error")
        await edit_or_replace_status(status, "Something went wrong. Please try again later.")
    finally:
        for p in [*compressed_paths, *work_paths]:
            await _safe_unlink(p)
