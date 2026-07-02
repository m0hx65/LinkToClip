from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, InputMediaPhoto, Message

from services.compressor import compress_video, split_video
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


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _is_image(path: Path) -> bool:
    return path.suffix.lower() in _IMAGE_EXTS


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
        "Send a link from <b>Instagram</b> (reel/post/carousel/story/highlight), <b>TikTok</b>, "
        "<b>X/Twitter</b>, or <b>YouTube</b>.\n"
        "I'll download and send the video or photos here."
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
    part_paths: list[Path] = []
    semaphore = _get_download_semaphore(settings.max_concurrent_downloads)

    try:
        if semaphore.locked():
            await edit_or_replace_status(
                status,
                "Another download is in progress. Your request is queued now...",
            )

        async with semaphore:
            _MAX_RETRIES = 3
            result = None
            for _attempt in range(_MAX_RETRIES + 1):
                try:
                    result = await download_media(url, settings)
                    break
                except DownloadError as e:
                    if not e.retryable or _attempt >= _MAX_RETRIES:
                        raise
                    await edit_or_replace_status(
                        status,
                        f"Download failed, retrying... ({_attempt + 1}/{_MAX_RETRIES})",
                    )
                    await asyncio.sleep(3)

            work_paths = [p for p in result.paths if p and p.is_file()]
            if not work_paths and result.path and result.path.is_file():
                work_paths = [result.path]

            if not work_paths:
                direct_urls = await get_direct_urls(url, settings)
                lines = [
                    "Could not download the media file.",
                    "You can try opening this link in a browser:",
                ]
                for u in direct_urls[:3]:
                    lines.append(u)
                if not direct_urls:
                    lines.append("(No direct URL available.)")
                await edit_or_replace_status(status, "\n".join(lines))
                return

            logger.info("Download ok files=%s title=%s", len(work_paths), result.title)

            photo_paths = [p for p in work_paths if _is_image(p)]
            video_paths = [p for p in work_paths if not _is_image(p)]

            caption = (result.title or "")[:1024]
            caption_used = False
            sent_anything = False

            # --- Send photos (single or carousel groups of up to 10) ---
            if photo_paths:
                await message.bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_PHOTO)
                for chunk_start in range(0, len(photo_paths), 10):
                    chunk = photo_paths[chunk_start:chunk_start + 10]
                    cap = (caption or None) if not caption_used else None
                    if len(chunk) == 1:
                        await message.answer_photo(
                            FSInputFile(chunk[0]),
                            caption=cap,
                            parse_mode=None,
                        )
                    else:
                        media = [
                            InputMediaPhoto(
                                media=FSInputFile(p),
                                caption=(cap if j == 0 else None),
                                parse_mode=None,
                            )
                            for j, p in enumerate(chunk)
                        ]
                        await message.answer_media_group(media)
                    caption_used = True
                    sent_anything = True

            # --- Send videos (with optional compression / splitting) ---
            send_items: list[tuple[Path, str | None]] = []
            for source_path in video_paths:
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
                            source_path = compressed_path
                            size = new_size
                            compressed_paths.append(compressed_path)
                            logger.info("Compressed to %s bytes", size)

                if size <= settings.telegram_max_file_bytes:
                    send_items.append((source_path, None))
                else:
                    await edit_or_replace_status(
                        status,
                        f"File is {size // (1024*1024)} MB — splitting into parts...",
                    )
                    parts = await split_video(
                        source_path, source_path.parent, settings.telegram_max_file_bytes
                    )
                    if parts:
                        n = len(parts)
                        for i, p in enumerate(parts, 1):
                            send_items.append((p, f"Part {i}/{n}"))
                        part_paths.extend(parts)
                        logger.info("Split into %d parts: %s", n, source_path.name)
                    else:
                        logger.warning("Split failed for %s", source_path)

            for send_path, part_label in send_items:
                await message.bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_VIDEO)
                vid = FSInputFile(send_path)
                if not caption_used:
                    cap = f"{caption}\n{part_label}" if (caption and part_label) else (caption or part_label or None)
                    caption_used = True
                else:
                    cap = part_label
                await message.answer_video(
                    vid,
                    caption=cap,
                    supports_streaming=True,
                    parse_mode=None,
                )
                sent_anything = True

            if sent_anything:
                await status.delete()
            else:
                direct_urls = await get_direct_urls(url, settings)
                lines = ["Could not send the file (splitting failed). Download links:"]
                for u in direct_urls[:5]:
                    lines.append(u)
                if not direct_urls:
                    lines.append("No stable direct URL. Try downloading on a PC with yt-dlp.")
                await edit_or_replace_status(status, "\n".join(lines))

    except DownloadError as e:
        logger.warning("DownloadError: %s", e)
        await edit_or_replace_status(status, str(e))
    except Exception:
        logger.exception("Handler error")
        await edit_or_replace_status(status, "Something went wrong. Please try again later.")
    finally:
        for p in [*part_paths, *compressed_paths, *work_paths]:
            await _safe_unlink(p)
