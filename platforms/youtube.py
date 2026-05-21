from __future__ import annotations

from typing import Any


def ytdlp_overrides() -> dict[str, Any]:
    return {
        # iOS only plays H.264 (avc1) reliably — VP9 and AV1 silently fail.
        # bv*[ext=mp4][vcodec^=avc1] picks the highest-resolution H.264 YouTube serves
        # (can be 1080p, 1440p, or 4K depending on the video). Stream-copied into MP4,
        # no re-encoding. Falls back through progressively broader options.
        "format": (
            "bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]"  # H.264 + AAC  (ideal, stream-copy both)
            "/bv*[ext=mp4][vcodec^=avc1]+ba"           # H.264 + any audio (transcode audio if needed)
            "/bv*[ext=mp4]+ba[ext=m4a]"                # any MP4 video + AAC (H.265 fallback)
            "/bv*+ba/best"                             # last resort
        ),
        # ios/android clients bypass YouTube's bot-detection that blocks datacenter IPs.
        "extractor_args": {"youtube": {"player_client": ["ios", "android", "web"]}},
    }
