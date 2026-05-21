from __future__ import annotations

from typing import Any


def ytdlp_overrides() -> dict[str, Any]:
    return {
        # Prefer merged MP4 when possible for Telegram compatibility.
        "format": "bv*+ba/best",
        # ios/android clients bypass YouTube's bot-detection that blocks datacenter IPs.
        "extractor_args": {"youtube": {"player_client": ["ios", "android", "web"]}},
    }
