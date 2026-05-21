from __future__ import annotations

from typing import Any


def ytdlp_overrides() -> dict[str, Any]:
    # Some tweets expose videos as multi-entry results (playlist-like metadata).
    # Keep playlist mode enabled so multi-video tweets can return all entries.
    return {
        "noplaylist": False,
        # Twitter serves H.264 MP4 natively; [vcodec^=avc1] guards against edge-cases
        # where a non-H.264 format might appear (iOS requires H.264 in MP4).
        "format": "bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/bv*[ext=mp4][vcodec^=avc1]+ba/bv*[ext=mp4]+ba/bv*+ba/b",
    }
