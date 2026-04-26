from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    if platform is Platform.TWITTER:
        if settings.twitter_cookies_file and settings.twitter_cookies_file.is_file():
            return str(settings.twitter_cookies_file)
        # Fall back to general cookies file for Twitter too.
        if settings.cookies_file and settings.cookies_file.is_file():
            return str(settings.cookies_file)
        return None
    if settings.cookies_file and settings.cookies_file.is_file():
        return str(settings.cookies_file)
    return None


def _map_download_failure(platform: Platform, err: Exception, settings: Settings) -> None:
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
        raise DownloadError("This video is unavailable or the link is invalid.") from err

    if platform is Platform.INSTAGRAM:
        if "unsupported url" in msg:
            raise DownloadError(f"Unsupported URL: {raw[:280]}") from err
        has = bool(settings.cookies_file and settings.cookies_file.is_file())
        raise DownloadError(
            "Instagram did not return this video to the server. "
            + (_IG_HELP_HAS_COOKIES if has else _IG_HELP_NO_COOKIES)
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
                "X blocked automated access." + _tw_cookie_hint
            ) from err
        if any(x in msg for x in ("401", "403", "unauthorized", "login", "cookies", "authenticate")):
            raise DownloadError("X rejected the request (auth error)." + _tw_cookie_hint) from err

    if "private" in msg or "login" in msg or "cookies" in msg:
        raise DownloadError(
            "This content is private or requires login. "
            "If the site is Instagram, add COOKIES_FILE with a browser cookies export."
        ) from err

    raise DownloadError(f"Download failed: {raw[:500]}") from err


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
    if platform is Platform.TWITTER:
        # Multi-video tweets are extracted as playlists; autonumber prevents filename collisions.
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


async def download_media(url: str, settings: Settings) -> DownloadResult:
    url = normalize_http_url(url)
    platform = detect_platform(url)
    if platform is Platform.UNKNOWN:
        raise DownloadError(
            "Unsupported URL. Send a link from Instagram, TikTok, X (Twitter), or YouTube."
        )

    out_dir = settings.temp_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_stem = f"{uuid.uuid4().hex}"

    logger.info("Downloading url=%s platform=%s", url, platform.value)

    paths: list[Path] = []
    title: str | None = None

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
            _map_download_failure(platform, last_err, settings)
        return DownloadResult(path=None, paths=[], title=title, direct_urls=[], platform=platform)

    return DownloadResult(path=paths[0], paths=paths, title=title, direct_urls=[], platform=platform)


async def get_direct_urls(url: str, settings: Settings) -> list[str]:
    return await asyncio.to_thread(_extract_direct_urls, normalize_http_url(url), settings)
