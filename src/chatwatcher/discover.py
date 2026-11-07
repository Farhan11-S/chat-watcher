"""Channel watcher: read each channel's Streams tab and queue finished streams.

The Streams tab lists only streams (no uploads or Shorts), each with its live status.
Upcoming and live streams are ignored until a later check sees them as finished.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from chatwatcher import store
from chatwatcher.config import Config
from chatwatcher.ingest.ytdlp import YtDlp, YtDlpError
from chatwatcher.jobs import enqueue

_UC = re.compile(r"\b(UC[A-Za-z0-9_-]{22})\b")
_HANDLE = re.compile(r"(@[\w.\-·]+)")
STREAM_TYPES = ("auto", "talk", "gaming", "karaoke")


class DiscoverError(Exception):
    pass


def streams_url(text: str) -> str:
    """Channel URL / @handle / UC id → that channel's Streams tab URL."""
    t = text.strip()
    if m := _UC.search(t):
        return f"https://www.youtube.com/channel/{m.group(1)}/streams"
    if m := _HANDLE.search(t):
        return f"https://www.youtube.com/{m.group(1)}/streams"
    if t.startswith("http") and "youtube.com/" in t:
        base = re.sub(r"/(streams|videos|shorts|featured|live|community|about|playlists)/?$", "", t.rstrip("/"))
        return base.split("?")[0] + "/streams"
    raise DiscoverError(f"Not a channel: {text!r} (use a channel URL, @handle or UC… id)")


@dataclass
class CheckResult:
    channel: str
    queued: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (video_id, reason)
    waiting: int = 0                                              # upcoming / live right now
    error: str | None = None


def add_channel(cfg: Config, conn: sqlite3.Connection, text: str, *, stream_type: str = "auto",
                backfill: int | None = None, auto_upload: bool = False,
                ytdlp: YtDlp | None = None) -> tuple[sqlite3.Row, CheckResult]:
    """Start watching a channel and run its first check (queues `backfill` recent streams)."""
    if stream_type not in STREAM_TYPES:
        raise DiscoverError(f"Unknown stream type {stream_type!r}; use one of {STREAM_TYPES}")
    yt = ytdlp or YtDlp(cfg.youtube)
    try:
        listing = yt.channel_streams(streams_url(text), cfg.discover.streams_per_check)
    except YtDlpError as e:
        raise DiscoverError(f"Couldn't read that channel: {e}") from e
    channel_id = listing.get("channel_id") or listing.get("id")
    if not channel_id or not _UC.fullmatch(channel_id):
        raise DiscoverError(f"Couldn't find a channel id for {text!r}")
    pk = store.upsert_channel(conn, channel_id, handle=listing.get("uploader_id"),
                              title=listing.get("channel") or listing.get("uploader"))
    conn.execute("UPDATE channels SET enabled = 1, default_type = ?, auto_upload = ? WHERE id = ?",
                 (stream_type, int(auto_upload), pk))
    row = conn.execute("SELECT * FROM channels WHERE id = ?", (pk,)).fetchone()
    first = row["last_checked_at"] is None
    result = _process(cfg, conn, row, listing,
                      backfill=(cfg.discover.default_backfill if backfill is None else backfill) if first else None)
    return conn.execute("SELECT * FROM channels WHERE id = ?", (pk,)).fetchone(), result


def check_channel(cfg: Config, conn: sqlite3.Connection, row: sqlite3.Row, *,
                  ytdlp: YtDlp | None = None) -> CheckResult:
    yt = ytdlp or YtDlp(cfg.youtube)
    try:
        listing = yt.channel_streams(streams_url(row["channel_id"]), cfg.discover.streams_per_check)
    except YtDlpError as e:
        conn.execute("UPDATE channels SET last_checked_at = datetime('now') WHERE id = ?", (row["id"],))
        return CheckResult(channel=row["title"] or row["channel_id"], error=str(e).splitlines()[0][:300])
    first = row["last_checked_at"] is None
    return _process(cfg, conn, row, listing, backfill=cfg.discover.default_backfill if first else None)


def due_channels(conn: sqlite3.Connection, poll_minutes: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM channels WHERE enabled = 1 AND
               (last_checked_at IS NULL OR last_checked_at <= datetime('now', ?))
           ORDER BY COALESCE(last_checked_at, '')""",
        (f"-{int(poll_minutes)} minutes",),
    ).fetchall()


def check_due(cfg: Config, conn: sqlite3.Connection, *, ytdlp: YtDlp | None = None) -> list[CheckResult]:
    return [check_channel(cfg, conn, row, ytdlp=ytdlp)
            for row in due_channels(conn, cfg.discover.poll_minutes)]


def _process(cfg: Config, conn: sqlite3.Connection, channel: sqlite3.Row, listing: dict,
             *, backfill: int | None) -> CheckResult:
    """Queue new finished streams. backfill=None: every new one (normal check).
    backfill=N: first check of a channel, only the N newest; the rest are marked seen."""
    res = CheckResult(channel=channel["title"] or channel["channel_id"])
    finished_seen = 0
    for e in listing.get("entries") or []:
        vid = e.get("id")
        status = e.get("live_status")
        if not vid or status in ("is_upcoming", "is_live"):
            res.waiting += status in ("is_upcoming", "is_live")
            continue
        already = conn.execute("SELECT 1 FROM discovered WHERE video_id = ? UNION "
                               "SELECT 1 FROM videos WHERE video_id = ?", (vid, vid)).fetchone()
        finished_seen += 1
        if already:
            continue
        reason = None
        if is_members_only(e) and not cfg.youtube.cookies_file:
            reason = "members-only (set youtube.cookies_file from a member account to include)"
        elif (e.get("duration") or 0) and e["duration"] < cfg.discover.min_duration_s:
            reason = f"shorter than {cfg.discover.min_duration_s // 60} min"
        elif backfill is not None and finished_seen > backfill:
            reason = "older than when the channel was added"
        if reason:
            _mark(conn, vid, channel["id"], e, "skipped", reason)
            res.skipped.append((vid, reason))
            continue
        _mark(conn, vid, channel["id"], e, "queued", None)
        fields = {"channel_id": channel["id"], "title": e.get("title"), "duration_s": e.get("duration"),
                  "status": "new"}
        if channel["default_type"] and channel["default_type"] != "auto":
            fields["stream_type"] = channel["default_type"]
        pk = store.upsert_video(conn, vid, **fields)
        enqueue(conn, pk, "ingest")
        res.queued.append(vid)
    conn.execute("UPDATE channels SET last_checked_at = datetime('now') WHERE id = ?", (channel["id"],))
    return res


_MEMBERS = re.compile(r"メン限|メンバー限定|メンバーシップ限定|members?[- ]only|member'?s? limited", re.I)


def is_members_only(e: dict) -> bool:
    return e.get("availability") == "subscriber_only" or bool(_MEMBERS.search(e.get("title") or ""))


def _mark(conn: sqlite3.Connection, vid: str, channel_pk: int, e: dict, action: str, reason: str | None) -> None:
    conn.execute(
        """INSERT INTO discovered (video_id, channel_id, title, duration_s, action, reason)
           VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (video_id) DO NOTHING""",
        (vid, channel_pk, e.get("title"), e.get("duration"), action, reason))


def list_channels(conn: sqlite3.Connection, *, include_disabled: bool = False) -> list[sqlite3.Row]:
    where = "" if include_disabled else "WHERE c.enabled = 1"
    return conn.execute(
        f"""SELECT c.*, (SELECT COUNT(*) FROM videos v WHERE v.channel_id = c.id) AS videos
            FROM channels c {where} ORDER BY c.title""").fetchall()


def find_channel(conn: sqlite3.Connection, text: str) -> sqlite3.Row | None:
    t = text.strip()
    return conn.execute(
        """SELECT * FROM channels WHERE channel_id = ? OR handle = ? OR handle = ? OR title = ?
           OR CAST(id AS TEXT) = ?""",
        (t, t, "@" + t.lstrip("@"), t, t)).fetchone()
