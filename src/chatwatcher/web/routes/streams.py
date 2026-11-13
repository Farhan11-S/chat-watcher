"""The streams list (home), adding a stream, the stream page and its buttons."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from chatwatcher import clock, discover, jobs, prefs, storage, store
from chatwatcher.ingest.ytdlp import extract_video_id
from chatwatcher.paths import vod_files
from chatwatcher.web import charts, listing, queries
from chatwatcher.web.routes import Ctx, redirect


def router(ctx: Ctx) -> APIRouter:
    r = APIRouter()
    cfg, tz, render, get_conn = ctx.cfg, ctx.tz, ctx.render, ctx.get_conn

    @r.get("/", response_class=HTMLResponse)
    def home(request: Request, day: str = "", start: str = Query("", alias="from"), end: str = Query("", alias="to"),
             q: str = "", channel: str = "", status: str = "", kind: str = Query("", alias="type"),
             sort: str = "", page: str = "", conn=Depends(get_conn)):
        """The streams: searched, filtered, sorted and paged (web/listing.py).
        ?day=today, or ?from=YYYY-MM-DD&to=YYYY-MM-DD (local days, both included)."""
        from datetime import date as _date, timedelta as _td
        today = clock.today(tz)
        span, error = None, None
        try:
            lo = _date.fromisoformat(start) if start else None
            hi = _date.fromisoformat(end) if end else None
        except ValueError:
            lo = hi = None
            error = "Dates must look like 2026-11-09."
        if lo or hi:                                   # typed dates win over Today / Yesterday
            lo, hi = lo or _date(2000, 1, 1), hi or today
            if lo > hi:
                lo, hi = hi, lo
            day = ""
        elif day in ("today", "yesterday"):
            lo = hi = today - _td(days=1 if day == "yesterday" else 0)
        if lo and hi:
            span = clock.local_days(lo, hi, tz)
        status = status if status in listing.STREAM_STATUS else ""
        kind = kind if kind in discover.STREAM_TYPES and kind != "auto" else ""
        sort = sort if sort in listing.STREAM_SORT else "new"
        chan = listing.to_int(channel)
        streams = listing.streams(conn, q=q, channel=chan, status=status, kind=kind, between=span, sort=sort,
                                  page=listing.to_int(page, 1), with_upload=cfg.drive.enabled)
        flt = {"day": day if span and day else "", "from": lo.isoformat() if span and not day else "",
               "to": hi.isoformat() if span and not day else "", "on": bool(span), "today": today.isoformat(),
               "label": ("today" if day == "today" else "yesterday" if day == "yesterday"
                         else lo.isoformat() if span and lo == hi else f"{lo.isoformat()} – {hi.isoformat()}" if span else ""),
               "q": q.strip(), "channel": chan, "status": status, "type": kind, "sort": sort,
               "any": bool(span or q.strip() or chan or status or kind)}
        return render(request, "home.html", summary=queries.summary(conn), page=streams, videos=streams.items,
                      flt=flt, channels=listing.stream_channels(conn), statuses=listing.STREAM_STATUS,
                      sorts=listing.STREAM_SORT, types=[t for t in discover.STREAM_TYPES if t != "auto"],
                      error=error or request.query_params.get("error"))

    @r.post("/add")
    def add_video(url: str = Form(...), conn=Depends(get_conn)):
        try:
            vid = extract_video_id(url)
        except ValueError as e:
            return redirect("/", error=str(e))
        pk = store.upsert_video(conn, vid)
        jobs.enqueue(conn, pk, "ingest")
        return redirect(f"/video/{vid}", msg="Queued. The worker will process it.")

    @r.get("/video/{video_id}", response_class=HTMLResponse)
    def video(request: Request, video_id: str, conn=Depends(get_conn)):
        d = queries.video_detail(conn, video_id, with_upload=cfg.drive.enabled)
        if d is None:
            raise HTTPException(404, "Unknown video")
        files = vod_files(cfg, video_id)
        chart = heat = None
        duration = d["video"]["duration_s"] or 0
        if files.scores.is_file():
            scores = json.loads(files.scores.read_text())
            chart = charts.score_timeline(scores, d["moments"], duration, cfg.scoring.min_score)
        if files.heatmap.is_file() and duration:
            heat = charts.heatmap_strip(json.loads(files.heatmap.read_text()), duration)
        kept = sum(1 for m in d["moments"] if m["status"] == "approved" and m["clip_id"])
        kept_all = sum(1 for m in d["moments"] if m["status"] == "approved")
        return render(request, "video.html", **d, chart=chart, heat=heat, kept=kept,
                      kept_all=kept_all, review=_review_items(d["moments"]),
                      auto_upload=prefs.auto_upload(cfg, conn),
                      compile=_compile_state(conn, d["video"]["id"], files.transcript.is_file()))

    @r.post("/video/{video_id}/{action}")
    def video_action(video_id: str, action: str, stream_type: str = Form("auto"), kept_only: str = Form(""),
                     back_to: str = Form(""), conn=Depends(get_conn)):
        v = store.get_video(conn, video_id)
        if v is None:
            raise HTTPException(404)
        back = f"/video/{video_id}"
        if action == "remove":
            if conn.execute("SELECT 1 FROM jobs WHERE video_id = ? AND status = 'running'", (v["id"],)).fetchone():
                return redirect(back, error="The worker is busy with this stream right now. Try again in a moment.")
            freed = storage.remove_video(cfg, conn, v, "removed from the dashboard")
            title = (v["title"] or video_id)[:80]
            msg = f"Removed {title}" + (f" and freed {storage.fmt_bytes(freed)}" if freed else "") + \
                ". The channel watcher won't fetch it again" + (" (Google Drive wasn't touched)." if cfg.drive.enabled else ".")
            return redirect("/jobs" if back_to == "jobs" else "/", msg=msg)
        if action == "retry":
            n = jobs.retry_video(conn, v["id"])
            if not n:
                jobs.enqueue(conn, v["id"], "ingest")
            store.upsert_video(conn, video_id, status="processing", error=None)
            return redirect(back, msg="Queued again.")
        if action == "clip":
            n = conn.execute("SELECT COUNT(*) FROM moments WHERE video_id = ? AND status = 'approved'",
                             (v["id"],)).fetchone()[0]
            if not n:
                return redirect(back, error="Mark the moments you want with ✓ Keep first.")
            jobs.enqueue(conn, v["id"], "clip", params={"kept_only": True})
            then = " Claude analyses them after." if cfg.pipeline.auto_analyze and cfg.claude.enabled else ""
            return redirect(back, msg=f"Downloading {n} kept moment(s).{then}")
        if action == "upload":
            if not cfg.drive.enabled:
                return redirect(back, error="Google Drive isn't set up (drive.enabled = false in config.toml).")
            if kept_only:
                from chatwatcher.upload.drive import clip_folders
                if not clip_folders(conn, video_id, kept_only=True):
                    return redirect(back, error="No downloaded clips are marked ✓ Keep yet.")
                jobs.enqueue(conn, v["id"], "upload", params={"kept_only": True})
                return redirect(back, msg="Upload of the kept clips queued.")
            jobs.enqueue(conn, v["id"], "upload")
            return redirect(back, msg="Upload queued.")
        if action == "compile":
            if not cfg.claude.enabled:
                return redirect(back + "#compilations", error="Claude is switched off (claude.enabled = false in config.toml).")
            if not vod_files(cfg, video_id).info.is_file():
                return redirect(back + "#compilations", error="This stream hasn't been fetched yet.")
            if jobs.enqueue(conn, v["id"], "compile") is None:
                return redirect(back + "#compilations", error="Claude is already reading this stream.")
            ctx.poke()
            return redirect(back + "#compilations",
                            msg="Queued. Claude reads the whole stream once; this takes a few minutes.")
        if action == "rescore":
            from chatwatcher.scoring.service import ScoreError, score_video
            if stream_type in discover.STREAM_TYPES:
                store.upsert_video(conn, video_id, stream_type=stream_type)
            try:
                r = score_video(cfg, conn, video_id, force=True)
            except ScoreError as e:
                return redirect(back, error=str(e))
            if cfg.pipeline.auto_clip:
                jobs.enqueue(conn, v["id"], "clip")
            then = " Clips queued." if cfg.pipeline.auto_clip else " Review them, then press Clip kept."
            return redirect(back, msg=f"Re-scored as {r.stream_type}: {len(r.moments)} moments.{then}")
        raise HTTPException(400)

    return r


def _compile_state(conn, video_pk: int, has_captions: bool) -> dict[str, Any]:
    """The stream page's Compilation ideas card: the last run's candidates and the job."""
    from chatwatcher.compile.service import load
    job = conn.execute("SELECT status, message, error, run_after FROM jobs WHERE video_id = ? AND stage = 'compile'",
                       (video_pk,)).fetchone()
    return {"run": load(conn, video_pk), "job": dict(job) if job else None, "has_captions": has_captions}


def _review_items(moments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the stream page's player panel needs per moment, in stream order."""
    out = []
    for m in sorted(moments, key=lambda m: m["start_s"]):
        a = m.get("analysis") or {}
        out.append({
            "id": m["id"], "rank": m["rank"], "start": m["start_s"], "end": m["end_s"], "peak": m["peak_s"],
            "status": m["status"], "kind": m["kind"], "score": round(m["score"], 2),
            "reasons": [r["signal"] for r in (m.get("reasons") or [])[:4]],
            "chat": [[t, n] for t, n in (m.get("top_messages") or [])[:5]],
            "title": (a.get("titles") or [{}])[0].get("text", ""), "summary": a.get("summary", ""),
            "why": a.get("why_it_works", ""), "context": a.get("context", ""),
            "worthiness": a.get("clip_worthiness"), "category": a.get("category"),
            "model": a.get("model"), "effort": (a.get("_meta") or {}).get("effort"),
            "analysed_at": a.get("created_at"), "aid": a.get("_id"),
            "clipped": bool(m.get("clip_id")),
        })
    return out
