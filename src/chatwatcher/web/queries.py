"""Read models for the dashboard pages."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from chatwatcher.jobs import STAGES


def _j(text: str | None, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except json.JSONDecodeError:
        return default


def version_stamp(conn: sqlite3.Connection) -> str:
    """Changes whenever anything the pages show changes (used for auto-refresh)."""
    row = conn.execute(
        """SELECT (SELECT COALESCE(MAX(updated_at), '') FROM jobs) || '|' ||
                  (SELECT COUNT(*) FROM jobs) || '|' ||
                  (SELECT COALESCE(MAX(updated_at), '') FROM videos) || '|' ||
                  (SELECT COUNT(*) FROM analyses) || '|' ||
                  (SELECT COALESCE(MAX(last_checked_at), '') FROM channels)""").fetchone()
    return row[0]


def summary(conn: sqlite3.Connection) -> dict[str, int]:
    out = {s: 0 for s in ("new", "processing", "done", "error")}
    for r in conn.execute("SELECT status, COUNT(*) n FROM videos WHERE status != 'skipped' GROUP BY status"):
        out[r["status"]] = r["n"]
    for r in conn.execute("SELECT status, COUNT(*) n FROM jobs WHERE status != 'done' GROUP BY status"):
        out[f"jobs_{r['status']}"] = r["n"]
    out["channels"] = conn.execute("SELECT COUNT(*) FROM channels WHERE enabled = 1").fetchone()[0]
    out["clips"] = conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
    out["storyboards"] = conn.execute("SELECT COUNT(*) FROM analyses WHERE moment_id IS NOT NULL").fetchone()[0]
    return out


def stage_map(conn: sqlite3.Connection, video_pk: int, *, with_upload: bool = False) -> dict[str, dict[str, Any]]:
    """Per-stage status from the job queue; for work done by hand with `cw ingest/score/...`
    (no job rows), infer it from the data. The upload stage is shown only when Drive is on
    or the stream was uploaded."""
    rows = conn.execute("SELECT * FROM jobs WHERE video_id = ? AND moment_id IS NULL", (video_pk,)).fetchall()
    by = {r["stage"]: dict(r) for r in rows}
    v = conn.execute("SELECT ingested_at, scored_at, drive_url FROM videos WHERE id = ?", (video_pk,)).fetchone()
    have = {
        "ingest": bool(v and v["ingested_at"]),
        "score": bool(v and v["scored_at"]),
        "clip": conn.execute("SELECT 1 FROM clips c JOIN moments m ON m.id = c.moment_id "
                             "WHERE m.video_id = ? LIMIT 1", (video_pk,)).fetchone() is not None,
        "frames": conn.execute("SELECT 1 FROM previews p JOIN moments m ON m.id = p.moment_id "
                               "WHERE m.video_id = ? LIMIT 1", (video_pk,)).fetchone() is not None,
        "analyze": conn.execute("SELECT 1 FROM analyses WHERE video_id = ? AND moment_id IS NOT NULL "
                                "LIMIT 1", (video_pk,)).fetchone() is not None,
        "upload": bool(v and v["drive_url"]),
    }
    stages = [s for s in STAGES if s != "upload" or with_upload or "upload" in by or have["upload"]]
    return {s: by.get(s, {"status": "done" if have[s] else "none"}) for s in stages}


def display_status(status: str, stages: dict[str, dict[str, Any]]) -> str:
    if status == "new" and all(j["status"] == "done" for j in stages.values()):
        return "done"
    return status


def videos(conn: sqlite3.Connection, limit: int = 50, *, with_upload: bool = False) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT v.*, c.title AS channel_title,
               (SELECT COUNT(*) FROM moments m WHERE m.video_id = v.id) AS n_moments,
               (SELECT COUNT(*) FROM clips cl JOIN moments m ON m.id = cl.moment_id WHERE m.video_id = v.id) AS n_clips,
               (SELECT COUNT(DISTINCT a.moment_id) FROM analyses a WHERE a.video_id = v.id AND a.moment_id IS NOT NULL) AS n_analyses,
               (SELECT COUNT(*) FROM moments m WHERE m.video_id = v.id AND m.status = 'approved') AS n_kept,
               (SELECT COUNT(*) FROM moments m WHERE m.video_id = v.id AND m.status = 'rejected') AS n_skipped
        FROM videos v LEFT JOIN channels c ON c.id = v.channel_id
        WHERE v.status != 'skipped'
        ORDER BY COALESCE(v.published_at, v.added_at) DESC LIMIT ?
        """, (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["stages"] = stage_map(conn, r["id"], with_upload=with_upload)
        d["status"] = display_status(d["status"], d["stages"])
        out.append(d)
    return out


def latest_analysis(conn: sqlite3.Connection, moment_pk: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM analyses WHERE moment_id = ? ORDER BY id DESC LIMIT 1",
                       (moment_pk,)).fetchone()
    if not row:
        return None
    return {"model": row["model"], "created_at": row["created_at"], "_id": row["id"], **_j(row["analysis_json"], {})}


def video_detail(conn: sqlite3.Connection, video_id: str, *, with_upload: bool = False) -> dict[str, Any] | None:
    v = conn.execute(
        """SELECT v.*, c.title AS channel_title, c.handle AS channel_handle
           FROM videos v LEFT JOIN channels c ON c.id = v.channel_id WHERE v.video_id = ?""",
        (video_id,)).fetchone()
    if v is None:
        return None
    moments = []
    for m in conn.execute(
            """SELECT m.*, cl.id AS clip_id, cl.height, cl.fps, cl.vcodec, cl.size_bytes, cl.folder,
                      cl.uploaded_at, cl.path AS clip_path
               FROM moments m LEFT JOIN clips cl ON cl.moment_id = m.id
               WHERE m.video_id = ? ORDER BY m.rank""", (v["id"],)):
        d = dict(m) | _j(m["features_json"], {})
        d["analysis"] = latest_analysis(conn, m["id"])
        d["clip_exists"] = bool(m["clip_path"]) and Path(m["clip_path"]).is_file()
        moments.append(d)
    digest = conn.execute(
        "SELECT * FROM analyses WHERE video_id = ? AND moment_id IS NULL ORDER BY id DESC LIMIT 1",
        (v["id"],)).fetchone()
    moment_jobs = conn.execute(
        "SELECT * FROM jobs WHERE video_id = ? AND moment_id IS NOT NULL AND status != 'done'",
        (v["id"],)).fetchall()
    stages = stage_map(conn, v["id"], with_upload=with_upload)
    video = dict(v)
    video["status"] = display_status(video["status"], stages)
    return {"video": video, "stages": stages, "moments": moments,
            "digest": _j(digest["analysis_json"], None) if digest else None,
            "moment_jobs": {r["moment_id"]: dict(r) for r in moment_jobs}}


def moment_detail(conn: sqlite3.Connection, moment_pk: int) -> dict[str, Any] | None:
    m = conn.execute("SELECT * FROM moments WHERE id = ?", (moment_pk,)).fetchone()
    if m is None:
        return None
    v = conn.execute("SELECT * FROM videos WHERE id = ?", (m["video_id"],)).fetchone()
    clip = conn.execute("SELECT * FROM clips WHERE moment_id = ?", (moment_pk,)).fetchone()
    preview = conn.execute("SELECT * FROM previews WHERE moment_id = ?", (moment_pk,)).fetchone()
    neighbours = conn.execute(
        """SELECT id, rank FROM moments WHERE video_id = ? AND rank IN (?, ?)""",
        (m["video_id"], m["rank"] - 1, m["rank"] + 1)).fetchall()
    jobs = conn.execute("SELECT * FROM jobs WHERE moment_id = ? AND status != 'done'", (moment_pk,)).fetchall()
    d = dict(m) | _j(m["features_json"], {})
    files: dict[str, str] = {}
    from chatwatcher.analyze.service import material_folder
    folder = material_folder(clip["folder"] if clip else None, preview["folder"] if preview else None)
    if folder:
        for name in ("transcript.txt", "chat_excerpt.txt"):
            p = folder / name
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace")
                files[name] = text if len(text) < 400_000 else text[:400_000] + "\n…(truncated)"
    return {
        "moment": d, "video": dict(v),
        "clip": dict(clip) | {"exists": Path(clip["path"]).is_file()} if clip else None,
        "frames": _j(clip["frames_json"], []) if clip else [],
        "preview": dict(preview) | {"frames": _j(preview["frames_json"], [])} if preview else None,
        "analysis": latest_analysis(conn, moment_pk), "files": files,
        "prev": next((r["id"] for r in neighbours if r["rank"] == m["rank"] - 1), None),
        "next": next((r["id"] for r in neighbours if r["rank"] == m["rank"] + 1), None),
        "jobs": [dict(j) for j in jobs],
    }
