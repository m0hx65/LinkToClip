from __future__ import annotations


def normalize_http_url(url: str) -> str:
    u = url.strip()
    if u.startswith("www."):
        return "https://" + u
    return u
