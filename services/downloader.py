from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yt_dlp

from platforms import Platform, detect_platform
from platforms import instagram as ig_mod
from platforms import tiktok as tt_mod
from platforms import twitter as tw_mod
from platforms import youtube as yt_mod
from utils.config import Settings
from utils.urltools import normalize_http_url

logger = logging.getLogger(__name__)
_TW_I_STATUS_RE = re.compile(
    r"^(https?://)(?:www\.)?(?:x\.com|twitter\.com)/i/status/(\d+)(?:[/?#].*)?$",
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
    # X/Twitter: guest-only (no cookiefile). yt-dlp uses GraphQL + syndication fallbacks below.
    if platform is Platform.TWITTER:
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

    if platform is Platform.TWITTER and "no video could be found in this tweet" in msg:
        raise DownloadError(
            "Could not get a video from this X link. The post may have no video, or X did not "
            "expose embeddable media to automated access (common for some threads or clips)."
        ) from err

    if "private" in msg or "login" in msg or "cookies" in msg:
        raise DownloadError(
            "This content is private or requires login. "
            "If the site is Instagram, add COOKIES_FILE with a browser cookies export."
        ) from err

    raise DownloadError(f"Download failed: {raw[:500]}") from err


@dataclass
class DownloadResult:
    path: Path | None
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
    return merged, platform


def _twitter_candidate_urls(url: str) -> list[str]:
    """Generate equivalent tweet URLs for extractor edge-cases."""
    m = _TW_I_STATUS_RE.match(url.strip())
    if not m:
        return [url]
    tweet_id = m.group(2)
    return [
        url,
        f"https://x.com/i/web/status/{tweet_id}",
        f"https://twitter.com/i/web/status/{tweet_id}",
        f"https://twitter.com/i/status/{tweet_id}",
    ]


def _twitter_ydl_opts_variants(ydl_opts: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Guest-only strategies: default GraphQL, then syndication API."""
    synd = _merge_dict(
        ydl_opts,
        {"extractor_args": {"twitter": {"api": ["syndication"]}}},
    )
    return [("graphql", ydl_opts), ("syndication", synd)]


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


def _download_sync(url: str, ydl_opts: dict[str, Any]) -> tuple[Path | None, str | None]:
    last_path: Path | None = None
    title: str | None = None

    def hook(d: dict[str, Any]) -> None:
        nonlocal last_path
        if d.get("status") == "finished":
            fp = d.get("filename")
            if fp:
                last_path = Path(fp)

    opts = dict(ydl_opts)
    opts["progress_hooks"] = [hook]
    opts.setdefault("logger", _YtdlpLogger())
    opts.setdefault("noprogress", True)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info:
            title = info.get("title") or info.get("id")
            fn = ydl.prepare_filename(info)
            candidate = Path(fn)
            if candidate.is_file():
                last_path = candidate
            elif "requested_downloads" in info:
                for part in info["requested_downloads"]:
                    p = part.get("filepath")
                    if p and Path(p).is_file():
                        last_path = Path(p)
                        break

    return last_path, title


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
    ydl_opts, _ = _build_ydl_opts(url, out_dir, out_stem, settings)
    candidate_urls = (
        _twitter_candidate_urls(url) if platform is Platform.TWITTER else [url]
    )
    if platform is Platform.TWITTER:
        opts_variants = _twitter_ydl_opts_variants(ydl_opts)
    else:
        opts_variants = [("default", ydl_opts)]

    logger.info("Downloading url=%s platform=%s", url, platform.value)

    path: Path | None = None
    title: str | None = None
    attempts = 1 if platform is Platform.INSTAGRAM else 2
    last_err: Exception | None = None
    for strategy_name, opt_variant in opts_variants:
        if platform is Platform.TWITTER:
            logger.info("Twitter yt-dlp strategy=%s", strategy_name)
        for candidate in candidate_urls:
            for attempt in range(attempts):
                try:
                    path, title = await asyncio.to_thread(_download_sync, candidate, opt_variant)
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
            if path and path.is_file():
                break
        if path and path.is_file():
            break

    if not path or not path.is_file():
        if last_err is not None:
            _map_download_failure(platform, last_err, settings)
        return DownloadResult(
            path=None,
            title=title,
            direct_urls=[],
            platform=platform,
        )

    return DownloadResult(
        path=path,
        title=title,
        direct_urls=[],
        platform=platform,
    )


async def get_direct_urls(url: str, settings: Settings) -> list[str]:
    return await asyncio.to_thread(_extract_direct_urls, normalize_http_url(url), settings)
