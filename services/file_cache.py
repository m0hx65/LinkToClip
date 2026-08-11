"""Remember what this bot has already sent, so a repeated link costs nothing.

Once Telegram holds a file, resending it by `file_id` re-delivers it with no
upload and no size limit — the cheapest send there is. That matters because
files over the 20 MB URL-send cap are uploaded from this host, so a link sent
twice is its whole size in outbound bandwidth, twice.

The cache lives in memory only. A redeploy or an idle spin-down clears it,
which is fine for what it is meant to catch: the same link shared again while
a conversation is still going. Surviving restarts would need a persistent
disk, which is a paid instance — not worth it for a hit rate that is already
concentrated in the minutes after a first send.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Each entry is a handful of short strings, so this is kilobytes at most. The
# bound exists to stop a long-lived process growing without limit.
_MAX_ENTRIES = 512


@dataclass(frozen=True)
class CachedItem:
    """One piece of media Telegram already stores for this bot."""

    kind: str  # "video" or "photo"
    file_id: str


@dataclass(frozen=True)
class CachedMedia:
    items: tuple[CachedItem, ...]
    caption: str | None


_entries: OrderedDict[str, CachedMedia] = OrderedDict()


def get(key: str) -> CachedMedia | None:
    """Look up a previous send, marking it as recently used."""
    hit = _entries.get(key)
    if hit is not None:
        _entries.move_to_end(key)
        logger.info("Cache hit for %s (%d file(s), no download needed)", key, len(hit.items))
    return hit


def put(key: str, items: list[CachedItem], caption: str | None) -> None:
    if not items:
        return
    _entries[key] = CachedMedia(items=tuple(items), caption=caption)
    _entries.move_to_end(key)
    while len(_entries) > _MAX_ENTRIES:
        _entries.popitem(last=False)


def drop(key: str) -> None:
    """Forget an entry whose file_ids Telegram no longer accepts."""
    if _entries.pop(key, None) is not None:
        logger.info("Dropped stale cache entry for %s", key)


def clear() -> None:
    _entries.clear()
