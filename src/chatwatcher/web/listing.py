"""Searchable, filterable, paged lists for the dashboard: streams, moments (the Clips page),
queue jobs and the streams the channel watcher has seen. Every list page's SQL lives here;
app.py only reads the query string and hands the Page to a template."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chatwatcher.web import queries

STREAMS_PER_PAGE = 20
MOMENTS_PER_PAGE = 24
JOBS_PER_PAGE = 30
SEEN_PER_PAGE = 20

# Each moment's newest analysis, and Claude's rating in it.
_LATEST = """latest AS (SELECT moment_id, MAX(id) AS id FROM analyses WHERE moment_id IS NOT NULL GROUP BY moment_id)"""
_RATING = "json_extract(a.analysis_json, '$.clip_worthiness')"
_TITLE = "json_extract(a.analysis_json, '$.titles[0].text')"
_STREAM_TIME = "datetime(COALESCE(v.published_at, v.added_at))"


@dataclass
class Page:
    """One page of a list, and what the pager needs to draw itself."""
    items: list[dict[str, Any]]
    total: int
    page: int
    per_page: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))

    @property
    def first(self) -> int:
        return (self.page - 1) * self.per_page + 1 if self.total else 0

    @property
    def last(self) -> int:
        return min(self.total, self.page * self.per_page)

    @property
    def numbers(self) -> list[int | None]:
        """Page numbers to show: the ends and the pages around this one; None = a gap."""
        keep = {1, self.pages, *range(self.page - 2, self.page + 3)}
        out: list[int | None] = []
        for n in sorted(p for p in keep if 1 <= p <= self.pages):
            if out and out[-1] is not None and n - out[-1] > 1:
                out.append(None)
            out.append(n)
        return out


def to_int(text: Any, default: int = 0) -> int:
    """A query-string number; anything else (an empty select, junk) is the default."""
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


def _like(q: str) -> str:
    return "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _paged(conn: sqlite3.Connection, sql: str, args: list[Any], order: str, page: int, per_page: int,
           *, cte: str = "") -> tuple[list[sqlite3.Row], int, int]:
    """Rows of one page, the total, and the page actually shown (a page past the end shows the last)."""
    total = conn.execute(f"{cte} SELECT COUNT(*) FROM ({sql})", args).fetchone()[0]
    pages = max(1, -(-total // per_page))
    page = min(max(1, page), pages)
    rows = conn.execute(f"{cte} {sql} ORDER BY {order} LIMIT ? OFFSET ?",
                        (*args, per_page, (page - 1) * per_page)).fetchall()
    return rows, total, page


# ── streams (home) ───────────────────────────────────────────────────────────

STREAM_STATUS = {
    "review": "to review", "reviewed": "reviewed", "clips": "with clips", "busy": "in progress", "failed": "failed",
}
STREAM_SORT = {
    "new": ("newest first", f"{_STREAM_TIME} DESC"),
    "old": ("oldest first", f"{_STREAM_TIME}"),
    "rating": ("best rating", f"top_w DESC NULLS LAST, n_great DESC, {_STREAM_TIME} DESC"),
    "review": ("most to review", f"n_left DESC, {_STREAM_TIME} DESC"),
}

_STREAMS_CTE = f"""WITH {_LATEST},
  agg AS (SELECT video_id, COUNT(*) AS n_moments, SUM(status = 'approved') AS n_kept,
                 SUM(status = 'rejected') AS n_skipped FROM moments GROUP BY video_id),
  top AS (SELECT m.video_id, MAX({_RATING}) AS top_w, SUM({_RATING} >= 8) AS n_great
          FROM moments m JOIN latest l ON l.moment_id = m.id JOIN analyses a ON a.id = l.id GROUP BY m.video_id),
  clipn AS (SELECT m.video_id, COUNT(*) AS n_clips FROM clips cl JOIN moments m ON m.id = cl.moment_id
            GROUP BY m.video_id)"""

_STREAMS_SQL = """
  SELECT v.*, c.title AS channel_title,
         COALESCE(agg.n_moments, 0) AS n_moments, COALESCE(agg.n_kept, 0) AS n_kept,
         COALESCE(agg.n_skipped, 0) AS n_skipped,
         COALESCE(agg.n_moments, 0) - COALESCE(agg.n_kept, 0) - COALESCE(agg.n_skipped, 0) AS n_left,
         COALESCE(clipn.n_clips, 0) AS n_clips, top.top_w AS top_w, COALESCE(top.n_great, 0) AS n_great
  FROM videos v LEFT JOIN channels c ON c.id = v.channel_id
       LEFT JOIN agg ON agg.video_id = v.id LEFT JOIN top ON top.video_id = v.id
       LEFT JOIN clipn ON clipn.video_id = v.id
  WHERE v.status != 'skipped'"""


def streams(conn: sqlite3.Connection, *, q: str = "", channel: int = 0, status: str = "", kind: str = "",
            between: tuple[str, str] | None = None, sort: str = "new", page: int = 1,
            per_page: int = STREAMS_PER_PAGE, with_upload: bool = False) -> Page:
    """The streams the home page lists. between: (from, to) UTC in SQLite format."""
    where, args = [], []
    if q.strip():
        where.append("(v.title LIKE ? ESCAPE '\\' OR c.title LIKE ? ESCAPE '\\' OR v.video_id = ?)")
        args += [_like(q), _like(q), q.strip()]
    if channel:
        where.append("v.channel_id = ?")
        args.append(channel)
    if kind:
        where.append("COALESCE(NULLIF(v.stream_type, 'auto'), v.detected_type) = ?")
        args.append(kind)
    if between:
        where.append(f"{_STREAM_TIME} >= ? AND {_STREAM_TIME} < ?")
        args += list(between)
    cond = {
        "review": "n_left > 0",
        "reviewed": "COALESCE(agg.n_moments, 0) > 0 AND n_left = 0",
        "clips": "COALESCE(clipn.n_clips, 0) > 0",
        "busy": "(v.status = 'processing' OR EXISTS (SELECT 1 FROM jobs j WHERE j.video_id = v.id "
                "AND j.status IN ('pending', 'running')))",
        "failed": "(v.status = 'error' OR EXISTS (SELECT 1 FROM jobs j WHERE j.video_id = v.id AND j.status = 'error'))",
    }.get(status)
    if cond:
        where.append(cond)
    sql = _STREAMS_SQL + "".join(f" AND {w}" for w in where)
    order = STREAM_SORT.get(sort, STREAM_SORT["new"])[1]
    rows, total, page = _paged(conn, sql, args, order, page, per_page, cte=_STREAMS_CTE)
    items = []
    for r in rows:
        d = dict(r)
        d["stages"] = queries.stage_map(conn, r["id"], with_upload=with_upload)
        d["status"] = queries.display_status(d["status"], d["stages"])
        d["top"] = queries.top_moment(conn, r["id"]) if r["top_w"] is not None else None
        items.append(d)
    return Page(items, total, page, per_page)


def stream_channels(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Channels that have at least one stream, for the filter."""
    return conn.execute(
        """SELECT c.id, c.title, COUNT(*) AS n FROM videos v JOIN channels c ON c.id = v.channel_id
           WHERE v.status != 'skipped' GROUP BY c.id ORDER BY c.title""").fetchall()


# ── moments across every stream (the Clips page) ─────────────────────────────

MOMENT_SHOW = {
    "": "All moments", "clips": "Downloaded", "kept": "Kept", "new": "To review", "edited": "Edited",
}
MOMENT_SORT = {
    "rating": ("best rating", "worthiness DESC NULLS LAST, m.score DESC"),
    "new": ("newest stream", f"{_STREAM_TIME} DESC, m.start_s"),
    "chat": ("chat score", "m.score DESC"),
}

_MOMENTS_SQL = f"""
  SELECT m.id, m.rank, m.start_s, m.end_s, m.status, m.kind, m.score,
         v.video_id AS yt_id, v.title AS video_title, COALESCE(v.published_at, v.added_at) AS stream_at,
         c.title AS channel_title, {_RATING} AS worthiness, {_TITLE} AS title,
         json_extract(a.analysis_json, '$.category') AS category,
         json_extract(a.analysis_json, '$.summary') AS summary,
         cl.id AS clip_id, cl.path AS clip_path, cl.frames_json AS clip_frames, cl.uploaded_at, cl.height,
         p.frames_json AS preview_frames,
         e.rendered_at AS edit_rendered, e.moment_id AS has_edit,
         s.rendered_at AS short_rendered, s.moment_id AS has_short
  FROM moments m JOIN videos v ON v.id = m.video_id LEFT JOIN channels c ON c.id = v.channel_id
       LEFT JOIN latest l ON l.moment_id = m.id LEFT JOIN analyses a ON a.id = l.id
       LEFT JOIN clips cl ON cl.moment_id = m.id LEFT JOIN previews p ON p.moment_id = m.id
       LEFT JOIN edits e ON e.moment_id = m.id LEFT JOIN shorts s ON s.moment_id = m.id
  WHERE v.status != 'skipped'"""


def thumb(yt_id: str, moment_id: int, clip_id: int | None, clip_frames: str | None,
          preview_frames: str | None) -> str:
    """A picture for a moment: its peak frame (the clip's, else the preview's), else the stream's thumbnail."""
    for frames, base in ((clip_frames, f"/media/frame/{clip_id}"), (preview_frames, f"/media/preview/{moment_id}")):
        try:
            fs = json.loads(frames) if frames else []
        except json.JSONDecodeError:
            fs = []
        if fs:
            f = next((x for x in fs if x.get("is_peak")), fs[len(fs) // 2])
            return f"{base}/{str(f.get('file', '')).split('/')[-1]}"
    return stream_thumb(yt_id)


def stream_thumb(yt_id: str) -> str:
    return f"https://i.ytimg.com/vi/{yt_id}/mqdefault.jpg"


def moments(conn: sqlite3.Connection, *, q: str = "", show: str = "", min_rating: int = 0, category: str = "",
            channel: int = 0, sort: str = "rating", page: int = 1, per_page: int = MOMENTS_PER_PAGE) -> Page:
    where, args = [], []
    if q.strip():
        like = _like(q)
        where.append(f"({_TITLE} LIKE ? ESCAPE '\\' OR json_extract(a.analysis_json, '$.summary') LIKE ? ESCAPE '\\' "
                     "OR v.title LIKE ? ESCAPE '\\' OR c.title LIKE ? ESCAPE '\\')")
        args += [like, like, like, like]
    cond = {"clips": "cl.id IS NOT NULL", "kept": "m.status = 'approved'", "new": "m.status = 'new'",
            "edited": "(e.moment_id IS NOT NULL OR s.moment_id IS NOT NULL)"}.get(show)
    if cond:
        where.append(cond)
    if min_rating:
        where.append(f"{_RATING} >= ?")
        args.append(min_rating)
    if category:
        where.append("json_extract(a.analysis_json, '$.category') = ?")
        args.append(category)
    if channel:
        where.append("v.channel_id = ?")
        args.append(channel)
    sql = _MOMENTS_SQL + "".join(f" AND {w}" for w in where)
    order = MOMENT_SORT.get(sort, MOMENT_SORT["rating"])[1]
    rows, total, page = _paged(conn, sql, args, order, page, per_page, cte=f"WITH {_LATEST}")
    items = []
    for r in rows:
        d = dict(r)
        d["thumb"] = thumb(r["yt_id"], r["id"], r["clip_id"], r["clip_frames"], r["preview_frames"])
        d["clip_exists"] = bool(r["clip_path"]) and Path(r["clip_path"]).is_file()
        items.append(d)
    return Page(items, total, page, per_page)


def moment_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many moments each tab of the Clips page holds."""
    r = conn.execute(
        """SELECT COUNT(*) AS n_all, SUM(m.status = 'approved') AS kept, SUM(m.status = 'new') AS new,
                  SUM(EXISTS (SELECT 1 FROM clips cl WHERE cl.moment_id = m.id)) AS clips,
                  SUM(EXISTS (SELECT 1 FROM edits e WHERE e.moment_id = m.id)
                      OR EXISTS (SELECT 1 FROM shorts s WHERE s.moment_id = m.id)) AS edited
           FROM moments m JOIN videos v ON v.id = m.video_id WHERE v.status != 'skipped'""").fetchone()
    return {"": r["n_all"] or 0, "clips": r["clips"] or 0, "kept": r["kept"] or 0, "new": r["new"] or 0,
            "edited": r["edited"] or 0}


def categories(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        f"""WITH {_LATEST} SELECT json_extract(a.analysis_json, '$.category') AS k, COUNT(*) AS n
            FROM latest l JOIN analyses a ON a.id = l.id GROUP BY k HAVING k IS NOT NULL ORDER BY n DESC""")]


# ── queue ────────────────────────────────────────────────────────────────────

JOB_STATUS = ("running", "pending", "error", "done")


def job_list(conn: sqlite3.Connection, *, q: str = "", status: str = "", stage: str = "", page: int = 1,
             per_page: int = JOBS_PER_PAGE) -> Page:
    """Queue jobs: what's running first, then failures, then what waits, then the newest finished."""
    where, args = [], []
    if status in JOB_STATUS:
        where.append("j.status = ?")
        args.append(status)
    if stage:
        where.append("j.stage = ?")
        args.append(stage)
    if q.strip():
        where.append("(v.title LIKE ? ESCAPE '\\' OR v.video_id = ?)")
        args += [_like(q), q.strip()]
    sql = """SELECT j.*, v.video_id AS yt_id, v.title AS title, m.rank AS moment_rank
             FROM jobs j JOIN videos v ON v.id = j.video_id LEFT JOIN moments m ON m.id = j.moment_id
             WHERE 1 = 1""" + "".join(f" AND {w}" for w in where)
    order = ("CASE j.status WHEN 'running' THEN 0 WHEN 'error' THEN 1 WHEN 'pending' THEN 2 ELSE 3 END, "
             "CASE WHEN j.status = 'pending' THEN j.run_after END, COALESCE(j.finished_at, j.updated_at) DESC, j.id DESC")
    rows, total, page = _paged(conn, sql, args, order, page, per_page)
    return Page([dict(r) for r in rows], total, page, per_page)


def job_counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = {s: 0 for s in JOB_STATUS}
    for r in conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"):
        out[r["status"]] = r["n"]
    out[""] = sum(out.values())
    return out


def job_stages(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT stage FROM jobs ORDER BY stage")]


# ── streams the channel watcher has seen ─────────────────────────────────────

def seen(conn: sqlite3.Connection, *, q: str = "", action: str = "", page: int = 1,
         per_page: int = SEEN_PER_PAGE) -> Page:
    where, args = [], []
    if action in ("queued", "skipped"):
        where.append("d.action = ?")
        args.append(action)
    if q.strip():
        where.append("(d.title LIKE ? ESCAPE '\\' OR c.title LIKE ? ESCAPE '\\' OR d.video_id = ?)")
        args += [_like(q), _like(q), q.strip()]
    sql = """SELECT d.*, c.title AS channel_title FROM discovered d LEFT JOIN channels c ON c.id = d.channel_id
             WHERE 1 = 1""" + "".join(f" AND {w}" for w in where)
    rows, total, page = _paged(conn, sql, args, "d.seen_at DESC", page, per_page)
    return Page([dict(r) for r in rows], total, page, per_page)
