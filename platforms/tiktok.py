from __future__ import annotations

from typing import Any


def ytdlp_overrides() -> dict[str, Any]:
    return {
        # Prefer best merged video; many TikTok streams are watermark-free when available
        "format": "bv*+ba/bv+ba/bestvideo+bestaudio/b",
        "extractor_args": {
            "tiktok": {},
        },
    }
