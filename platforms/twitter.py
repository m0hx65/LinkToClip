from __future__ import annotations

from typing import Any


def ytdlp_overrides() -> dict[str, Any]:
    # Some tweets expose videos as multi-entry results (playlist-like metadata).
    # Keep playlist mode enabled so multi-video tweets can return all entries.
    return {
        "noplaylist": False,
        # Prefer MP4+M4A (AAC) to avoid transcoding. Twitter natively serves MP4/AAC
        # so this almost always hits the first option.
        "format": "bv*[ext=mp4]+ba[ext=m4a]/bv*[ext=mp4]+ba/bv*+ba/b",
        "postprocessor_args": {
            "ffmpeg": ["-c:a", "aac", "-b:a", "192k"],
        },
    }
