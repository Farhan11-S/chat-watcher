"""
db.py — PostgreSQL database access layer

Tables:
    streams       — stream links + watch status
    spike_config  — per-stream or global detection thresholds
    spike_results — detected spikes per stream
"""

from __future__ import annotations

import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from typing import Generator

from src.config import Config


def get_connection(config: Config):
    """Open a new psycopg2 connection using DB settings from Config."""
    return psycopg2.connect(
        host=config.db_host,
        port=config.db_port,
        dbname=config.db_name,
        user=config.db_user,
        password=config.db_password,
    )


@contextmanager
def cursor(config: Config) -> Generator[psycopg2.extensions.cursor, None, None]:
    """Context manager: yields a DictCursor and auto-commits or rolls back."""
    conn = get_connection(config)
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                yield cur
    finally:
        conn.close()


# ── Streams ───────────────────────────────────────────────────────────────────

def add_stream(config: Config, video_id: str, url: str) -> int:
    """
    Insert a new stream (status=pending). Returns the new stream id.
    If the video_id already exists, returns the existing id without modifying it.
    """
    with cursor(config) as cur:
        cur.execute(
            """
            INSERT INTO streams (video_id, url)
            VALUES (%s, %s)
            ON CONFLICT (video_id) DO UPDATE SET video_id = EXCLUDED.video_id
            RETURNING id
            """,
            (video_id, url),
        )
        return cur.fetchone()["id"]


def update_stream_info(
    config: Config,
    stream_id: int,
    title: str,
    channel: str | None,
    stream_start: str | None,
    duration_secs: int | None,
):
    """Populate metadata fetched from YouTube after the stream is identified."""
    with cursor(config) as cur:
        cur.execute(
            """
            UPDATE streams
            SET title = %s, channel = %s, stream_start = %s, duration_secs = %s
            WHERE id = %s
            """,
            (title, channel, stream_start, duration_secs, stream_id),
        )


def set_stream_status(
    config: Config,
    stream_id: int,
    status: str,
    error_message: str | None = None,
):
    """Update stream status. Valid values: pending, processing, done, error."""
    with cursor(config) as cur:
        cur.execute(
            """
            UPDATE streams
            SET status = %s,
                error_message = %s,
                processed_at = CASE WHEN %s IN ('done', 'error') THEN NOW() ELSE processed_at END
            WHERE id = %s
            """,
            (status, error_message, status, stream_id),
        )


def get_pending_streams(config: Config) -> list[dict]:
    """Return all streams with status='pending', oldest first."""
    with cursor(config) as cur:
        cur.execute(
            "SELECT * FROM streams WHERE status = 'pending' ORDER BY added_at ASC"
        )
        return cur.fetchall()


def get_stream_by_video_id(config: Config, video_id: str) -> dict | None:
    """Fetch a single stream row by video_id."""
    with cursor(config) as cur:
        cur.execute("SELECT * FROM streams WHERE video_id = %s", (video_id,))
        return cur.fetchone()


def list_streams(config: Config) -> list[dict]:
    """Return all rows from the stream_summary view."""
    with cursor(config) as cur:
        cur.execute("SELECT * FROM stream_summary ORDER BY id DESC")
        return cur.fetchall()


# ── Spike Config ──────────────────────────────────────────────────────────────

def get_spike_config(config: Config, stream_id: int | None = None) -> dict:
    """
    Return spike detection config for a specific stream.
    Falls back to the global default (stream_id IS NULL) if no per-stream config exists.
    """
    with cursor(config) as cur:
        if stream_id is not None:
            cur.execute(
                "SELECT * FROM spike_config WHERE stream_id = %s", (stream_id,)
            )
            row = cur.fetchone()
            if row:
                return dict(row)

        # Fall back to global default
        cur.execute("SELECT * FROM spike_config WHERE stream_id IS NULL")
        return dict(cur.fetchone())


def set_spike_config(
    config: Config,
    stream_id: int | None,
    window_seconds: int,
    baseline_windows: int,
    spike_multiplier: float,
    min_messages: int,
    cooldown_seconds: int,
):
    """
    Upsert spike detection config for a stream (or global default if stream_id is None).
    """
    with cursor(config) as cur:
        cur.execute(
            """
            INSERT INTO spike_config
                (stream_id, window_seconds, baseline_windows, spike_multiplier,
                 min_messages, cooldown_seconds)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (stream_id) DO UPDATE SET
                window_seconds   = EXCLUDED.window_seconds,
                baseline_windows = EXCLUDED.baseline_windows,
                spike_multiplier = EXCLUDED.spike_multiplier,
                min_messages     = EXCLUDED.min_messages,
                cooldown_seconds = EXCLUDED.cooldown_seconds
            """,
            (
                stream_id, window_seconds, baseline_windows,
                spike_multiplier, min_messages, cooldown_seconds,
            ),
        )


# ── Spike Results ─────────────────────────────────────────────────────────────

def save_spike(config: Config, stream_id: int, spike) -> int:
    """Insert a single Spike into spike_results. Returns the new row id."""
    with cursor(config) as cur:
        cur.execute(
            """
            INSERT INTO spike_results
                (stream_id, stream_timestamp, stream_seconds,
                 current_rate_per_min, baseline_rate_per_min,
                 spike_ratio, messages_in_window)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                stream_id,
                spike.stream_timestamp,
                spike.stream_seconds,
                spike.current_rate_per_min,
                spike.baseline_rate_per_min,
                spike.spike_ratio,
                spike.messages_in_window,
            ),
        )
        return cur.fetchone()["id"]


def save_all_spikes(config: Config, stream_id: int, spikes: list) -> int:
    """Replace all spikes for a stream — deletes existing rows then bulk-inserts new ones."""
    with cursor(config) as cur:
        # Always clear existing spikes first so reruns don't accumulate
        cur.execute("DELETE FROM spike_results WHERE stream_id = %s", (stream_id,))
        if not spikes:
            return 0
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO spike_results
                (stream_id, stream_timestamp, stream_seconds,
                 current_rate_per_min, baseline_rate_per_min,
                 spike_ratio, messages_in_window)
            VALUES %s
            """,
            [
                (
                    stream_id,
                    s.stream_timestamp,
                    s.stream_seconds,
                    s.current_rate_per_min,
                    s.baseline_rate_per_min,
                    s.spike_ratio,
                    s.messages_in_window,
                )
                for s in spikes
            ],
        )
        return len(spikes)


def get_spikes_for_stream(config: Config, stream_id: int) -> list[dict]:
    """Return all spike results for a stream, ordered by stream time."""
    with cursor(config) as cur:
        cur.execute(
            """
            SELECT * FROM spike_results
            WHERE stream_id = %s
            ORDER BY stream_seconds ASC
            """,
            (stream_id,),
        )
        return cur.fetchall()


# ── Chat Messages ──────────────────────────────────────────────────────────────

def save_chat_messages(config: Config, stream_id: int, messages: list[dict]) -> int:
    """Bulk-insert all chat messages for a stream. Clears existing messages first."""
    with cursor(config) as cur:
        cur.execute("DELETE FROM chat_messages WHERE stream_id = %s", (stream_id,))
        if not messages:
            return 0

        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO chat_messages
                (stream_id, time_in_seconds, stream_timestamp, author, message)
            VALUES %s
            """,
            [
                (
                    stream_id,
                    m["time_in_seconds"],
                    _format_stream_timestamp(m["time_in_seconds"]),
                    m.get("author", ""),
                    m.get("message", ""),
                )
                for m in messages
            ],
        )
        return len(messages)


def get_chat_messages_for_window(
    config: Config,
    stream_id: int,
    center_seconds: float,
    window_seconds: int = 60,
    max_messages: int = 100,
) -> list[dict]:
    """Fetch chat messages around a spike timestamp for summarization."""
    half = window_seconds / 2
    start = center_seconds - half
    end = center_seconds + half
    with cursor(config) as cur:
        cur.execute(
            """
            SELECT author, message, stream_timestamp
            FROM chat_messages
            WHERE stream_id = %s
              AND time_in_seconds >= %s
              AND time_in_seconds <= %s
            ORDER BY time_in_seconds ASC
            LIMIT %s
            """,
            (stream_id, start, end, max_messages),
        )
        return cur.fetchall()


def _format_stream_timestamp(seconds: float) -> str:
    """Convert seconds to HH:MM:SS or MM:SS string."""
    s = max(0, int(seconds))
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"
