from __future__ import annotations

from typing import Any


def ytdlp_overrides() -> dict[str, Any]:
    return {
        # Prefer iOS-friendly MP4/H.264 streams first.
        "format": "bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4]/b",
        "extractor_args": {
            "instagram": {
                # Prefer embedded/higher quality when available
            }
        },
    }
