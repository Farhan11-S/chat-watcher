"""
db.py — Database access layer for the clipper.
Reads spike data and clip_jobs from PostgreSQL.
"""

import psycopg2
import psycopg2.extras
from datetime import datetime
from typing import Optional
from .config import Config


def get_conn(cfg: Config):
    return psycopg2.connect(
        host=cfg.db_host,
        port=cfg.db_port,
        dbname=cfg.db_name,
        user=cfg.db_user,
        password=cfg.db_password,
    )


# ── Streams ───────────────────────────────────────────────────────────────────

def get_stream(cfg: Config, stream_id: int) -> Optional[dict]:
    with get_conn(cfg) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM streams WHERE id = %s", (stream_id,))
            return cur.fetchone()


def get_spike(cfg: Config, spike_id: int) -> Optional[dict]:
    with get_conn(cfg) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM spike_results WHERE id = %s", (spike_id,))
            return cur.fetchone()


# ── Spike config ──────────────────────────────────────────────────────────────

def get_clip_config(cfg: Config, stream_id: int) -> dict:
    """
    Returns clip_window_secs and clip_resolution for the stream.
    Falls back to global config, then hardcoded defaults.
    """
    with get_conn(cfg) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Per-stream override
            cur.execute(
                "SELECT clip_window_secs, clip_resolution FROM spike_config WHERE stream_id = %s",
                (stream_id,)
            )
            row = cur.fetchone()
            if row:
                return {
                    "clip_window_secs": row["clip_window_secs"] or 600,
                    "clip_resolution":  row["clip_resolution"]  or "720p",
                }

            # Global default (stream_id IS NULL)
            cur.execute(
                "SELECT clip_window_secs, clip_resolution FROM spike_config WHERE stream_id IS NULL"
            )
            row = cur.fetchone()
            if row:
                return {
                    "clip_window_secs": row["clip_window_secs"] or 600,
                    "clip_resolution":  row["clip_resolution"]  or "720p",
                }

    return {"clip_window_secs": 600, "clip_resolution": "720p"}


# ── Spike results ─────────────────────────────────────────────────────────────

def get_spikes(cfg: Config, stream_id: int) -> list:
    with get_conn(cfg) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT stream_timestamp, stream_seconds, current_rate_per_min, spike_ratio
                FROM spike_results
                WHERE stream_id = %s
                ORDER BY stream_seconds ASC
                """,
                (stream_id,)
            )
            return cur.fetchall()


# ── Clip jobs ─────────────────────────────────────────────────────────────────

def create_clip_job(cfg: Config, stream_id: int) -> int:
    with get_conn(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO clip_jobs (stream_id, status, created_at)
                VALUES (%s, 'pending', NOW())
                RETURNING id
                """,
                (stream_id,)
            )
            job_id = cur.fetchone()[0]
        conn.commit()
    return job_id


def update_clip_job(cfg: Config, job_id: int, status: str,
                    error_msg: str = None, clips_count: int = None,
                    drive_folder_url: str = None, progress_msg: str = None):
    with get_conn(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE clip_jobs
                SET status = %s,
                    error_msg = %s,
                    clips_count = %s,
                    drive_folder_url = %s,
                    progress_msg = %s,
                    updated_at = NOW(),
                    finished_at = CASE WHEN %s IN ('done', 'error') THEN NOW() ELSE finished_at END
                WHERE id = %s
                """,
                (status, error_msg, clips_count, drive_folder_url, progress_msg, status, job_id)
            )
        conn.commit()


def get_clip_jobs(cfg: Config, stream_id: int) -> list:
    with get_conn(cfg) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM clip_jobs
                WHERE stream_id = %s
                ORDER BY created_at DESC
                """,
                (stream_id,)
            )
            return cur.fetchall()


def get_clip_job(cfg: Config, job_id: int) -> Optional[dict]:
    with get_conn(cfg) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM clip_jobs WHERE id = %s", (job_id,))
            return cur.fetchone()
