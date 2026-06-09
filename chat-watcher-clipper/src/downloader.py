"""
downloader.py — Download VOD sections or full VOD using yt-dlp,
then crop clips with ffmpeg.
"""

import os
import subprocess
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

# Ensure Deno is in PATH for yt-dlp EJS challenge solver
_DENO_BIN = os.path.expanduser("~/.deno/bin")
if _DENO_BIN not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _DENO_BIN + ":" + os.environ.get("PATH", "")

PART_DURATION_SECS = 60  # each parallel chunk is 60 seconds


def seconds_to_hms(seconds: float) -> str:
    """Convert seconds to HH:MM:SS string."""
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _cookies_args(cookies_file: str) -> list:
    """Return yt-dlp cookie args if the file exists."""
    if cookies_file and os.path.exists(cookies_file):
        return ["--cookies", cookies_file]
    return []


def _po_token_args(po_token: str) -> list:
    """Return yt-dlp PO Token args if a token is provided."""
    if po_token:
        return [
            "--extractor-args",
            f"youtube:po_token=web+{po_token}",
        ]
    return []


def _resolution_format(resolution: str) -> str:
    """Map resolution string to yt-dlp format selector.

    Forces AVC (H.264) codec — the most edit-friendly codec for tools like
    Kdenlive, Premiere, DaVinci Resolve. VP9/AV1 are delivery codecs and
    cause import issues in video editors.
    """
    mapping = {
        "360p":  "bestvideo[height<=360][vcodec^=avc]+bestaudio[ext=m4a]/best[height<=360][vcodec^=avc]/best[height<=360]",
        "480p":  "bestvideo[height<=480][vcodec^=avc]+bestaudio[ext=m4a]/best[height<=480][vcodec^=avc]/best[height<=480]",
        "720p":  "bestvideo[height<=720][vcodec^=avc]+bestaudio[ext=m4a]/best[height<=720][vcodec^=avc]/best[height<=720]",
        "1080p": "bestvideo[height<=1080][vcodec^=avc]+bestaudio[ext=m4a]/best[height<=1080][vcodec^=avc]/best[height<=1080]",
        "best":  "bestvideo[vcodec^=avc]+bestaudio[ext=m4a]/best[vcodec^=avc]/best",
    }
    return mapping.get(resolution, mapping["720p"])


def _download_one_part(
    video_id: str,
    part_index: int,
    part_start: float,
    part_end: float,
    output_path: str,
    cookies_file: str,
    po_token: str,
    resolution: str,
) -> tuple[int, bool]:
    """
    Download a single time-range part via yt-dlp --download-sections.
    Returns (part_index, success).
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    start_hms = seconds_to_hms(part_start)
    end_hms   = seconds_to_hms(part_end)
    section   = f"*{start_hms}-{end_hms}"

    cmd = [
        "yt-dlp",
        "--download-sections", section,
        "-f", _resolution_format(resolution),
        "--remux-video", "mp4",
        "-o", output_path,
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        *_cookies_args(cookies_file),
        *_po_token_args(po_token),
        url,
    ]

    logger.info(f"Part {part_index:03d}: downloading {start_hms} → {end_hms}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)

    if result.returncode == 0 and os.path.exists(output_path):
        logger.info(f"Part {part_index:03d}: done → {output_path}")
        return part_index, True

    logger.warning(f"Part {part_index:03d}: failed (rc={result.returncode}): {result.stderr[:200]}")
    return part_index, False


def try_download_section_parts(
    video_id: str,
    start_sec: float,
    end_sec: float,
    output_dir: str,
    clip_name: str,
    cookies_file: str = "",
    po_token: str = "",
    resolution: str = "720p",
) -> list[str]:
    """
    Split [start_sec, end_sec] into PART_DURATION_SECS chunks and download
    each in parallel. Returns list of successfully downloaded part file paths,
    in chronological order.
    """
    total = end_sec - start_sec
    chunks = []
    t = start_sec
    idx = 1
    while t < end_sec:
        chunk_end = min(t + PART_DURATION_SECS + 2, end_sec)
        out_path = os.path.join(output_dir, f"{clip_name}_part{idx:03d}.mp4")
        chunks.append((idx, t, chunk_end, out_path))
        t = chunk_end
        idx += 1

    logger.info(f"Splitting {total:.0f}s into {len(chunks)} part(s) of up to {PART_DURATION_SECS}s each")

    results = {}  # part_index -> path

    with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
        futures = {
            pool.submit(
                _download_one_part,
                video_id, part_idx, part_start, part_end, out_path,
                cookies_file, po_token, resolution,
            ): (part_idx, out_path)
            for part_idx, part_start, part_end, out_path in chunks
        }

        for future in as_completed(futures):
            part_idx, out_path = futures[future]
            try:
                _, success = future.result()
                if success:
                    results[part_idx] = out_path
            except Exception as e:
                logger.warning(f"Part {part_idx:03d} raised exception: {e}")

    # Return paths sorted by part index (chronological order)
    return [results[i] for i in sorted(results)]


def download_full_vod(video_id: str, output_path: str, cookies_file: str = "",
                      po_token: str = "", resolution: str = "720p") -> bool:
    """Download the full VOD as mp4."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        "yt-dlp",
        "-f", _resolution_format(resolution),
        "--remux-video", "mp4",
        "-o", output_path,
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        *_cookies_args(cookies_file),
        *_po_token_args(po_token),
        url,
    ]

    logger.info(f"Downloading full VOD: {video_id} ({resolution})")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)

    if result.returncode == 0 and os.path.exists(output_path):
        logger.info(f"Full VOD downloaded: {output_path}")
        return True

    logger.error(f"Full VOD download failed: {result.stderr[:300]}")
    return False


def crop_clip(source_path: str, start_sec: float, end_sec: float,
              output_path: str) -> bool:
    """Crop a clip from a local video file using ffmpeg (fast stream copy)."""
    duration = end_sec - start_sec
    cmd = [
        "ffmpeg",
        "-ss", str(start_sec),
        "-i", source_path,
        "-t", str(duration),
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        "-y",
        output_path,
    ]

    logger.info(f"Cropping clip: {seconds_to_hms(start_sec)} → {seconds_to_hms(end_sec)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    if result.returncode == 0 and os.path.exists(output_path):
        logger.info(f"Clip saved: {output_path}")
        return True

    logger.error(f"ffmpeg crop failed: {result.stderr[:300]}")
    return False


def make_clip(video_id: str, spike_seconds: float, clip_window_secs: int,
              output_dir: str, clip_name: str, cookies_file: str = "",
              po_token: str = "", resolution: str = "720p") -> list[str]:
    """
    Main entry point. Downloads the clip window split into 60s parts in parallel.
    Falls back to full VOD + crop if all parts fail.
    Returns list of output file paths (one per part), raises RuntimeError on total failure.
    """
    os.makedirs(output_dir, exist_ok=True)

    half      = clip_window_secs / 2
    start_sec = max(0, spike_seconds - half)
    end_sec   = spike_seconds + half

    # Strategy 1: parallel part downloads
    parts = try_download_section_parts(
        video_id, start_sec, end_sec, output_dir, clip_name,
        cookies_file, po_token, resolution,
    )
    if parts:
        return parts

    # Strategy 2: full VOD download + ffmpeg crop (single file fallback)
    logger.info("All part downloads failed — falling back to full VOD download + crop")
    full_vod_path = os.path.join(output_dir, f"{video_id}_full.mp4")
    output_path   = os.path.join(output_dir, f"{clip_name}.mp4")

    if not os.path.exists(full_vod_path):
        if not download_full_vod(video_id, full_vod_path, cookies_file, po_token, resolution):
            raise RuntimeError(f"Both part downloads and full VOD download failed for {video_id}")

    if crop_clip(full_vod_path, start_sec, end_sec, output_path):
        return [output_path]

    raise RuntimeError(f"ffmpeg crop failed for {video_id} at {seconds_to_hms(spike_seconds)}")
