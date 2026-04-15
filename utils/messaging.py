from __future__ import annotations

from aiogram.types import Message

TG_MAX = 4000


def chunk_text(text: str, max_len: int = TG_MAX) -> list[str]:
    if len(text) <= max_len:
        return [text]
    return [text[i : i + max_len] for i in range(0, len(text), max_len)]


async def edit_or_replace_status(status: Message, text: str) -> None:
    parts = chunk_text(text)
    try:
        await status.edit_text(parts[0], parse_mode=None)
    except Exception:
        await status.answer(parts[0], parse_mode=None)
    for extra in parts[1:]:
        await status.answer(extra, parse_mode=None)
