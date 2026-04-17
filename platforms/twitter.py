from __future__ import annotations

from typing import Any


def ytdlp_overrides() -> dict[str, Any]:
    # Some tweets expose videos as multi-entry results (playlist-like metadata).
    # Allow entry extraction and download the first item to avoid hard failures.
    return {
        "noplaylist": False,
        "playlist_items": "1",
    }
