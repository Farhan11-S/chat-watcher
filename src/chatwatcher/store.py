"""Small data-access helpers for channels and videos."""

from __future__ import annotations

import sqlite3
from typing import Any


def upsert_channel(conn: sqlite3.Connection, channel_id: str, *, handle: str | None,
                   title: str | None) -> int:
    """Insert or refresh a channel. New channels found via a single VOD are *not* watched
    (enabled=0) until the user turns them on."""
    row = conn.execute(
        """
        INSERT INTO channels (channel_id, handle, title, enabled)
        VALUES (?, ?, ?, 0)
        ON CONFLICT (channel_id) DO UPDATE SET
            handle = COALESCE(excluded.handle, channels.handle),
            title  = COALESCE(excluded.title, channels.title)
        RETURNING id
        """,
        (channel_id, handle, title),
    ).fetchone()
    return row["id"]


def upsert_video(conn: sqlite3.Connection, video_id: str, **fields: Any) -> int:
    """Insert a video row if missing, then update the given columns."""
    conn.execute("INSERT INTO videos (video_id) VALUES (?) ON CONFLICT (video_id) DO NOTHING",
                 (video_id,))
    if fields:
        bad = [k for k in fields if not k.isidentifier()]
        if bad:
            raise ValueError(f"Invalid column name(s): {bad}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE videos SET {cols}, updated_at = datetime('now') WHERE video_id = ?",
            (*fields.values(), video_id),
        )
    return conn.execute("SELECT id FROM videos WHERE video_id = ?", (video_id,)).fetchone()["id"]


def get_video(conn: sqlite3.Connection, video_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,)).fetchone()


def list_videos(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT v.*, c.title AS channel_title
        FROM videos v LEFT JOIN channels c ON c.id = v.channel_id
        ORDER BY COALESCE(v.published_at, v.added_at) DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
