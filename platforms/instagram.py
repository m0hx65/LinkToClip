from __future__ import annotations

from typing import Any


def ytdlp_overrides() -> dict[str, Any]:
    return {
        # Prefer the pre-muxed combined stream (b) rather than merging separate bv+ba.
        # Instagram encodes its combined streams with correct SAR/DAR and rotation metadata;
        # merging separate streams via ffmpeg can silently drop that metadata, causing the
        # video to render at the wrong aspect ratio on iOS and other players.
        # [vcodec^=avc1] keeps us on H.264 (required for iOS). Fallbacks handle carousels
        # and image posts where no H.264 combined stream exists.
        "format": "b[ext=mp4][vcodec^=avc1]/bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4]/b/best",
        # Required to download all images in carousel/multi-photo posts.
        "noplaylist": False,
        # Safety net for the bv*+ba fallback path: if a merge+transcode does happen, keep audio quality high.
        "postprocessor_args": {
            "ffmpeg": ["-c:a", "aac", "-b:a", "192k"],
        },
    }
