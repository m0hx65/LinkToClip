from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


async def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _run_ffmpeg_sync(
    input_path: Path,
    output_path: Path,
    target_bytes: int,
) -> bool:
    """
    Re-encode to H.264 + AAC in MP4, scaling down until roughly under target size.
    Uses two-pass style estimate: start with CRF and max resolution cap.
    """
    # Bitrate budget (rough): target_bits / duration
    try:
        dur_s = float(
            subprocess.check_output(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(input_path),
                ],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        )
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
        dur_s = 120.0

    dur_s = max(dur_s, 1.0)
    audio_bps = 128_000
    budget_bps = max(int((target_bytes * 8) / dur_s - audio_bps), 200_000)
    video_k = max(budget_bps // 1000, 200)

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vf",
        "scale='min(1280,iw)':-2",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-b:v",
        f"{video_k}k",
        "-maxrate",
        f"{int(video_k * 1.2)}k",
        "-bufsize",
        f"{int(video_k * 2)}k",
        "-movflags",
        "+faststart",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        str(output_path),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return output_path.is_file() and output_path.stat().st_size > 0
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.warning("ffmpeg compress failed: %s", e)
        return False


async def compress_video(
    input_path: Path,
    output_path: Path,
    target_bytes: int,
) -> bool:
    if not await ffmpeg_available():
        logger.error("ffmpeg not found on PATH")
        return False
    return await asyncio.to_thread(_run_ffmpeg_sync, input_path, output_path, target_bytes)


def _run_ios_compatible_sync(input_path: Path, output_path: Path) -> bool:
    """
    Re-encode to a broadly compatible MP4 profile for iOS players.
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "high",
        "-level",
        "4.1",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-movflags",
        "+faststart",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        str(output_path),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return output_path.is_file() and output_path.stat().st_size > 0
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.warning("ffmpeg iOS-compatible transcode failed: %s", e)
        return False


async def make_ios_compatible(input_path: Path, output_path: Path) -> bool:
    if not await ffmpeg_available():
        logger.error("ffmpeg not found on PATH")
        return False
    return await asyncio.to_thread(_run_ios_compatible_sync, input_path, output_path)
