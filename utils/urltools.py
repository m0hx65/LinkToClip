from __future__ import annotations

import re


def normalize_http_url(url: str) -> str:
    u = url.strip()
    if u.startswith("www."):
        return "https://" + u
    return u


# Identity of the *content* behind a link, ignoring how it was shared. The app
# appends per-share tracking params (?igsh=…, ?t=…), so the same reel pasted by
# two people is two different URLs pointing at one video — keying a cache on the
# raw URL would miss every one of those.
_KEY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ig", re.compile(r"instagram\.com/(?:p|reel|reels|tv)/(?P<id>[A-Za-z0-9_-]+)", re.I)),
    ("ig-story", re.compile(r"instagram\.com/stories/highlights/(?P<id>\d+)", re.I)),
    ("ig-story", re.compile(r"instagram\.com/stories/(?P<id>[^/?#]+/\d+)", re.I)),
    ("tt", re.compile(r"tiktok\.com/(?:@[^/]+/)?(?:video|photo)/(?P<id>\d+)", re.I)),
    ("tw", re.compile(r"(?:x|twitter)\.com/(?:i/(?:web/)?status|[^/?#]+/status)/(?P<id>\d+)", re.I)),
    ("yt", re.compile(r"(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)(?P<id>[A-Za-z0-9_-]{6,})", re.I)),
)


def canonical_media_key(url: str) -> str:
    """Stable identity for the media a link points at.

    Two links that resolve to the same post return the same key regardless of
    tracking parameters. Links whose target can only be known after a network
    round trip (instagram.com/share/…, vm.tiktok.com/…) fall back to the URL
    itself — a miss, not a wrong hit, which is the safe direction.
    """
    u = normalize_http_url(url)
    for prefix, pattern in _KEY_PATTERNS:
        m = pattern.search(u)
        if m:
            return f"{prefix}:{m.group('id').lower()}"
    return u.split("#", 1)[0].rstrip("/").lower()
