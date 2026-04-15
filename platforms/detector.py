from __future__ import annotations

import re
from enum import Enum


class Platform(str, Enum):
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    TWITTER = "twitter"
    YOUTUBE = "youtube"
    UNKNOWN = "unknown"


_IG = re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/", re.I)
_TT = re.compile(
    r"(?:https?://)?(?:www\.|vm\.|m\.)?tiktok\.com/|(?:https?://)?(?:www\.)?tiktok\.com/t/",
    re.I,
)
_TW = re.compile(
    r"(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/", re.I,
)
_YT = re.compile(
    r"(?:https?://)?(?:www\.)?(?:youtube\.com|youtu\.be)/", re.I,
)


def detect_platform(url: str) -> Platform:
    u = url.strip()
    if _IG.search(u):
        return Platform.INSTAGRAM
    if _TT.search(u):
        return Platform.TIKTOK
    if _TW.search(u):
        return Platform.TWITTER
    if _YT.search(u):
        return Platform.YOUTUBE
    return Platform.UNKNOWN
