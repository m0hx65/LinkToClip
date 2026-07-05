"""Login-free Instagram media downloader (saveinsta.to).

Covers stories, highlights, posts, reels, and photo carousels. Same logic as
the_watcher_V3.0 stories client: 100% anonymous — no Instagram login, cookie,
or session. Instagram blocks its own endpoints for anonymous/datacenter
clients, so we drive saveinsta.to's public token flow the same way its
web UI does:

    1. GET  https://saveinsta.to/en/highlights        -> page carries k_exp / k_token
    2. POST https://saveinsta.to/api/userverify        -> issues a per-request cftoken
    3. POST https://saveinsta.to/api/ajaxSearch         -> returns media HTML for the URL

The HTML lists each item as a <li> with a dl.snapcdn.app download link (whose
JWT encodes the real scontent.cdninstagram.com URL). curl_cffi's Chrome TLS
impersonation is required — a plain Python TLS stack gets blocked.

Like any third-party source this can break or rate-limit; every method degrades
gracefully (returns [] / None) so the caller can fall back to yt-dlp.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from curl_cffi.requests import AsyncSession

logger = logging.getLogger(__name__)

_BASE = "https://saveinsta.to"
_TOKEN_PAGE = f"{_BASE}/en/highlights"
_VERIFY_URL = f"{_BASE}/api/userverify"
_SEARCH_URL = f"{_BASE}/api/ajaxSearch"
_CHROME = "chrome120"

# Page carries `k_exp = "..."` / `k_token = "..."` inline; ajaxSearch needs both.
_K_EXP_RE = re.compile(r'k_exp\s*=\s*"([^"]+)"')
_K_TOKEN_RE = re.compile(r'k_token\s*=\s*"([^"]+)"')
# Each media item is one <li>…</li>; the video/image icon class tells the type.
_LI_RE = re.compile(r"<li\b.*?</li>", re.S)
_ANCHOR_RE = re.compile(
    r'<a\b[^>]*href="(https://dl\.snapcdn\.app[^"]+)"[^>]*title="([^"]*)"', re.S
)
_FALLBACK_DL_RE = re.compile(r'href="(https://dl\.snapcdn\.app[^"]+)"')
# scontent filenames embed a stable numeric media id: /<mediaid>_<ownerid>_…
_MEDIA_ID_RE = re.compile(r"/(\d{6,})_\d{6,}_")

# instagram.com/stories/<username>/ or instagram.com/stories/<username>/<story_pk>/
_STORY_URL_RE = re.compile(
    r"instagram\.com/stories/(?!highlights(?:/|$))(?P<username>[^/?#]+)(?:/(?P<pk>\d+))?",
    re.I,
)
_HIGHLIGHT_URL_RE = re.compile(r"instagram\.com/stories/highlights/(?P<id>\d+)", re.I)
# App share links: instagram.com/s/<base64("highlight:<id>")>
_SHARE_URL_RE = re.compile(r"instagram\.com/s/(?P<blob>[A-Za-z0-9_-]+={0,2})", re.I)
# Posts, reels, and IGTV: instagram.com/{p|reel|reels|tv}/<shortcode>
_POST_URL_RE = re.compile(
    r"instagram\.com/(?P<kind>p|reel|reels|tv)/(?P<code>[A-Za-z0-9_-]+)", re.I
)
# Opaque app share links (instagram.com/share/...) — server-side redirects
# to the real post/reel URL; must be resolved before download.
_SHARE_REDIRECT_RE = re.compile(r"instagram\.com/share/", re.I)


@dataclass
class StoryItem:
    pk: str
    media_type: str  # "image" or "video"
    url: str  # dl.snapcdn.app download link


def normalize_story_share_url(url: str) -> str:
    """Decode instagram.com/s/<base64> app-share links to canonical highlight URLs.

    The blob is base64url of "highlight:<numeric id>". Non-highlight or
    undecodable blobs are returned unchanged.
    """
    m = _SHARE_URL_RE.search(url)
    if not m:
        return url
    blob = m.group("blob").rstrip("=")
    padded = blob + "=" * (-len(blob) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return url
    if decoded.startswith("highlight:"):
        hid = decoded.split(":", 1)[1]
        if hid.isdigit():
            return f"https://www.instagram.com/stories/highlights/{hid}/"
    return url


def canonical_post_url(url: str) -> str | None:
    """Canonical post/reel URL with tracking params (igsh, utm_*) stripped.

    Returns None when the URL doesn't point at a post, reel, or IGTV video.
    """
    m = _POST_URL_RE.search(url)
    if not m:
        return None
    kind = m.group("kind").lower()
    if kind == "reels":
        kind = "reel"
    return f"https://www.instagram.com/{kind}/{m.group('code')}/"


class StoriesClient:
    """Async client wrapping the saveinsta.to anonymous downloader."""

    def __init__(self) -> None:
        self._session = AsyncSession(
            impersonate=_CHROME,
            timeout=30,
            allow_redirects=True,
        )
        # Cached (k_exp, k_token) from the token page + the monotonic time they
        # stop being reused. Lets repeat fetches skip one of three round-trips.
        self._tokens: tuple[str, str] | None = None
        self._tokens_until: float = 0.0

    async def close(self) -> None:
        await self._session.close()

    # ---------------------------------------------------------------- public

    async def fetch_items(self, target_url: str) -> list[StoryItem]:
        """Return the media items saveinsta lists for an Instagram URL."""
        data = await self._fetch_media_html(target_url)
        return self._parse_items(data)

    async def download(self, item: StoryItem, dest: Path) -> Path | None:
        """Download a story item to dest. Returns the path on success."""
        try:
            resp = await self._session.get(item.url)
            if resp.status_code != 200 or not resp.content:
                logger.info(
                    "saveinsta media pk=%s HTTP %s", item.pk, resp.status_code
                )
                return None
            dest.write_bytes(resp.content)
            return dest
        except Exception as e:
            logger.info("saveinsta media pk=%s download error: %s", item.pk, e)
            return None

    async def resolve_redirect(self, url: str) -> str | None:
        """Follow redirects and return the final URL (None on failure).

        Uses the Chrome-impersonating session, which Instagram redirects
        normally even for clients it would block on content endpoints.
        """
        try:
            resp = await self._session.get(url)
            final = str(resp.url or "")
            return final or None
        except Exception as e:
            logger.info("redirect resolve failed for %s: %s", url, e)
            return None

    # --------------------------------------------------------------- internal

    async def _get_tokens(self) -> tuple[str, str] | None:
        """Return cached (k_exp, k_token), refreshing from the token page when
        the cache has expired. Cached for up to 5 minutes — saves one HTTP
        round-trip on every fetch after the first."""
        if self._tokens and time.monotonic() < self._tokens_until:
            return self._tokens
        page = await self._session.get(_TOKEN_PAGE)
        if page.status_code != 200:
            logger.info("saveinsta token page HTTP %s", page.status_code)
            return None
        ke = _K_EXP_RE.search(page.text)
        kt = _K_TOKEN_RE.search(page.text)
        if not ke or not kt:
            logger.info("saveinsta token block not found")
            return None
        self._tokens = (ke.group(1), kt.group(1))
        self._tokens_until = time.monotonic() + 300.0
        return self._tokens

    async def _fetch_media_html(self, target_url: str) -> str:
        """Run the saveinsta token flow for an Instagram URL; return media HTML.

        Returns "" on any failure so callers degrade to an empty result set.
        """
        try:
            tokens = await self._get_tokens()
            if tokens is None:
                return ""
            k_exp, k_token = tokens

            verify = await self._session.post(
                _VERIFY_URL,
                data={"url": target_url},
                headers={
                    "Origin": _BASE,
                    "Referer": f"{_BASE}/en/video",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            cftoken = ""
            if verify.status_code == 200:
                try:
                    cftoken = verify.json().get("token", "") or ""
                except Exception:
                    cftoken = ""

            search = await self._session.post(
                _SEARCH_URL,
                data={
                    "k_exp": k_exp,
                    "k_token": k_token,
                    "q": target_url,
                    "t": "media",
                    "lang": "en",
                    "v": "v2",
                    "cftoken": cftoken,
                },
                headers={
                    "Origin": _BASE,
                    "Referer": _TOKEN_PAGE,
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            if search.status_code != 200:
                logger.info("saveinsta ajaxSearch HTTP %s", search.status_code)
                self._tokens = None  # tokens may be stale — force refresh next time
                return ""
            payload = search.json()
            if payload.get("status") != "ok":
                logger.info("saveinsta ajaxSearch status=%s", payload.get("status"))
                return ""
            return payload.get("data", "") or ""
        except Exception as exc:
            logger.info("saveinsta fetch failed for %s: %s", target_url, exc)
            return ""

    def _parse_items(self, data: str) -> list[StoryItem]:
        """Parse the media HTML into StoryItems, de-duplicated by media id."""
        items: list[StoryItem] = []
        seen: set[str] = set()
        if not data:
            return items
        for li in _LI_RE.findall(data):
            is_video = "icon-dlvideo" in li
            href = self._pick_download_href(li, is_video)
            if not href:
                continue
            href = href.replace("&amp;", "&")
            cdn_url = self._decode_jwt_url(href)
            pk = self._derive_pk(cdn_url, href)
            if pk in seen:
                continue
            seen.add(pk)
            items.append(
                StoryItem(
                    pk=pk,
                    media_type="video" if is_video else "image",
                    url=href,
                )
            )
        return items

    @staticmethod
    def _pick_download_href(li: str, is_video: bool) -> str | None:
        """Choose the right download link inside a <li>.

        Video items expose two links — "Download Thumbnail" (poster) and
        "Download Video" (the mp4); pick the video. Image items have a single
        download link. Falls back to the last dl.snapcdn link found.
        """
        anchors = _ANCHOR_RE.findall(li)
        if is_video:
            for url, title in anchors:
                if "video" in title.lower():
                    return url
        else:
            for url, title in anchors:
                if "thumbnail" not in title.lower():
                    return url
        all_links = _FALLBACK_DL_RE.findall(li)
        return all_links[-1] if all_links else None

    @staticmethod
    def _decode_jwt_url(href: str) -> str | None:
        """Decode the embedded JWT in a snapcdn link to the real cdn URL."""
        token = href.split("token=")[-1]
        for part in token.split("."):
            padded = part + "=" * (-len(part) % 4)
            try:
                decoded = json.loads(base64.urlsafe_b64decode(padded))
            except (binascii.Error, ValueError, json.JSONDecodeError):
                continue
            if isinstance(decoded, dict) and decoded.get("url"):
                return str(decoded["url"])
        return None

    @staticmethod
    def _derive_pk(cdn_url: str | None, href: str) -> str:
        """Stable per-item id for dedup: the numeric media id when present,
        otherwise a hash of the media path (query strings carry volatile signing
        params, so they're stripped first)."""
        base = cdn_url or href
        path = base.split("?", 1)[0]
        media_id = _MEDIA_ID_RE.search(path)
        if media_id:
            return media_id.group(1)
        return hashlib.sha1(path.encode("utf-8")).hexdigest()[:24]


# One long-lived client per process: keeps the TLS session and the 5-minute
# (k_exp, k_token) cache warm across requests, same as the watcher.
_client: StoriesClient | None = None


def get_client() -> StoriesClient:
    global _client
    if _client is None:
        _client = StoriesClient()
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


async def _download_items(
    client: StoriesClient, items: list[StoryItem], out_dir: Path, out_stem: str
) -> list[Path]:
    """Download items concurrently (highlights/carousels can hold dozens); the
    semaphore keeps us polite to the CDN and bounds memory. gather preserves
    order, so the _<idx> numbering still matches the source sequence."""
    sem = asyncio.Semaphore(4)

    async def _bounded_download(item: StoryItem, dest: Path) -> Path | None:
        async with sem:
            return await client.download(item, dest)

    tasks = []
    for idx, item in enumerate(items, 1):
        ext = ".mp4" if item.media_type == "video" else ".jpg"
        tasks.append(_bounded_download(item, out_dir / f"{out_stem}_{idx}{ext}"))
    results = await asyncio.gather(*tasks)
    return [p for p in results if p and p.is_file() and p.stat().st_size > 0]


async def download_story_media(
    url: str, out_dir: Path, out_stem: str
) -> tuple[list[Path], str | None]:
    """Download Instagram story/highlight media via saveinsta.to (login-free).

    Accepts stories/<username>/, stories/<username>/<pk>/, and
    stories/highlights/<id>/ URLs. When the URL points at one specific story
    and that item is found, only it is downloaded; otherwise all listed items
    are. Returns ([], None) on any failure so callers can fall back to yt-dlp.
    """
    m_h = _HIGHLIGHT_URL_RE.search(url)
    m_s = _STORY_URL_RE.search(url)
    want_pk: str | None = None
    if m_h:
        target = f"https://www.instagram.com/stories/highlights/{m_h.group('id')}/"
    elif m_s:
        target = f"https://www.instagram.com/stories/{m_s.group('username')}/"
        want_pk = m_s.group("pk")
    else:
        return [], None

    client = get_client()
    items = await client.fetch_items(target)
    if want_pk:
        exact = [it for it in items if it.pk == want_pk]
        if exact:
            items = exact

    paths = await _download_items(client, items, out_dir, out_stem)
    return paths, None


async def download_post_media(
    url: str, out_dir: Path, out_stem: str
) -> tuple[list[Path], str | None]:
    """Download an Instagram post/reel/carousel via saveinsta.to (login-free).

    Works for public content from datacenter IPs where Instagram blocks
    anonymous yt-dlp requests. Returns ([], None) on any failure so callers
    can fall back to yt-dlp.
    """
    target = canonical_post_url(url)
    if not target:
        return [], None
    client = get_client()
    items = await client.fetch_items(target)
    paths = await _download_items(client, items, out_dir, out_stem)
    return paths, None


async def resolve_share_url(url: str) -> str:
    """Resolve opaque instagram.com/share/... links to the real post URL.

    Share links are server-side redirects; anonymous clients sometimes land on
    the login page instead, which still carries the destination in ?next=.
    Returns the original URL on any failure so downloads can still be tried.
    """
    if not _SHARE_REDIRECT_RE.search(url):
        return url
    final = await get_client().resolve_redirect(url)
    if not final:
        return url
    if "/accounts/login" in final:
        nxt = (parse_qs(urlparse(final).query).get("next") or [""])[0]
        if nxt.startswith("/"):
            final = "https://www.instagram.com" + nxt
        elif nxt.startswith("http"):
            final = nxt
        else:
            return url
    if _SHARE_REDIRECT_RE.search(final):
        return url
    logger.info("Resolved IG share link to %s", final.split("?", 1)[0])
    return final
