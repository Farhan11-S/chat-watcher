"""Settings changed from the dashboard. They live in the database's `settings` table and
win over config.toml, so the worker (in this process or a separate `cw worker`) sees a
change at its next job without a restart."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from chatwatcher.config import Config

AUTO_UPLOAD = "drive.auto_upload"


def get(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value_json"])
    except json.JSONDecodeError:
        return default


def put(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT INTO settings (key, value_json) VALUES (?, ?) "
                 "ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json",
                 (key, json.dumps(value)))


def auto_upload(cfg: Config, conn: sqlite3.Connection | None) -> bool:
    """Upload each stream to Drive when it finishes? The dashboard switch wins over
    drive.auto_upload in config.toml."""
    if conn is None:
        return cfg.drive.auto_upload
    return bool(get(conn, AUTO_UPLOAD, cfg.drive.auto_upload))
