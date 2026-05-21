from __future__ import annotations

from typing import Any


def ytdlp_overrides() -> dict[str, Any]:
    return {
        # [vcodec^=avc1] ensures H.264 video — required for iOS playback.
        # Audio: no [ext=m4a] restriction so yt-dlp picks the highest-quality audio
        # stream regardless of format (TikTok sometimes has better Opus than M4A).
        # The merge step always runs (separate bv+ba), so postprocessor_args always apply.
        "format": "bv*[ext=mp4][vcodec^=avc1]+ba/bv*[ext=mp4]+ba/b[ext=mp4]/b",
        # Always re-encode audio to stereo 192k AAC on merge:
        #   -ac 2      → force stereo (TikTok often serves mono tracks)
        #   -b:a 192k  → high bitrate to avoid lossy-to-lossy degradation
        "postprocessor_args": {
            "ffmpeg": ["-c:a", "aac", "-b:a", "192k", "-ac", "2"],
        },
        "extractor_args": {
            "tiktok": {},
        },
    }
