"""FastAPI app for the local dashboard. Long work never runs in a request: buttons queue
jobs, and the worker (in this process, or a separate `cw worker`) does them."""

from __future__ import annotations

import fcntl
import json
import logging
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from chatwatcher import db, discover, jobs, store
from chatwatcher.config import Config
from chatwatcher.ingest.ytdlp import extract_video_id
from chatwatcher.paths import vod_files
from chatwatcher.pipeline import Deps
from chatwatcher.scoring.moments import fmt_ts
from chatwatcher.web import charts, queries

log = logging.getLogger("chatwatcher.web")
HERE = Path(__file__).parent


def _redirect(url: str, msg: str | None = None, error: str | None = None) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    if msg:
        url += f"{sep}msg={quote(msg)}"
    elif error:
        url += f"{sep}error={quote(error)}"
    return RedirectResponse(url, status_code=303)


def external_worker_running(cfg: Config) -> bool:
    lock = cfg.paths.data_dir / "worker.lock"
    if not lock.exists():
        return False
    with open(lock, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
    return False


def create_app(cfg: Config, *, start_worker: bool = False, deps: Deps | None = None) -> FastAPI:
    state: dict[str, Any] = {"worker": None, "thread": None}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        conn = db.open_db(cfg.paths.db_path)   # create / migrate once
        conn.close()
        if start_worker:
            from chatwatcher.worker import Worker, WorkerLocked
            w = Worker(cfg, deps=deps)

            def run() -> None:
                try:
                    w.run()
                except WorkerLocked:
                    log.info("A separate worker is already running; the dashboard won't start one.")
                except Exception:
                    log.exception("Worker thread crashed")

            t = threading.Thread(target=run, name="chatwatcher-worker", daemon=True)
            t.start()
            state.update(worker=w, thread=t)
        yield
        if state["worker"]:
            state["worker"].stop.set()
            state["thread"].join(timeout=3)

    app = FastAPI(title="chat-watcher", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["ts"] = lambda s: fmt_ts(s or 0)
    templates.env.filters["mb"] = lambda n: f"{(n or 0) / 1_048_576:.0f} MB"
    templates.env.filters["json"] = lambda v: Markup(json.dumps(v, ensure_ascii=False))

    def get_conn() -> Iterator[sqlite3.Connection]:
        # One connection per request; FastAPI may open, use and close it on different threads.
        conn = db.connect(cfg.paths.db_path, check_same_thread=False)
        try:
            yield conn
        finally:
            conn.close()

    def worker_status() -> str:
        if state["thread"] is not None and state["thread"].is_alive() and state["worker"]._lock_file:
            return "running here"
        if external_worker_running(cfg):
            return "running (separate process)"
        return "stopped"

    login_cache: dict[str, Any] = {"at": 0.0, "notice": None}

    def drive_login() -> Any:
        # Asking rclone takes a moment, so check at most once a minute.
        if time.monotonic() - login_cache["at"] > 60:
            from chatwatcher.upload.login import drive_login_notice
            conn = db.connect(cfg.paths.db_path, check_same_thread=False)
            try:
                login_cache["notice"] = drive_login_notice(cfg, conn, rclone=deps.rclone if deps else None)
            except Exception:
                log.exception("Drive sign-in check failed")
                login_cache["notice"] = None
            finally:
                conn.close()
            login_cache["at"] = time.monotonic()
        return login_cache["notice"]

    def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        ctx.setdefault("msg", request.query_params.get("msg"))
        ctx.setdefault("error", request.query_params.get("error"))
        return templates.TemplateResponse(request, name, {"worker": worker_status(), "cfg": cfg,
                                                          "drive_login": drive_login(), **ctx})

    # ── pages ────────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, conn=Depends(get_conn)):
        videos = queries.videos(conn, with_upload=cfg.drive.enabled)
        summary = queries.summary(conn)
        for st in ("new", "processing", "done", "error"):   # count what the table shows
            summary[st] = sum(v["status"] == st for v in videos)
        return render(request, "home.html", summary=summary, videos=videos, queue=jobs.list_jobs(conn, limit=12))

    @app.post("/add")
    def add_video(url: str = Form(...), conn=Depends(get_conn)):
        try:
            vid = extract_video_id(url)
        except ValueError as e:
            return _redirect("/", error=str(e))
        pk = store.upsert_video(conn, vid)
        jobs.enqueue(conn, pk, "ingest")
        return _redirect(f"/video/{vid}", msg="Queued. The worker will process it.")

    @app.get("/channels", response_class=HTMLResponse)
    def channels(request: Request, conn=Depends(get_conn)):
        return render(request, "channels.html", channels=discover.list_channels(conn, include_disabled=True),
                      discovered=conn.execute(
                          """SELECT d.*, c.title AS channel_title FROM discovered d
                             LEFT JOIN channels c ON c.id = d.channel_id ORDER BY d.seen_at DESC LIMIT 30""").fetchall())

    @app.post("/channels")
    def channel_add(channel: str = Form(...), stream_type: str = Form("auto"), backfill: int = Form(1),
                    conn=Depends(get_conn)):
        try:
            row, res = discover.add_channel(cfg, conn, channel, stream_type=stream_type,
                                            backfill=backfill, ytdlp=deps.ytdlp if deps else None)
        except discover.DiscoverError as e:
            return _redirect("/channels", error=str(e))
        return _redirect("/channels", msg=f"Watching {row['title']}: queued {len(res.queued)}, "
                                          f"skipped {len(res.skipped)}, {res.waiting} upcoming/live.")

    @app.post("/channels/{pk}/{action}")
    def channel_action(pk: int, action: str, stream_type: str = Form("auto"), conn=Depends(get_conn)):
        row = conn.execute("SELECT * FROM channels WHERE id = ?", (pk,)).fetchone()
        if not row:
            raise HTTPException(404)
        if action in ("enable", "disable"):
            conn.execute("UPDATE channels SET enabled = ? WHERE id = ?", (int(action == "enable"), pk))
        elif action == "remove":
            conn.execute("DELETE FROM channels WHERE id = ?", (pk,))
        elif action == "type" and stream_type in discover.STREAM_TYPES:
            conn.execute("UPDATE channels SET default_type = ? WHERE id = ?", (stream_type, pk))
        elif action == "check":
            res = discover.check_channel(cfg, conn, row, ytdlp=deps.ytdlp if deps else None)
            if res.error:
                return _redirect("/channels", error=res.error)
            return _redirect("/channels", msg=f"{res.channel}: queued {len(res.queued)} new stream(s).")
        else:
            raise HTTPException(400)
        return _redirect("/channels", msg="Saved.")

    @app.get("/video/{video_id}", response_class=HTMLResponse)
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
        ranks = {m["rank"]: m for m in d["moments"]}
        return render(request, "video.html", **d, chart=chart, heat=heat, ranks=ranks)

    @app.post("/video/{video_id}/{action}")
    def video_action(video_id: str, action: str, stream_type: str = Form("auto"), conn=Depends(get_conn)):
        v = store.get_video(conn, video_id)
        if v is None:
            raise HTTPException(404)
        back = f"/video/{video_id}"
        if action == "retry":
            n = jobs.retry_video(conn, v["id"])
            if not n:
                jobs.enqueue(conn, v["id"], "ingest")
            store.upsert_video(conn, video_id, status="processing", error=None)
            return _redirect(back, msg="Queued again.")
        if action in ("clip", "analyze", "upload"):
            if action == "upload" and not cfg.drive.enabled:
                return _redirect(back, error="Google Drive upload is off (drive.enabled = false in config.toml).")
            jobs.enqueue(conn, v["id"], action)
            return _redirect(back, msg=f"{action.capitalize()} queued.")
        if action == "rescore":
            from chatwatcher.scoring.service import ScoreError, score_video
            if stream_type in discover.STREAM_TYPES:
                store.upsert_video(conn, video_id, stream_type=stream_type)
            try:
                r = score_video(cfg, conn, video_id, force=True)
            except ScoreError as e:
                return _redirect(back, error=str(e))
            if cfg.pipeline.auto_clip:
                jobs.enqueue(conn, v["id"], "clip")
            return _redirect(back, msg=f"Re-scored as {r.stream_type}: {len(r.moments)} moments. Clips queued.")
        raise HTTPException(400)

    @app.get("/moment/{pk}", response_class=HTMLResponse)
    def moment(request: Request, pk: int, conn=Depends(get_conn)):
        d = queries.moment_detail(conn, pk)
        if d is None:
            raise HTTPException(404, "Unknown moment")
        return render(request, "moment.html", **d)

    @app.post("/moment/{pk}/{action}")
    def moment_action(pk: int, action: str, start: str = Form(""), end: str = Form(""),
                      conn=Depends(get_conn)):
        m = conn.execute("SELECT * FROM moments WHERE id = ?", (pk,)).fetchone()
        if m is None:
            raise HTTPException(404)
        back = f"/moment/{pk}"
        if action in ("approve", "reject", "reset"):
            status = {"approve": "approved", "reject": "rejected", "reset": "new"}[action]
            conn.execute("UPDATE moments SET status = ? WHERE id = ?", (status, pk))
            return _redirect(back, msg=f"Marked {status}.")
        if action == "cut":
            v = conn.execute("SELECT duration_s FROM videos WHERE id = ?", (m["video_id"],)).fetchone()
            try:
                s, e = _parse_ts(start), _parse_ts(end)
            except ValueError as err:
                return _redirect(back, error=str(err))
            s, e = max(0.0, s), min(e, v["duration_s"] or e)
            if e - s < 5:
                return _redirect(back, error="The clip must be at least 5 seconds long.")
            conn.execute("UPDATE moments SET start_s = ?, end_s = ? WHERE id = ?", (s, e, pk))
            jobs.enqueue(conn, m["video_id"], "clip", moment_pk=pk)
            return _redirect(back, msg=f"New cut {fmt_ts(s)}–{fmt_ts(e)} queued; Claude re-analyses it after.")
        if action in ("reclip", "reanalyze"):
            jobs.enqueue(conn, m["video_id"], "clip" if action == "reclip" else "analyze", moment_pk=pk)
            return _redirect(back, msg="Queued.")
        raise HTTPException(400)

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request, conn=Depends(get_conn)):
        return render(request, "jobs.html", rows=jobs.list_jobs(conn, include_done=True, limit=200))

    @app.post("/jobs/{pk}/retry")
    def job_retry(pk: int, conn=Depends(get_conn)):
        j = conn.execute("SELECT * FROM jobs WHERE id = ?", (pk,)).fetchone()
        if j is None:
            raise HTTPException(404)
        jobs.enqueue(conn, j["video_id"], j["stage"], moment_pk=j["moment_id"])
        return _redirect("/jobs", msg="Queued again.")

    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request):
        from chatwatcher.doctor import run_checks
        config = cfg.model_dump(mode="json")
        if config["youtube"].get("po_token"):
            config["youtube"]["po_token"] = "•••• (hidden)"
        return render(request, "settings.html", checks=run_checks(cfg), config=config, source=cfg.source)

    @app.get("/api/version")
    def api_version(conn=Depends(get_conn)):
        return JSONResponse({"v": queries.version_stamp(conn), "worker": worker_status()})

    # ── media: only files the database points at ─────────────────────────────

    @app.get("/media/clip/{clip_id}")
    def media_clip(clip_id: int, conn=Depends(get_conn)):
        c = conn.execute("SELECT path FROM clips WHERE id = ?", (clip_id,)).fetchone()
        if not c or not Path(c["path"]).is_file():
            raise HTTPException(404)
        p = Path(c["path"])
        kind = "video/mp4" if p.suffix == ".mp4" else "video/x-matroska"
        return FileResponse(p, media_type=kind)

    @app.get("/media/frame/{clip_id}/{name}")
    def media_frame(clip_id: int, name: str, conn=Depends(get_conn)):
        c = conn.execute("SELECT folder FROM clips WHERE id = ?", (clip_id,)).fetchone()
        if not c or not c["folder"]:
            raise HTTPException(404)
        frames = (Path(c["folder"]) / "frames").resolve()
        p = (frames / name).resolve()
        if p.parent != frames or p.suffix != ".jpg" or not p.is_file():
            raise HTTPException(404)
        return FileResponse(p, media_type="image/jpeg")

    return app


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
