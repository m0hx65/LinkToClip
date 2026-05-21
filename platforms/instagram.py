from __future__ import annotations

from typing import Any


def ytdlp_overrides() -> dict[str, Any]:
    return {
        # Video-first (iOS-friendly); fall back to `best` which also matches image formats
        # for photo posts and carousels.
        "format": "bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4]/b/best",
        # Required to download all images in carousel/multi-photo posts.
        "noplaylist": False,
        # Safety net: if audio transcoding is needed (fallback formats), ensure 192k AAC.
        "postprocessor_args": {
            "ffmpeg": ["-c:a", "aac", "-b:a", "192k"],
        },
    }
