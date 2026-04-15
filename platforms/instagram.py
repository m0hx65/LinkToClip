from __future__ import annotations

from typing import Any


def ytdlp_overrides() -> dict[str, Any]:
    return {
        "extractor_args": {
            "instagram": {
                # Prefer embedded/higher quality when available
            }
        },
    }
