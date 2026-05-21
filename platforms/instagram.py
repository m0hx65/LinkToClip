from __future__ import annotations

from typing import Any


def ytdlp_overrides() -> dict[str, Any]:
    return {
        # H.264 (avc1) in MP4 is the only codec iOS plays reliably.
        # Fall back to any MP4 so photo carousels / image formats still match.
        "format": "bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4]/b/best",
        # Required to download all images in carousel/multi-photo posts.
        "noplaylist": False,
        # Safety net: if audio transcoding is needed (fallback formats), ensure 192k AAC.
        "postprocessor_args": {
            "ffmpeg": ["-c:a", "aac", "-b:a", "192k"],
        },
    }
