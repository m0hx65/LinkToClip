from __future__ import annotations

from typing import Any


def ytdlp_overrides() -> dict[str, Any]:
    return {
        # Pick the highest-resolution video stream regardless of codec (VP9/AV1/H.264),
        # paired with AAC audio. yt-dlp remuxes compatible streams into MP4 via stream-
        # copy; VP9/AV1 are embedded in the MP4 container without video re-encoding.
        "format": "bv*+ba[ext=m4a]/bv*+ba/best",
        # When audio transcoding is unavoidable (e.g. Opus→AAC fallback), use 192k.
        # YouTube's default Opus audio is 160k; transcoding at lower bitrate degrades it.
        "postprocessor_args": {
            "ffmpeg": ["-c:a", "aac", "-b:a", "192k"],
        },
        # ios/android clients bypass YouTube's bot-detection that blocks datacenter IPs.
        "extractor_args": {"youtube": {"player_client": ["ios", "android", "web"]}},
    }
