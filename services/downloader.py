from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import aiohttp
import yt_dlp

from platforms import Platform, detect_platform
from platforms import instagram as ig_mod
from platforms import tiktok as tt_mod
from platforms import twitter as tw_mod
from platforms import youtube as yt_mod
from utils.config import Settings
from utils.urltools import normalize_http_url

logger = logging.getLogger(__name__)
_IG_STORY_RE = re.compile(r"instagram\.com/stories/", re.I)
# Matches a bare profile URL — instagram.com/username or instagram.com/username?igsh=...
# Used to give a clear "not downloadable" error before wasting a download attempt.
_IG_PROFILE_RE = re.compile(r"instagram\.com/([^/?#]+)/?(?:\?|#|$)", re.I)
_IG_CONTENT_PATH_RE = re.compile(r"instagram\.com/(?:p|reel|tv|stories|reels)/", re.I)
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
                "Could not download this story. The third-party fallback (saveig.app) returned "
                "nothing and yt-dlp also failed. Stories from private accounts always need cookies; "
                "for public accounts saveig.app may be temporarily down. "
                + (_IG_HELP_HAS_COOKIES if has else _IG_HELP_NO_COOKIES),
                retryable=False,
            ) from err
        raise DownloadError(
            "Instagram did not return this content to the server. "
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
        "socket_timeout": 120,
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


async def _fxtwitter_fallback(url: str, out_dir: Path, out_stem: str) -> tuple[list[Path], str | None]:
    """Download via fxtwitter API — works from datacenter IPs without auth."""
    m = _TW_STATUS_RE.match(url.strip())
    if not m:
        return [], None
    tweet_id = m.group("id")
    user = m.group("user") or "i"
    api_url = f"https://api.fxtwitter.com/{user}/status/{tweet_id}"
    paths: list[Path] = []
    title: str | None = None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.info("fxtwitter API %s for tweet %s", resp.status, tweet_id)
                    return [], None
                data = await resp.json()
            tw = data.get("tweet") or {}
            title = tw.get("text") or None
            videos = (tw.get("media") or {}).get("videos") or []
            for idx, video in enumerate(videos, 1):
                video_url = video.get("url") if isinstance(video, dict) else None
                if not video_url:
                    continue
                out_path = out_dir / f"{out_stem}_{idx}.mp4"
                try:
                    async with session.get(video_url, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                        if resp.status != 200:
                            logger.info("fxtwitter video %s returned %s", idx, resp.status)
                            continue
                        with open(out_path, "wb") as f:
                            async for chunk in resp.content.iter_chunked(8 * 1024 * 1024):
                                f.write(chunk)
                    if out_path.is_file() and out_path.stat().st_size > 0:
                        paths.append(out_path)
                except Exception as e:
                    logger.info("fxtwitter video %s download error: %s", idx, e)
    except Exception as e:
        logger.info("fxtwitter fallback error: %s", e)
    return paths, title


def _clean_ig_url(url: str) -> str:
    """Strip Instagram share-tracking params (igsh, utm_*) that confuse third-party APIs."""
    p = urlparse(url)
    path = p.path.rstrip("/") + "/"
    return urlunparse((p.scheme or "https", p.netloc, path, "", "", ""))


def _is_ig_profile_url(url: str) -> bool:
    """Return True if url is a bare Instagram profile link with no downloadable content path."""
    return bool(_IG_PROFILE_RE.search(url) and not _IG_CONTENT_PATH_RE.search(url))


async def _cobalt_download(url: str, out_dir: Path, out_stem: str) -> tuple[list[Path], str | None]:
    """Download via cobalt.tools API (v11+). Works from datacenter IPs for YouTube, TikTok, Instagram, Twitter."""
    media_urls: list[str] = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.cobalt.tools/",
                json={"url": url, "videoQuality": "max"},
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    logger.info("cobalt.tools HTTP %s for %s", resp.status, url)
                    return [], None
                data = await resp.json()
        status = data.get("status")
        if status in ("tunnel", "redirect") and data.get("url"):
            media_urls.append(data["url"])
        elif status == "picker":
            for item in data.get("picker") or []:
                if item.get("url"):
                    media_urls.append(item["url"])
        logger.info("cobalt.tools status=%s found=%d URL(s)", status, len(media_urls))
    except Exception as e:
        logger.info("cobalt.tools error: %s", e)
        return [], None

    if not media_urls:
        return [], None

    paths: list[Path] = []
    async with aiohttp.ClientSession() as session:
        for idx, media_url in enumerate(media_urls, 1):
            ext = "mp4" if ".mp4" in media_url.lower() else "jpg"
            out_path = out_dir / f"{out_stem}_{idx}.{ext}"
            try:
                async with session.get(
                    media_url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=aiohttp.ClientTimeout(total=300),
                ) as resp:
                    if resp.status != 200:
                        logger.info("cobalt media %s HTTP %s", idx, resp.status)
                        continue
                    ct = resp.headers.get("Content-Type", "")
                    if "image" in ct:
                        out_path = out_path.with_suffix(".jpg")
                    with open(out_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(8 * 1024 * 1024):
                            f.write(chunk)
                if out_path.is_file() and out_path.stat().st_size > 0:
                    paths.append(out_path)
            except Exception as e:
                logger.info("cobalt media %s download error: %s", idx, e)
    return paths, None


async def _story_api_fallback(url: str, out_dir: Path, out_stem: str) -> tuple[list[Path], str | None]:
    """Download Instagram story via third-party APIs — no login needed for public accounts.

    Tries two services in order:
    1. saveig.app  — general Instagram downloader (posts/reels/stories)
    2. cobalt.tools — open-source media downloader with Instagram support

    Both use their own backend sessions so callers need no cookies.
    Falls through gracefully if a service is down or returns no media.
    """
    clean_url = _clean_ig_url(url)
    logger.info("Story fallback clean_url=%s", clean_url)
    media_urls: list[str] = []

    # --- Attempt 1: saveig.app ---
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://v3.saveig.app/api/ajaxSearch",
                data={"q": clean_url, "t": "media", "lang": "en"},
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "X-Requested-With": "XMLHttpRequest",
                    "Origin": "https://saveig.app",
                    "Referer": "https://saveig.app/en",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    html = data.get("data", "")
                    found = list(dict.fromkeys(re.findall(
                        r'https://[^\s"\'<>]+?\.(?:mp4|jpg|jpeg|png)(?:\?[^\s"\'<>]*)?',
                        html, re.I,
                    )))
                    media_urls.extend(found)
                    logger.info("saveig.app returned %d media URL(s)", len(found))
                else:
                    logger.info("saveig.app HTTP %s", resp.status)
    except Exception as e:
        logger.info("saveig.app error: %s", e)

    # --- Attempt 2: cobalt.tools (open-source, supports Instagram) ---
    if not media_urls:
        cobalt_paths, _ = await _cobalt_download(clean_url, out_dir, out_stem + "_s")
        return cobalt_paths, None

    if not media_urls:
        return [], None

    paths: list[Path] = []
    async with aiohttp.ClientSession() as session:
        for idx, media_url in enumerate(media_urls, 1):
            ext = "mp4" if ".mp4" in media_url.lower() else "jpg"
            out_path = out_dir / f"{out_stem}_s{idx}.{ext}"
            try:
                async with session.get(
                    media_url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status != 200:
                        logger.info("Story media %s HTTP %s", idx, resp.status)
                        continue
                    with open(out_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(8 * 1024 * 1024):
                            f.write(chunk)
                if out_path.is_file() and out_path.stat().st_size > 0:
                    paths.append(out_path)
            except Exception as e:
                logger.info("Story media %s download error: %s", idx, e)

    return paths, None


async def download_media(url: str, settings: Settings) -> DownloadResult:
    url = normalize_http_url(url)
    platform = detect_platform(url)
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
    out_stem = f"{uuid.uuid4().hex}"

    logger.info("Downloading url=%s platform=%s", url, platform.value)

    paths: list[Path] = []
    title: str | None = None

    # Instagram stories require a logged-in session — skip straight to a clear error.
    # Highlights (/stories/highlights/…) are different and work via yt-dlp without auth.
    if platform is Platform.INSTAGRAM and _IG_STORY_RE.search(url) and "/highlights/" not in url.lower():
        has_cookies = bool(settings.cookies_file and settings.cookies_file.is_file())
        if not has_cookies:
            raise DownloadError(
                "Instagram story downloading requires cookies. "
                "Export a Netscape cookies.txt while logged in at instagram.com and set "
                "COOKIES_FILE (e.g. in Render → Environment).",
                retryable=False,
            )

    # Twitter: fxtwitter first — instant on cloud IPs, no auth needed.
    # Fall through to yt-dlp only if fxtwitter has no video (private/deleted/no-media tweet).
    if platform is Platform.TWITTER:
        paths, title = await _fxtwitter_fallback(url, out_dir, out_stem)
        if paths:
            logger.info("fxtwitter ok files=%s", len(paths))
            return DownloadResult(path=paths[0], paths=paths, title=title, direct_urls=[], platform=platform)
        logger.info("fxtwitter no videos; falling back to yt-dlp")


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
