"""Job queue on the `jobs` table: one row per (video, stage). Restart-safe."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

STAGES = ("ingest", "score", "clip", "analyze", "upload")


def sql_time(dt: datetime) -> str:
    """UTC in SQLite's datetime('now') format, so text comparison works."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(conn: sqlite3.Connection, video_pk: int, stage: str, *, moment_pk: int | None = None,
            run_after: datetime | None = None, reset_attempts: bool = True) -> int | None:
    """Queue (or re-queue) a stage, for the whole video or for one moment (redo a clip or
    an analysis). A job that is currently running is left alone. Returns the job id, or
    None if it was running."""
    if stage not in STAGES:
        raise ValueError(f"Unknown stage {stage!r}")
    if moment_pk is not None and stage not in ("clip", "analyze"):
        raise ValueError("Only clip and analyze can run for a single moment")
    row = conn.execute(
        """
        INSERT INTO jobs (video_id, moment_id, stage, status, run_after)
        VALUES (?, ?, ?, 'pending', ?)
        ON CONFLICT (video_id, stage, COALESCE(moment_id, 0)) DO UPDATE SET
            status = 'pending', run_after = excluded.run_after, error = NULL,
            attempts = CASE WHEN ? THEN 0 ELSE jobs.attempts END,
            updated_at = datetime('now')
        WHERE jobs.status != 'running'
        RETURNING id
        """,
        (video_pk, moment_pk, stage, sql_time(run_after or now()), int(reset_attempts)),
    ).fetchone()
    return row["id"] if row else None


def claim(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Take the next due job and mark it running (atomic)."""
    return conn.execute(
        """
        UPDATE jobs SET status = 'running', attempts = attempts + 1,
                        started_at = datetime('now'), updated_at = datetime('now')
        WHERE id = (SELECT id FROM jobs WHERE status = 'pending' AND run_after <= datetime('now')
                    ORDER BY run_after, id LIMIT 1)
        RETURNING *
        """
    ).fetchone()


def finish(conn: sqlite3.Connection, job_id: int, *, ok: bool, message: str | None = None,
           retry_in: timedelta | None = None) -> None:
    """ok → done. retry_in → pending again later. Otherwise → error."""
    if retry_in is not None:
        conn.execute(
            """UPDATE jobs SET status = 'pending', run_after = ?, error = ?, message = ?,
                   updated_at = datetime('now') WHERE id = ?""",
            (sql_time(now() + retry_in), message, message, job_id))
    elif ok:
        conn.execute(
            """UPDATE jobs SET status = 'done', error = NULL, message = ?, finished_at = datetime('now'),
                   updated_at = datetime('now') WHERE id = ?""", (message, job_id))
    else:
        conn.execute(
            """UPDATE jobs SET status = 'error', error = ?, message = ?, finished_at = datetime('now'),
                   updated_at = datetime('now') WHERE id = ?""", (message, message, job_id))


def reset_running(conn: sqlite3.Connection) -> int:
    """After a crash/restart nothing is really running: put those jobs back in the queue."""
    return conn.execute(
        "UPDATE jobs SET status = 'pending', updated_at = datetime('now') WHERE status = 'running'"
    ).rowcount


def retry_video(conn: sqlite3.Connection, video_pk: int) -> int:
    """Re-queue a video's failed or waiting jobs to run now."""
    return conn.execute(
        """UPDATE jobs SET status = 'pending', run_after = datetime('now'), attempts = 0, error = NULL,
               updated_at = datetime('now')
           WHERE video_id = ? AND status IN ('error', 'pending')""", (video_pk,)).rowcount


def list_jobs(conn: sqlite3.Connection, *, include_done: bool = False, limit: int = 50) -> list[sqlite3.Row]:
    where = "" if include_done else "WHERE j.status != 'done'"
    return conn.execute(
        f"""
        SELECT j.*, v.video_id AS yt_id, v.title AS title, m.rank AS moment_rank
        FROM jobs j JOIN videos v ON v.id = j.video_id LEFT JOIN moments m ON m.id = j.moment_id
        {where}
        ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'error' THEN 1 WHEN 'pending' THEN 2 ELSE 3 END,
                 j.run_after, j.id
        LIMIT ?
        """, (limit,)).fetchall()


def next_stage(stage: str) -> str | None:
    i = STAGES.index(stage)
    return STAGES[i + 1] if i + 1 < len(STAGES) else None
