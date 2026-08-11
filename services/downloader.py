from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import yt_dlp

from platforms import Platform, detect_platform
from platforms import instagram as ig_mod
from platforms import tiktok as tt_mod
from platforms import twitter as tw_mod
from platforms import youtube as yt_mod
from services import ig_stories
from utils.config import Settings
from utils.urltools import normalize_http_url

logger = logging.getLogger(__name__)
_IG_STORY_RE = re.compile(r"instagram\.com/stories/", re.I)
# Matches a bare profile URL — instagram.com/username or instagram.com/username?igsh=...
# Used to give a clear "not downloadable" error before wasting a download attempt.
_IG_PROFILE_RE = re.compile(r"instagram\.com/([^/?#]+)/?(?:\?|#|$)", re.I)
_IG_CONTENT_PATH_RE = re.compile(r"instagram\.com/(?:p|reel|tv|stories|reels|share)/", re.I)
_TW_STATUS_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:x\.com|twitter\.com)/(?:i/(?:web/)?status|(?P<user>[^/?#]+)/status)/(?P<id>\d+)(?:[/?#].*)?$",
    re.I,
)

_IG_HELP_NO_COOKIES = (
    "Public reels often fail from cloud/datacenter IPs until Instagram sees a real browser session. "
    "Export a Netscape cookies.txt while logged in at instagram.com and set COOKIES_FILE "
    "(e.g. in Render → Environment)."
)
_IG_HELP_HAS_COOKIES = (
    " COOKIES_FILE is set — cookies may be expired or invalid; re-export from your browser."
)


class DownloadError(Exception):
    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


# API calls fail fast so the next fallback in the chain starts sooner; media
# transfers get a generous total budget but still a bounded connect time.
_API_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=10)
_MEDIA_TIMEOUT = aiohttp.ClientTimeout(total=300, connect=15)

_http_session: aiohttp.ClientSession | None = None


def _get_http_session() -> aiohttp.ClientSession:
    """One shared session for all fallback downloaders: reuses TCP/TLS
    connections and caches DNS instead of handshaking on every request."""
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=16, ttl_dns_cache=300),
            headers={"User-Agent": "Mozilla/5.0"},
        )
    return _http_session


async def close_http_session() -> None:
    global _http_session
    if _http_session is not None:
        await _http_session.close()
        _http_session = None


async def _fetch_to_file(
    url: str,
    out_path: Path,
    headers: dict[str, str] | None = None,
) -> Path | None:
    """Stream a media URL to disk; returns the final path or None on failure.

    Image responses are renamed to .jpg regardless of the guessed extension.
    """
    session = _get_http_session()
    try:
        async with session.get(url, headers=headers, timeout=_MEDIA_TIMEOUT) as resp:
            if resp.status != 200:
                logger.info("media fetch HTTP %s for %s", resp.status, url[:120])
                return None
            if "image" in resp.headers.get("Content-Type", ""):
                out_path = out_path.with_suffix(".jpg")
            with open(out_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    f.write(chunk)
        if out_path.is_file() and out_path.stat().st_size > 0:
            return out_path
    except Exception as e:
        logger.info("media fetch error for %s: %s", url[:120], e)
    return None


# Partial files from failed or interrupted downloads are never referenced
# again, so sweep anything older than this to keep the disk from filling
# on long-running instances. Runs at most once per _SWEEP_INTERVAL_S.
_STALE_AFTER_S = 2 * 3600
_SWEEP_INTERVAL_S = 1800
_last_sweep = 0.0


def _sweep_stale_files(out_dir: Path) -> None:
    global _last_sweep
    now = time.time()
    if now - _last_sweep < _SWEEP_INTERVAL_S:
        return
    _last_sweep = now
    cutoff = now - _STALE_AFTER_S
    try:
        for p in out_dir.iterdir():
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    logger.info("Swept stale temp file %s", p.name)
            except OSError:
                pass
    except OSError:
        pass


class _YtdlpLogger:
    """Route yt-dlp console output into our logger (avoids raw ERROR: lines on stderr)."""

    def debug(self, msg: str) -> None:
        logger.debug("yt-dlp: %s", msg.rstrip())

    def info(self, msg: str) -> None:
        logger.debug("yt-dlp: %s", msg.rstrip())

    def warning(self, msg: str) -> None:
        logger.info("yt-dlp: %s", msg.rstrip())

    def error(self, msg: str) -> None:
        logger.info("yt-dlp: %s", msg.rstrip())


def _cookiefile_for_platform(settings: Settings, platform: Platform) -> str | None:
    if platform is Platform.TIKTOK:
        if settings.tiktok_cookies_file and settings.tiktok_cookies_file.is_file():
            return str(settings.tiktok_cookies_file)
        if settings.cookies_file and settings.cookies_file.is_file():
            return str(settings.cookies_file)
        return None
    if platform is Platform.TWITTER:
        if settings.twitter_cookies_file and settings.twitter_cookies_file.is_file():
            return str(settings.twitter_cookies_file)
        if settings.cookies_file and settings.cookies_file.is_file():
            return str(settings.cookies_file)
        return None
    if platform is Platform.YOUTUBE:
        if settings.youtube_cookies_file and settings.youtube_cookies_file.is_file():
            return str(settings.youtube_cookies_file)
        if settings.cookies_file and settings.cookies_file.is_file():
            return str(settings.cookies_file)
        return None
    if settings.cookies_file and settings.cookies_file.is_file():
        return str(settings.cookies_file)
    return None


def _map_download_failure(platform: Platform, err: Exception, settings: Settings, url: str = "") -> None:
    """Raise DownloadError with a user-facing message; logs full yt-dlp output."""
    raw = str(err)
    msg = raw.lower()
    logger.info("yt-dlp error: %s", raw[:1200])

    if any(
        x in msg
        for x in (
            "unavailable",
            "not available",
            "deleted",
            "removed",
            "does not exist",
        )
    ):
        raise DownloadError("This video is unavailable or the link is invalid.", retryable=False) from err

    if platform is Platform.INSTAGRAM:
        if "unsupported url" in msg:
            raise DownloadError(f"Unsupported URL: {raw[:280]}", retryable=False) from err
        has = bool(settings.cookies_file and settings.cookies_file.is_file())
        if _IG_STORY_RE.search(url) and "/highlights/" not in url.lower():
            raise DownloadError(
                "Could not download this story. The anonymous downloader (saveinsta.to) returned "
                "nothing and yt-dlp also failed. Stories from private accounts always need cookies; "
                "for public accounts saveinsta.to may be temporarily down. "
                + (_IG_HELP_HAS_COOKIES if has else _IG_HELP_NO_COOKIES),
                retryable=False,
            ) from err
        raise DownloadError(
            "Could not download this post. Both the anonymous downloader (saveinsta.to) "
            "and the direct fetch failed — the account may be private, or the services "
            "are temporarily rate-limited. This usually resolves on retry. "
            + (_IG_HELP_HAS_COOKIES if has else _IG_HELP_NO_COOKIES),
            retryable=True,
        ) from err

    if platform is Platform.TWITTER:
        _tw_has_cookies = bool(
            (settings.twitter_cookies_file and settings.twitter_cookies_file.is_file())
            or (settings.cookies_file and settings.cookies_file.is_file())
        )
        _tw_cookie_hint = (
            " TWITTER_COOKIES_FILE is set — cookies may be expired; re-export from your browser."
            if _tw_has_cookies
            else " X now requires authentication for most content. Export a Netscape cookies.txt "
            "while logged in at x.com and set TWITTER_COOKIES_FILE (or COOKIES_FILE)."
        )
        if "no video could be found in this tweet" in msg:
            raise DownloadError(
                "Could not get a video from this X link. The post may have no video, or "
                "X blocked automated access." + _tw_cookie_hint,
                retryable=False,
            ) from err
        if any(x in msg for x in ("401", "403", "unauthorized", "login", "cookies", "authenticate")):
            raise DownloadError("X rejected the request (auth error)." + _tw_cookie_hint, retryable=False) from err

    if platform is Platform.YOUTUBE:
        _yt_has_cookies = bool(
            (settings.youtube_cookies_file and settings.youtube_cookies_file.is_file())
            or (settings.cookies_file and settings.cookies_file.is_file())
        )
        _yt_cookie_hint = (
            " YOUTUBE_COOKIES_FILE is set — cookies may be expired; re-export from your browser."
            if _yt_has_cookies
            else (
                " YouTube blocks automated downloads from cloud servers without authentication. "
                "Export a Netscape cookies.txt while logged in at youtube.com and set "
                "YOUTUBE_COOKIES_FILE in your environment."
            )
        )
        if any(x in msg for x in ("members only", "premium", "join")):
            raise DownloadError(
                "This YouTube video is members-only or requires a premium subscription.",
                retryable=False,
            ) from err
        if any(x in msg for x in ("private",)):
            raise DownloadError("This YouTube video is private.", retryable=False) from err
        raise DownloadError(
            "YouTube download failed." + _yt_cookie_hint,
            retryable=False,
        ) from err

    if platform is Platform.TIKTOK:
        if "private" in msg or "login" in msg or "cookies" in msg:
            raise DownloadError(
                "TikTok blocked the download from this server's IP. "
                "This is common on cloud/datacenter hosts. "
                "Export a Netscape cookies.txt while logged in at tiktok.com and set "
                "TIKTOK_COOKIES_FILE (or COOKIES_FILE) in your environment to bypass this.",
                retryable=False,
            ) from err

    if "private" in msg or "login" in msg or "cookies" in msg:
        raise DownloadError(
            "This content is private or requires login.",
            retryable=False,
        ) from err

    raise DownloadError(f"Download failed: {raw[:500]}", retryable=True) from err


@dataclass
class DownloadResult:
    path: Path | None
    paths: list[Path]
    title: str | None
    direct_urls: list[str]
    platform: Platform
    # Maps a downloaded file to the public URL it came from, when that URL is
    # one Telegram's own servers can fetch. The handler offers it to Telegram
    # instead of uploading the bytes, which costs the host no outbound
    # bandwidth at all (Render and friends bill egress only). Empty for yt-dlp
    # results, whose formats are fragmented/expiring and not URL-sendable.
    source_urls: dict[Path, str] = field(default_factory=dict)


def _merge_dict(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge_dict(out[k], v)
        else:
            out[k] = v
    return out


def _base_opts(out_dir: Path, out_stem: str, settings: Settings, platform: Platform) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "outtmpl": str(out_dir / f"{out_stem}.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "noprogress": True,
        "quiet": True,
        "no_warnings": False,
        "retries": 3,
        "fragment_retries": 3,
        # 120s meant a single hung connection stalled the whole download for
        # two minutes before retrying; 30s recovers much faster.
        "socket_timeout": 30,
        # HLS/DASH content (Instagram, YouTube) downloads fragments in
        # parallel instead of one at a time — the single biggest speedup.
        "concurrent_fragment_downloads": 4,
        "http_chunk_size": 10 * 1024 * 1024,
    }
    cf = _cookiefile_for_platform(settings, platform)
    if cf:
        opts["cookiefile"] = cf
    return opts


def _platform_opts(platform: Platform) -> dict[str, Any]:
    if platform is Platform.INSTAGRAM:
        return ig_mod.ytdlp_overrides()
    if platform is Platform.TIKTOK:
        return tt_mod.ytdlp_overrides()
    if platform is Platform.TWITTER:
        return tw_mod.ytdlp_overrides()
    if platform is Platform.YOUTUBE:
        return yt_mod.ytdlp_overrides()
    return {}


def _build_ydl_opts(
    url: str,
    out_dir: Path,
    out_stem: str,
    settings: Settings,
) -> tuple[dict[str, Any], Platform]:
    platform = detect_platform(url)
    merged = _base_opts(out_dir, out_stem, settings, platform)
    merged = _merge_dict(merged, _platform_opts(platform))
    if platform in (Platform.TWITTER, Platform.INSTAGRAM):
        # Multi-entry results (multi-video tweets, photo carousels) need autonumber to prevent
        # filename collisions when multiple files are downloaded into the same directory.
        merged["outtmpl"] = str(out_dir / f"{out_stem}_%(autonumber)s.%(ext)s")
    return merged, platform


def _twitter_candidate_urls(url: str) -> list[str]:
    """Generate equivalent tweet URLs for extractor edge-cases."""
    u = url.strip()
    m = _TW_STATUS_RE.match(u)
    if not m:
        return [url]
    tweet_id = m.group("id")
    candidates = [
        u,
        f"https://x.com/i/web/status/{tweet_id}",
        f"https://x.com/i/status/{tweet_id}",
        f"https://twitter.com/i/web/status/{tweet_id}",
        f"https://twitter.com/i/status/{tweet_id}",
    ]
    # Preserve order while deduplicating.
    return list(dict.fromkeys(candidates))


def _twitter_ydl_opts_variants(ydl_opts: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Syndication first (works without auth), GraphQL second (works with cookies/auth)."""
    synd = _merge_dict(
        ydl_opts,
        {"extractor_args": {"twitter": {"api": ["syndication"]}}},
    )
    return [("syndication", synd), ("graphql", ydl_opts)]


def _extract_direct_urls(url: str, settings: Settings) -> list[str]:
    opts: dict[str, Any] = {
        "quiet": True,
        "skip_download": True,
        "forcejson": True,
        "noplaylist": True,
    }
    platform = detect_platform(url)
    cf = _cookiefile_for_platform(settings, platform)
    if cf:
        opts["cookiefile"] = cf
    urls: list[str] = []
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return []
            if "url" in info and info["url"]:
                urls.append(str(info["url"]))
            for entry in info.get("entries") or []:
                eu = entry.get("url")
                if eu and eu not in urls:
                    urls.append(str(eu))
                for f in entry.get("formats") or []:
                    u = f.get("url")
                    if u and u not in urls:
                        urls.append(str(u))
            for f in info.get("formats") or []:
                u = f.get("url")
                if u and u not in urls:
                    urls.append(str(u))
    except Exception as e:
        logger.info("Could not list direct URLs: %s", e)
    return urls[:5]


def _download_sync(url: str, ydl_opts: dict[str, Any]) -> tuple[list[Path], str | None]:
    found_paths: list[Path] = []
    title: str | None = None

    def add_path(p: Path | None) -> None:
        if not p:
            return
        if p not in found_paths:
            found_paths.append(p)

    def hook(d: dict[str, Any]) -> None:
        if d.get("status") == "finished":
            fp = d.get("filename")
            if fp:
                add_path(Path(fp))

    opts = dict(ydl_opts)
    opts["progress_hooks"] = [hook]
    opts.setdefault("logger", _YtdlpLogger())
    opts.setdefault("noprogress", True)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info:
            title = info.get("title") or info.get("id")
            items = [info]
            if info.get("entries"):
                items = [e for e in info["entries"] if isinstance(e, dict)]

            for item in items:
                fn = ydl.prepare_filename(item)
                candidate = Path(fn)
                if candidate.is_file():
                    add_path(candidate)
                for part in item.get("requested_downloads") or []:
                    p = part.get("filepath")
                    if p:
                        add_path(Path(p))

    existing_paths = [p for p in found_paths if p.is_file()]
    return existing_paths, title


async def _fxtwitter_fallback(
    url: str, out_dir: Path, out_stem: str
) -> tuple[list[Path], str | None, dict[Path, str]]:
    """Download all tweet media (videos, photos, GIFs) via the fxtwitter API —
    works from datacenter IPs without auth. Mixed-media tweets are supported."""
    m = _TW_STATUS_RE.match(url.strip())
    if not m:
        return [], None, {}
    tweet_id = m.group("id")
    user = m.group("user") or "i"
    api_url = f"https://api.fxtwitter.com/{user}/status/{tweet_id}"
    try:
        session = _get_http_session()
        async with session.get(api_url, timeout=_API_TIMEOUT) as resp:
            if resp.status != 200:
                logger.info("fxtwitter API %s for tweet %s", resp.status, tweet_id)
                return [], None, {}
            data = await resp.json()
    except Exception as e:
        logger.info("fxtwitter fallback error: %s", e)
        return [], None, {}
    tw = data.get("tweet") or {}
    title: str | None = tw.get("text") or None
    media = tw.get("media") or {}
    # `all` preserves the tweet's media order for mixed photo/video posts;
    # fall back to the separate lists if it's missing.
    items = media.get("all")
    if not isinstance(items, list) or not items:
        items = [*(media.get("videos") or []), *(media.get("photos") or [])]
    tasks = []
    media_urls: list[str] = []
    for idx, item in enumerate(items, 1):
        if not (isinstance(item, dict) and item.get("url")):
            continue
        # Photos are .jpg; videos and GIFs (which fxtwitter serves as mp4) are .mp4.
        ext = "jpg" if item.get("type") == "photo" else "mp4"
        media_url = str(item["url"])
        media_urls.append(media_url)
        tasks.append(_fetch_to_file(media_url, out_dir / f"{out_stem}_{idx}.{ext}"))
    results = await asyncio.gather(*tasks)
    # video.twimg.com / pbs.twimg.com are public, unauthenticated CDNs, so
    # Telegram can pull these itself.
    sources = {p: u for p, u in zip(results, media_urls, strict=True) if p}
    return [p for p in results if p], title, sources


def _tikwm_abs(u: str) -> str:
    """tikwm returns proxy paths relative to its own host (e.g. /video/media/…)."""
    return "https://www.tikwm.com" + u if u.startswith("/") else u


async def _tikwm_download(
    url: str, out_dir: Path, out_stem: str
) -> tuple[list[Path], str | None, dict[Path, str]]:
    """Download TikTok via tikwm.com API — no auth, no cookies, works from datacenter IPs."""
    try:
        session = _get_http_session()
        async with session.post(
            "https://www.tikwm.com/api/",
            data={"url": url, "count": 12, "cursor": 0, "web": 1, "hd": 1},
            timeout=_API_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                logger.info("tikwm HTTP %s", resp.status)
                return [], None, {}
            data = await resp.json(content_type=None)
    except Exception as e:
        logger.info("tikwm error: %s", e)
        return [], None, {}
    if data.get("code") != 0:
        logger.info("tikwm error code=%s msg=%s", data.get("code"), data.get("msg"))
        return [], None, {}
    item = data.get("data") or {}
    title: str | None = item.get("title") or None
    headers = {"Referer": "https://www.tikwm.com/"}

    # Photo-mode posts carry the slides in `images`; their play/hdplay URLs
    # are only the soundtrack MP3, so download the images instead.
    images = [i for i in (item.get("images") or []) if isinstance(i, str)]
    if images:
        abs_images = [_tikwm_abs(img) for img in images]
        results = await asyncio.gather(
            *(
                _fetch_to_file(img, out_dir / f"{out_stem}_{idx}.jpg", headers=headers)
                for idx, img in enumerate(abs_images, 1)
            )
        )
        paths = [p for p in results if p]
        sources = {p: u for p, u in zip(results, abs_images, strict=True) if p}
        if paths:
            logger.info("tikwm ok photos=%d title=%r", len(paths), title)
        return paths, title if paths else None, sources

    # Prefer HD play URL, fall back to standard play URL
    video_url: str | None = item.get("hdplay") or item.get("play") or None
    if not video_url:
        logger.info("tikwm no video URL in response")
        return [], None, {}
    abs_video_url = _tikwm_abs(video_url)
    out_path = await _fetch_to_file(
        abs_video_url,
        out_dir / f"{out_stem}_1.mp4",
        headers=headers,
    )
    if out_path:
        logger.info("tikwm ok title=%r", title)
        # tikwm's proxy 302s to the TikTok CDN and needs no Referer, so
        # Telegram can fetch this URL itself.
        return [out_path], title, {out_path: abs_video_url}
    return [], None, {}


def _is_ig_profile_url(url: str) -> bool:
    """Return True if url is a bare Instagram profile link with no downloadable content path."""
    return bool(_IG_PROFILE_RE.search(url) and not _IG_CONTENT_PATH_RE.search(url))


async def download_media(url: str, settings: Settings) -> DownloadResult:
    url = normalize_http_url(url)
    platform = detect_platform(url)
    if platform is Platform.INSTAGRAM:
        # App share links (instagram.com/s/<base64>) decode to highlight URLs.
        url = ig_stories.normalize_story_share_url(url)
        # instagram.com/share/... links are opaque server-side redirects;
        # resolve them to the real post/reel URL so the chain below can run.
        url = await ig_stories.resolve_share_url(url)
    if platform is Platform.UNKNOWN:
        raise DownloadError(
            "Unsupported URL. Send a link from Instagram, TikTok, X (Twitter), or YouTube.",
            retryable=False,
        )
    if platform is Platform.INSTAGRAM and _is_ig_profile_url(url):
        raise DownloadError(
            "That looks like an Instagram profile link — there's no media to download. "
            "Send a direct link to a post, reel, or story.",
            retryable=False,
        )

    out_dir = settings.temp_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    _sweep_stale_files(out_dir)
    out_stem = f"{uuid.uuid4().hex}"

    logger.info("Downloading url=%s platform=%s", url, platform.value)

    paths: list[Path] = []
    title: str | None = None

    # Instagram stories & highlights: saveinsta.to first — anonymous, no cookies
    # needed for public accounts (Instagram's own endpoints return login_required
    # for story media). Falls back to yt-dlp, which for stories needs cookies.
    if platform is Platform.INSTAGRAM and _IG_STORY_RE.search(url):
        paths, title, sources = await ig_stories.download_story_media(url, out_dir, out_stem)
        if paths:
            logger.info("saveinsta.to ok files=%d", len(paths))
            return DownloadResult(
                path=paths[0], paths=paths, title=title, direct_urls=[],
                platform=platform, source_urls=sources,
            )
        logger.info("saveinsta.to returned nothing for %s", url)
        # Without cookies, yt-dlp cannot download stories (login_required) —
        # skip the doomed attempt and give a clear error. Highlights sometimes
        # work anonymously via yt-dlp, so those fall through.
        is_story = "/highlights/" not in url.lower()
        has_cookies = bool(settings.cookies_file and settings.cookies_file.is_file())
        if is_story and not has_cookies:
            raise DownloadError(
                "Could not download this story. The anonymous downloader (saveinsta.to) "
                "returned nothing — the account may be private, the story may have expired, "
                "or the service is temporarily down. Stories from private accounts need "
                "cookies: export a Netscape cookies.txt while logged in at instagram.com "
                "and set COOKIES_FILE (e.g. in Render → Environment).",
                retryable=False,
            )
        logger.info("Falling back to yt-dlp for %s", url)

    # Instagram posts/reels/carousels: saveinsta.to first — anonymous, works
    # from datacenter IPs where Instagram blocks cookieless yt-dlp requests.
    # Falls back to yt-dlp, which succeeds with COOKIES_FILE or friendly IPs.
    if platform is Platform.INSTAGRAM and not _IG_STORY_RE.search(url):
        paths, title, sources = await ig_stories.download_post_media(url, out_dir, out_stem)
        if paths:
            logger.info("saveinsta.to post ok files=%d", len(paths))
            return DownloadResult(
                path=paths[0], paths=paths, title=title, direct_urls=[],
                platform=platform, source_urls=sources,
            )
        logger.info("saveinsta.to returned nothing for post %s; falling back to yt-dlp", url)

    # TikTok: tikwm.com first — no auth, no cookies, bypasses datacenter IP
    # blocking, and handles photo-mode posts. Falls back to yt-dlp (last
    # resort, needs cookies on cloud hosts). cobalt.tools was dropped from
    # the chain: its API now rejects all anonymous requests (JWT required).
    if platform is Platform.TIKTOK:
        paths, title, sources = await _tikwm_download(url, out_dir, out_stem)
        if paths:
            logger.info("tikwm TikTok ok files=%d", len(paths))
            return DownloadResult(
                path=paths[0], paths=paths, title=title, direct_urls=[],
                platform=platform, source_urls=sources,
            )
        logger.info("tikwm TikTok failed; falling back to yt-dlp")

    # Twitter: fxtwitter first — instant on cloud IPs, no auth needed, and it
    # returns every attachment (photos, videos, GIFs) of mixed-media tweets.
    # Fall through to yt-dlp only if fxtwitter has nothing (private/deleted tweet).
    if platform is Platform.TWITTER:
        paths, title, sources = await _fxtwitter_fallback(url, out_dir, out_stem)
        if paths:
            logger.info("fxtwitter ok files=%s", len(paths))
            return DownloadResult(
                path=paths[0], paths=paths, title=title, direct_urls=[],
                platform=platform, source_urls=sources,
            )
        logger.info("fxtwitter no media; falling back to yt-dlp")


    ydl_opts, _ = _build_ydl_opts(url, out_dir, out_stem, settings)
    candidate_urls = (
        _twitter_candidate_urls(url) if platform is Platform.TWITTER else [url]
    )
    opts_variants = (
        _twitter_ydl_opts_variants(ydl_opts) if platform is Platform.TWITTER else [("default", ydl_opts)]
    )
    # Twitter: fxtwitter already failed so skip retries — yt-dlp is a last resort.
    attempts = 1 if platform in (Platform.INSTAGRAM, Platform.TWITTER) else 2
    last_err: Exception | None = None
    for strategy_name, opt_variant in opts_variants:
        if platform is Platform.TWITTER:
            logger.info("Twitter yt-dlp strategy=%s", strategy_name)
        for candidate in candidate_urls:
            for attempt in range(attempts):
                try:
                    paths, title = await asyncio.to_thread(_download_sync, candidate, opt_variant)
                    break
                except yt_dlp.utils.DownloadError as e:
                    last_err = e
                    if attempt < attempts - 1:
                        logger.warning("Download retry after error: %s", e)
                        await asyncio.sleep(2)
                        continue
                    logger.info("Download failed for candidate url=%s err=%s", candidate, e)
                except Exception as e:
                    last_err = e
                    if attempt < attempts - 1:
                        logger.warning("Download retry after error: %s", e)
                        await asyncio.sleep(2)
                        continue
                    logger.info("Download failed for candidate url=%s err=%s", candidate, e)
            if paths:
                break
        if paths:
            break

    if not paths:
        if last_err is not None:
            _map_download_failure(platform, last_err, settings, url)
        return DownloadResult(path=None, paths=[], title=title, direct_urls=[], platform=platform)

    return DownloadResult(path=paths[0], paths=paths, title=title, direct_urls=[], platform=platform)


async def get_direct_urls(url: str, settings: Settings) -> list[str]:
    return await asyncio.to_thread(_extract_direct_urls, normalize_http_url(url), settings)
