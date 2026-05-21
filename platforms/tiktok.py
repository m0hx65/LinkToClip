from __future__ import annotations

from typing import Any


def ytdlp_overrides() -> dict[str, Any]:
    return {
        # Prefer MP4+M4A (both AAC) so the merge requires no audio transcoding.
        # Falling back to any bv+ba avoids failures when those formats aren't served.
        "format": "bv*[ext=mp4]+ba[ext=m4a]/bv*[ext=mp4]+ba/bv*+ba/b",
        # When audio transcoding is unavoidable (e.g. Opus→AAC), use 192k to avoid degradation.
        "postprocessor_args": {
            "ffmpeg": ["-c:a", "aac", "-b:a", "192k"],
        },
        "extractor_args": {
            "tiktok": {},
        },
    }
