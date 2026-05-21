from __future__ import annotations

from typing import Any


def ytdlp_overrides() -> dict[str, Any]:
    return {
        # Prefer H.264/MP4 + AAC/M4A so ffmpeg can stream-copy both tracks into MP4
        # with no re-encoding (no quality loss). Falls back to any best video+audio
        # when the preferred containers aren't available.
        "format": "bv*[ext=mp4]+ba[ext=m4a]/bv*[ext=mp4]+ba/bv*+ba/best",
        # When audio transcoding is unavoidable (e.g. Opus→AAC fallback), use 192k.
        # YouTube's default Opus audio is 160k; transcoding at lower bitrate degrades it.
        "postprocessor_args": {
            "ffmpeg": ["-c:a", "aac", "-b:a", "192k"],
        },
        # ios/android clients bypass YouTube's bot-detection that blocks datacenter IPs.
        "extractor_args": {"youtube": {"player_client": ["ios", "android", "web"]}},
    }
