"""Warns on the dashboard before (and after) the Google Drive sign-in expires.

Google apps in "Testing" give rclone a sign-in that lasts 7 days. We note when the
current sign-in started (the refresh token changes on every reconnect) and count from there.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from chatwatcher.config import Config
from chatwatcher.upload.drive import Rclone, RcloneError

WARN_BEFORE = timedelta(days=2)
_KEY = "drive_login"


@dataclass
class LoginNotice:
    level: str        # "warn" | "err"
    text: str
    command: str      # what to run to sign in again


def _reconnect_cmd(cfg: Config, rc: Rclone | None) -> str:
    exe = rc.exe if rc else cfg.drive.rclone_path
    return f"{exe} config reconnect {cfg.drive.remote.rstrip(':')}:"


def _load(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT value_json FROM settings WHERE key = ?", (_KEY,)).fetchone()
    return json.loads(row["value_json"]) if row else {}


def _save(conn: sqlite3.Connection, value: dict) -> None:
    conn.execute("INSERT INTO settings (key, value_json) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json", (_KEY, json.dumps(value)))


def _auth_failing(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        """SELECT 1 FROM jobs WHERE stage = 'upload' AND status IN ('pending', 'error')
           AND error LIKE '%login expired%' LIMIT 1""").fetchone() is not None


def drive_login_notice(cfg: Config, conn: sqlite3.Connection, *, rclone: Rclone | None = None,
                       now: datetime | None = None) -> LoginNotice | None:
    if not cfg.drive.enabled:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        rc = rclone or Rclone(cfg.drive)
    except RcloneError:
        return None
    cmd = _reconnect_cmd(cfg, rc)

    fp = rc.login_fingerprint()
    saved = _load(conn)
    if fp and fp != saved.get("fp"):
        # New sign-in: start counting, and let uploads waiting on the old one run now.
        saved = {"fp": fp, "at": now.isoformat()}
        _save(conn, saved)
        conn.execute("""UPDATE jobs SET status = 'pending', run_after = datetime('now'), attempts = 0, error = NULL,
                               updated_at = datetime('now')
                        WHERE stage = 'upload' AND status IN ('pending', 'error')
                          AND error LIKE '%login expired%'""")

    if _auth_failing(conn):
        return LoginNotice("err", "Google Drive sign-in has expired, so uploads are paused. "
                                  "Sign in again and they'll continue on their own.", cmd)

    days = cfg.drive.login_expires_days
    if not days or not saved.get("at"):
        return None
    expires = datetime.fromisoformat(saved["at"]) + timedelta(days=days)
    left = expires - now
    if left > WARN_BEFORE:
        return None
    from chatwatcher.clock import abbr, zone
    local = expires.astimezone(zone(cfg))
    when = local.strftime("%a %d %b, %H:%M ") + abbr(local)
    if left <= timedelta(0):
        return LoginNotice("err", f"Google Drive sign-in expired on {when}. Sign in again to keep uploading.", cmd)
    hours = int(left.total_seconds() // 3600)
    span = f"{hours} hour{'s' if hours != 1 else ''}" if hours < 48 else f"{left.days} days"
    return LoginNotice("warn", f"Google Drive sign-in expires in {span} ({when}). Sign in again before then "
                               "so uploads don't stop.", cmd)
