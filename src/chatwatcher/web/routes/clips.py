"""The /clips list, the moment page, Keep / Skip, and the moment page's buttons.

`/moment/{pk}/{action}` catches every other POST under /moment/{pk}/, so app.py includes
this router after editor and brand."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from chatwatcher import jobs
from chatwatcher.clip import stitch
from chatwatcher.scoring.moments import fmt_ts
from chatwatcher.web import listing, queries
from chatwatcher.web.routes import Ctx, redirect


class StatusIn(BaseModel):
    status: str


def router(ctx: Ctx) -> APIRouter:
    r = APIRouter()
    cfg, render, get_conn = ctx.cfg, ctx.render, ctx.get_conn

    @r.get("/clips", response_class=HTMLResponse)
    def clips_page(request: Request, q: str = "", show: str = "", rating: str = "", category: str = "",
                   channel: str = "", sort: str = "", page: str = "", conn=Depends(get_conn)):
        """Every stream's moments in one list: the best ones, your kept ones, the downloaded clips."""
        show = show if show in listing.MOMENT_SHOW else ""
        sort = sort if sort in listing.MOMENT_SORT else "rating"
        flt = {"q": q.strip(), "show": show, "rating": listing.to_int(rating), "category": category,
               "channel": listing.to_int(channel), "sort": sort}
        flt["any"] = bool(flt["q"] or flt["rating"] or category or flt["channel"])
        moments = listing.moments(conn, q=q, show=show, min_rating=flt["rating"], category=category,
                                  channel=flt["channel"], sort=sort, page=listing.to_int(page, 1))
        return render(request, "clips.html", page=moments, flt=flt, counts=listing.moment_counts(conn),
                      shows=listing.MOMENT_SHOW, sorts=listing.MOMENT_SORT, categories=listing.categories(conn),
                      channels=listing.stream_channels(conn))

    @r.get("/moment/{pk}", response_class=HTMLResponse)
    def moment(request: Request, pk: int, conn=Depends(get_conn)):
        d = queries.moment_detail(conn, pk)
        if d is None:
            raise HTTPException(404, "Unknown moment")
        if d["moment"]["kind"] == stitch.KIND:
            d["pieces"] = stitch.pieces(d["moment"].get("compilation") or {})
            d["has_edit"] = conn.execute("SELECT 1 FROM edits WHERE moment_id = ? UNION ALL "
                                         "SELECT 1 FROM shorts WHERE moment_id = ?", (pk, pk)).fetchone() is not None
        return render(request, "moment.html", **d)

    @r.post("/moment/{pk}/status")
    def moment_status(pk: int, body: StatusIn, conn=Depends(get_conn)):
        """Keep / Skip / Undo from the stream page's review panel, without a page load."""
        if body.status not in ("approved", "rejected", "new"):
            raise HTTPException(400)
        m = conn.execute("SELECT video_id FROM moments WHERE id = ?", (pk,)).fetchone()
        if m is None:
            raise HTTPException(404)
        conn.execute("UPDATE moments SET status = ? WHERE id = ?", (body.status, pk))
        counts = conn.execute(
            """SELECT SUM(m.status = 'approved') AS kept,
                      SUM(m.status = 'approved' AND c.id IS NOT NULL) AS kept_clipped,
                      SUM(m.status = 'new') AS to_review
               FROM moments m LEFT JOIN clips c ON c.moment_id = m.id WHERE m.video_id = ?""",
            (m["video_id"],)).fetchone()
        return JSONResponse({"ok": True, "status": body.status, "kept": counts["kept"] or 0,
                             "kept_clipped": counts["kept_clipped"] or 0, "to_review": counts["to_review"] or 0})

    @r.post("/moment/{pk}/pieces")
    def moment_pieces(pk: int, start: list[str] = Form([]), end: list[str] = Form([]), role: list[str] = Form([]),
                      note: list[str] = Form([]), conn=Depends(get_conn)):
        """A compilation's pieces, changed on its page: save them and download the clip again
        (clip.stitch). One row per piece; a row with both times empty is left out."""
        m = conn.execute("SELECT * FROM moments WHERE id = ?", (pk,)).fetchone()
        if m is None:
            raise HTTPException(404)
        if m["kind"] != stitch.KIND:
            raise HTTPException(400)
        back = f"/moment/{pk}#pieces"
        job = conn.execute("SELECT status FROM jobs WHERE moment_id = ? AND stage = 'clip'", (pk,)).fetchone()
        if job and job["status"] == "running":
            return redirect(back, error="Its pieces are being downloaded right now. Change them when that is done.")
        rows = []
        for i, (a, b) in enumerate(zip(start, end)):
            if not a.strip() and not b.strip():
                continue
            try:
                rows.append({"start": _parse_ts(a), "end": _parse_ts(b), "role": role[i] if i < len(role) else "beat",
                             "note": note[i] if i < len(note) else ""})
            except ValueError as err:
                return redirect(back, error=f"Piece {len(rows) + 1}: {err}")
        v = conn.execute("SELECT duration_s FROM videos WHERE id = ?", (m["video_id"],)).fetchone()
        try:
            segments = stitch.check(rows, v["duration_s"] or 0)
        except ValueError as err:
            return redirect(back, error=str(err))
        changed = stitch.set_pieces(conn, pk, segments)
        clip = conn.execute("SELECT path FROM clips WHERE moment_id = ?", (pk,)).fetchone()
        had = bool(clip) and Path(clip["path"]).is_file()
        if had and not changed and not (job and job["status"] == "error"):
            return redirect(back, msg="These are the pieces the clip already has.")
        jobs.enqueue(conn, m["video_id"], "clip", moment_pk=pk)
        ctx.poke()
        return redirect(f"/moment/{pk}", msg="Saved. The new pieces are being downloaded and joined; the clip below is the old "
                                             "one until then, and its edit starts over: press ✨ AI edit again after."
                        if had else "Saved. The pieces are being downloaded and joined into one clip.")

    @r.post("/moment/{pk}/{action}")
    def moment_action(pk: int, action: str, start: str = Form(""), end: str = Form(""), conn=Depends(get_conn)):
        m = conn.execute("SELECT * FROM moments WHERE id = ?", (pk,)).fetchone()
        if m is None:
            raise HTTPException(404)
        back = f"/moment/{pk}"
        if action == "cut":
            if m["kind"] == "compilation":
                return redirect(back, error="A compilation is several pieces of the stream joined: trim it in the editor.")
            v = conn.execute("SELECT duration_s FROM videos WHERE id = ?", (m["video_id"],)).fetchone()
            try:
                s, e = _parse_ts(start), _parse_ts(end)
            except ValueError as err:
                return redirect(back, error=str(err))
            s, e = max(0.0, s), min(e, v["duration_s"] or e)
            if e - s < 5:
                return redirect(back, error="The clip must be at least 5 seconds long.")
            conn.execute("UPDATE moments SET start_s = ?, end_s = ? WHERE id = ?", (s, e, pk))
            jobs.enqueue(conn, m["video_id"], "clip", moment_pk=pk)
            return redirect(back, msg=f"New cut {fmt_ts(s)}–{fmt_ts(e)} queued; Claude re-analyses it after.")
        if action == "reclip":
            jobs.enqueue(conn, m["video_id"], "clip", moment_pk=pk)
            return redirect(back, msg="Queued.")
        if action == "reanalyze":
            jobs.enqueue(conn, m["video_id"], "analyze", moment_pk=pk)
            return redirect(back, msg="Queued for Claude to analyse again.")
        if action == "upload":
            if not cfg.drive.enabled:
                return redirect(back, error="Google Drive isn't set up (drive.enabled = false in config.toml).")
            jobs.enqueue(conn, m["video_id"], "upload", moment_pk=pk)
            return redirect(back, msg="Upload of this clip queued.")
        raise HTTPException(400)

    return r


def _parse_ts(text: str) -> float:
    """'1:02:03', '62:03', '3723' or '3723.5' → seconds."""
    t = (text or "").strip()
    if not t:
        raise ValueError("Missing time")
    try:
        parts = [float(x) for x in t.split(":")]
    except ValueError:
        raise ValueError(f"Bad time {text!r}; use h:mm:ss or seconds")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return secs
