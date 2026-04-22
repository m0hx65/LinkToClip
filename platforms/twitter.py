from __future__ import annotations

from typing import Any


def ytdlp_overrides() -> dict[str, Any]:
    # Some tweets expose videos as multi-entry results (playlist-like metadata).
    # Keep playlist mode enabled so multi-video tweets can return all entries.
    return {
        "noplaylist": False,
    }
