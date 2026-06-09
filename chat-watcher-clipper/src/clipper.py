"""
clipper.py — Single-spike clipping orchestrator.
Called by main.py with stream_id, spike_id, and job_id.
"""

import os
import logging
from .config import Config
from .db import (
    get_stream, get_spike, get_clip_config,
    update_clip_job,
)
from .downloader import make_clip
from .uploader import upload_clip, get_folder_url

logger = logging.getLogger(__name__)


def run_spike_clip_job(cfg: Config, stream_id: int, spike_id: int, job_id: int):
    """
    Pipeline for a single spike:
    1. Load stream + spike from DB
    2. Download/crop the clip window around the spike
    3. Upload clip to Google Drive
    4. Update job status in DB
    """
    logger.info(f"Starting clip job {job_id} — stream {stream_id}, spike {spike_id}")
    update_clip_job(cfg, job_id, "processing", progress_msg="Starting…")

    try:
        # Load stream
        stream = get_stream(cfg, stream_id)
        if not stream:
            raise ValueError(f"Stream {stream_id} not found")

        video_id = stream["video_id"]

        # Load spike
        spike = get_spike(cfg, spike_id)
        if not spike:
            raise ValueError(f"Spike {spike_id} not found")

        # Load clip window config
        clip_cfg         = get_clip_config(cfg, stream_id)
        clip_window_secs = clip_cfg["clip_window_secs"]
        clip_resolution  = clip_cfg["clip_resolution"]
        spike_secs       = float(spike["stream_seconds"])
        ts               = spike["stream_timestamp"].replace(":", "-")

        logger.info(f"Spike at {spike['stream_timestamp']} "
                    f"(ratio {spike['spike_ratio']:.2f}×) — "
                    f"window {clip_window_secs}s, resolution {clip_resolution}")

        # Output dir per stream
        output_dir = os.path.join(cfg.output_dir, video_id)
        os.makedirs(output_dir, exist_ok=True)

        clip_name = f"{video_id}_spike{spike_id:03d}_{ts}"

        # Progress: downloading
        half = clip_window_secs / 2
        start_ts = _fmt_ts(max(0, spike_secs - half))
        end_ts   = _fmt_ts(spike_secs + half)
        update_clip_job(cfg, job_id, "processing",
                        progress_msg=f"⬇ Downloading {start_ts} → {end_ts} in 60s parts ({clip_resolution})…")

        clip_paths = make_clip(
            video_id          = video_id,
            spike_seconds     = spike_secs,
            clip_window_secs  = clip_window_secs,
            output_dir        = output_dir,
            clip_name         = clip_name,
            cookies_file      = cfg.cookies_file,
            po_token          = cfg.yt_po_token,
            resolution        = clip_resolution,
        )

        parts_count = len(clip_paths)

        # Progress: uploading
        update_clip_job(cfg, job_id, "processing",
                        progress_msg=f"☁ Uploading {parts_count} part(s) to Google Drive…")

        # Upload all parts to Drive subfolder per video
        uploaded = 0
        for clip_path in clip_paths:
            ok, _ = upload_clip(
                local_path    = clip_path,
                remote        = cfg.rclone_remote,
                remote_folder = cfg.rclone_folder,
                subfolder     = video_id,
            )
            if ok:
                uploaded += 1
            else:
                logger.warning(f"Failed to upload {clip_path}")

        if uploaded == 0:
            raise RuntimeError("Failed to upload any clip parts to Google Drive")

        drive_url = get_folder_url(cfg.rclone_remote, cfg.rclone_folder, video_id)

        logger.info(f"Job {job_id} done — {uploaded}/{parts_count} part(s) uploaded to Drive")
        update_clip_job(cfg, job_id, "done",
                        clips_count=uploaded,
                        drive_folder_url=drive_url,
                        progress_msg=f"✅ Done — {uploaded} part(s) uploaded")

    except Exception as e:
        logger.error(f"Job {job_id} failed: {e}")
        update_clip_job(cfg, job_id, "error", error_msg=str(e), progress_msg=f"❌ {str(e)[:120]}")
        raise


def _fmt_ts(seconds: float) -> str:
    """Format seconds as MM:SS or HH:MM:SS."""
    s = max(0, int(seconds))
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"
