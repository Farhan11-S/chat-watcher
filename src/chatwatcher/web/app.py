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

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import BaseModel

from chatwatcher import db, discover, jobs, prefs, storage, store
from chatwatcher.clip.media import MediaError
from chatwatcher.config import Config
from chatwatcher.ingest.ytdlp import extract_video_id
from chatwatcher.paths import vod_files
from chatwatcher.pipeline import Deps
from chatwatcher.scoring.moments import fmt_ts
from chatwatcher.web import charts, queries

log = logging.getLogger("chatwatcher.web")
HERE = Path(__file__).parent
CLAUDE_MODELS = ("sonnet", "opus", "haiku")          # CLI aliases for the latest of each
EFFORTS = ("low", "medium", "high", "xhigh", "max")


class StatusIn(BaseModel):
    status: str


class RedoIn(BaseModel):
    model: str = ""
    effort: str = ""


class EditIn(BaseModel):
    segments: Any = None      # [[start, end, gain_db], …] clip time; checked by edit.service
    source: str | None = None
    effects: Any = None       # [{t, sound, gain, duck, …}] clip time; None = keep the saved ones
    subs: Any = None          # [{t0, t1, text, by}] clip time; None = keep (or keep building them from the words)
    subs_style: Any = None    # {on, size, position}


class RegionsIn(BaseModel):
    regions: Any = None       # [{name, x, y, w, h}] as fractions of the frame


class PolishIn(EditIn):
    model: str = ""
    effort: str = ""


class NamesIn(BaseModel):
    terms: Any = None         # the whole list (replaces it)
    add: Any = None           # or terms to add


class SuggestIn(EditIn):
    intensity: str = "normal"
    model: str = ""
    effort: str = ""


class AskIn(BaseModel):
    question: str
    model: str = ""
    effort: str = ""
    web: bool = False


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
    templates.env.filters["bytes"] = storage.fmt_bytes
    # Stored times are UTC; show them in general.timezone (e.g. "2026-11-08 12:51 WIB").
    from chatwatcher import clock
    tz = clock.zone(cfg)
    templates.env.filters["when"] = lambda v: clock.fmt(v, tz)
    templates.env.filters["day"] = lambda v: clock.fmt(v, tz, date_only=True)
    templates.env.globals["tzname"] = clock.zone_name(tz)

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

    models = list(dict.fromkeys([cfg.claude.model, cfg.claude.redo_model, *CLAUDE_MODELS]))

    def claude_choice(model: str, effort: str) -> dict[str, str]:
        """Model/effort picked on a form; only known values reach the claude command line."""
        out = {}
        if model in models:
            out["model"] = model
        if effort in EFFORTS:
            out["effort"] = effort
        return out

    def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        ctx.setdefault("msg", request.query_params.get("msg"))
        ctx.setdefault("error", request.query_params.get("error"))
        return templates.TemplateResponse(request, name, {"worker": worker_status(), "cfg": cfg,
                                                          "drive_login": drive_login(),
                                                          "claude_models": models, "efforts": EFFORTS,
                                                          **ctx})

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
        kept = sum(1 for m in d["moments"] if m["status"] == "approved" and m["clip_id"])
        kept_all = sum(1 for m in d["moments"] if m["status"] == "approved")
        return render(request, "video.html", **d, chart=chart, heat=heat, ranks=ranks, kept=kept,
                      kept_all=kept_all, review=_review_items(d["moments"]),
                      auto_upload=prefs.auto_upload(cfg, conn))

    @app.post("/video/{video_id}/{action}")
    def video_action(video_id: str, action: str, stream_type: str = Form("auto"), model: str = Form(""),
                     effort: str = Form(""), redo: str = Form(""), kept_only: str = Form(""),
                     conn=Depends(get_conn)):
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
        if action == "clip":
            n = conn.execute("SELECT COUNT(*) FROM moments WHERE video_id = ? AND status = 'approved'",
                             (v["id"],)).fetchone()[0]
            if not n:
                return _redirect(back, error="Mark the moments you want with ✓ Keep first.")
            jobs.enqueue(conn, v["id"], "clip", params={"kept_only": True})
            then = " Claude analyses them after." if cfg.pipeline.auto_analyze and cfg.claude.enabled else ""
            return _redirect(back, msg=f"Downloading {n} kept moment(s).{then}")
        if action == "analyze":
            params = claude_choice(model, effort) | ({"redo": True} if redo else {})
            jobs.enqueue(conn, v["id"], "analyze", params=params)
            what = "Re-analysis of every clip" if redo else "Analysis"
            return _redirect(back, msg=f"{what} queued ({params.get('model', cfg.claude.model)}, "
                                       f"{params.get('effort', cfg.claude.effort)} effort).")
        if action == "upload":
            if not cfg.drive.enabled:
                return _redirect(back, error="Google Drive isn't set up (drive.enabled = false in config.toml).")
            if kept_only:
                from chatwatcher.upload.drive import clip_folders
                if not clip_folders(conn, video_id, kept_only=True):
                    return _redirect(back, error="No downloaded clips are marked ✓ Keep yet.")
                jobs.enqueue(conn, v["id"], "upload", params={"kept_only": True})
                return _redirect(back, msg="Upload of the kept clips queued.")
            jobs.enqueue(conn, v["id"], "upload")
            return _redirect(back, msg="Upload queued.")
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
            then = " Clips queued." if cfg.pipeline.auto_clip else " Review them, then press Clip kept."
            return _redirect(back, msg=f"Re-scored as {r.stream_type}: {len(r.moments)} moments.{then}")
        raise HTTPException(400)

    @app.get("/moment/{pk}", response_class=HTMLResponse)
    def moment(request: Request, pk: int, conn=Depends(get_conn)):
        d = queries.moment_detail(conn, pk)
        if d is None:
            raise HTTPException(404, "Unknown moment")
        from chatwatcher.analyze.ask import thread
        return render(request, "moment.html", **d, chat=thread(conn, pk))

    @app.post("/moment/{pk}/status")
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

    @app.post("/moment/{pk}/analyze")
    def moment_redo(pk: int, body: RedoIn, conn=Depends(get_conn)):
        """Analyse one moment again (e.g. with Opus) from the stream page's review panel."""
        m = conn.execute("SELECT video_id FROM moments WHERE id = ?", (pk,)).fetchone()
        if m is None:
            raise HTTPException(404)
        if not cfg.claude.enabled:
            return JSONResponse({"ok": False, "error": "claude.enabled is false in config.toml."}, status_code=400)
        params = claude_choice(body.model, body.effort)
        jobs.enqueue(conn, m["video_id"], "analyze", moment_pk=pk, params=params)
        ahead = conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('pending', 'running') "
                             "AND run_after <= datetime('now') AND NOT (moment_id = ? AND stage = 'analyze')",
                             (pk,)).fetchone()[0]
        return JSONResponse({"ok": True, "model": params.get("model", cfg.claude.model),
                             "effort": params.get("effort", cfg.claude.effort), "ahead": ahead})

    @app.get("/api/moment/{pk}")
    def api_moment(pk: int, conn=Depends(get_conn)):
        """The moment's latest analysis and its pending re-analysis, for the review panel."""
        m = conn.execute("SELECT * FROM moments WHERE id = ?", (pk,)).fetchone()
        if m is None:
            raise HTTPException(404)
        d = dict(m) | queries._j(m["features_json"], {})
        d["analysis"] = queries.latest_analysis(conn, pk)
        d["clip_id"] = None
        job = conn.execute("SELECT status, error, message FROM jobs WHERE moment_id = ? AND stage = 'analyze'",
                           (pk,)).fetchone()
        return JSONResponse({"moment": _review_items([d])[0], "job": dict(job) if job else None})

    @app.post("/moment/{pk}/ask")
    def moment_ask(pk: int, body: AskIn, conn=Depends(get_conn)):
        """Answer a follow-up question about the clip (takes 5-60 s; the page waits)."""
        from chatwatcher.analyze.ask import AskError, ask
        choice = claude_choice(body.model, body.effort)
        try:
            a = ask(cfg, conn, pk, body.question, runner=deps.claude if deps else None,
                    web_search=body.web, **choice)
        except AskError as e:
            status = 404 if e.kind == "missing" else 400 if e.kind in ("error", "not_clipped", "disabled") else 503
            return JSONResponse({"ok": False, "error": str(e)}, status_code=status)
        return JSONResponse({"ok": True, "answer": a.text, "model": a.model,
                             "tokens": a.usage.get("input", 0) + a.usage.get("cache_read", 0)
                             + a.usage.get("cache_write", 0) + a.usage.get("output", 0)})

    # ── editor ───────────────────────────────────────────────────────────────

    def edit_error(e: Exception) -> JSONResponse:
        from chatwatcher.edit.service import EditError
        status = 500
        if isinstance(e, EditError):
            status = 404 if e.kind == "not_clipped" else 400
        return JSONResponse({"ok": False, "error": str(e)}, status_code=status)

    def with_worker(state: dict[str, Any]) -> dict[str, Any]:
        return state | {"worker": worker_status()}

    @app.get("/api/moment/{pk}/edit")
    def api_edit(pk: int, conn=Depends(get_conn)):
        """Waveform, pauses, Claude's cuts, your saved cut and the render's state."""
        from chatwatcher.edit.service import EditError, load
        try:
            d = load(cfg, conn, pk)
        except (EditError, MediaError) as e:
            return edit_error(e)
        d["render"] = with_worker(d["render"])
        return JSONResponse({"ok": True, **d})

    @app.post("/moment/{pk}/edit")
    def edit_save(pk: int, body: EditIn, conn=Depends(get_conn)):
        from chatwatcher.edit.service import EditError, render_state, save
        try:
            segs = save(cfg, conn, pk, body.segments, body.source, body.effects, body.subs, body.subs_style)
        except (EditError, MediaError) as e:
            return edit_error(e)
        return JSONResponse({"ok": True, "segments": segs, "render": with_worker(render_state(conn, pk, cfg=cfg))})

    @app.post("/moment/{pk}/render")
    def edit_render(pk: int, body: EditIn, conn=Depends(get_conn)):
        """Save the cut and queue the render (it goes ahead of other queued work)."""
        from chatwatcher.edit.service import EditError, render_state, save
        m = conn.execute("SELECT video_id FROM moments WHERE id = ?", (pk,)).fetchone()
        if m is None:
            raise HTTPException(404)
        try:
            save(cfg, conn, pk, body.segments, body.source, body.effects, body.subs, body.subs_style)
        except (EditError, MediaError) as e:
            return edit_error(e)
        if jobs.enqueue(conn, m["video_id"], "render", moment_pk=pk) is None:
            return JSONResponse({"ok": False, "error": "A render of this clip is running. Press Render again "
                                                       "when it's done to include your latest changes."},
                                status_code=409)
        if state["worker"]:
            state["worker"].poke()
        return JSONResponse({"ok": True, "render": with_worker(render_state(conn, pk, cfg=cfg))})

    @app.post("/moment/{pk}/effects/suggest")
    def edit_suggest(pk: int, body: SuggestIn, conn=Depends(get_conn)):
        """Save the cut, then have Claude place sounds and camera effects on it (10-60 s; the page waits)."""
        from chatwatcher.edit.service import EditError, render_state, save
        from chatwatcher.edit.suggest import SuggestError, suggest
        try:
            save(cfg, conn, pk, body.segments, body.source, body.effects, body.subs, body.subs_style)
            r = suggest(cfg, conn, pk, intensity=body.intensity, runner=deps.claude if deps else None,
                        **claude_choice(body.model, body.effort))
        except (EditError, MediaError) as e:
            return edit_error(e)
        except SuggestError as e:
            return JSONResponse({"ok": False, "error": str(e), "kind": e.kind},
                                status_code=503 if e.kind in ("usage_limit", "timeout") else 400)
        return JSONResponse({"ok": True, "effects": r.effects, "added": r.added, "dropped": r.dropped,
                             "added_sounds": r.added_sounds, "added_visual": r.added_visual,
                             "note": r.note, "model": r.model, "render": with_worker(render_state(conn, pk, cfg=cfg))})

    @app.post("/moment/{pk}/subs/polish")
    def subs_polish(pk: int, body: PolishIn, conn=Depends(get_conn)):
        """Save the edit, then have Claude fix (and translate) its subtitle lines (10-60 s; the page waits)."""
        from chatwatcher.edit.service import EditError, render_state, save
        from chatwatcher.subs.polish import PolishError, polish
        try:
            save(cfg, conn, pk, body.segments, body.source, body.effects, body.subs, body.subs_style)
            r = polish(cfg, conn, pk, runner=deps.claude if deps else None, **claude_choice(body.model, body.effort))
        except (EditError, MediaError) as e:
            return edit_error(e)
        except PolishError as e:
            return JSONResponse({"ok": False, "error": str(e), "kind": e.kind},
                                status_code=503 if e.kind in ("usage_limit", "timeout") else 400)
        return JSONResponse({"ok": True, "subs": r.subs, "changed": r.changed, "joined": r.joined, "dropped": r.dropped,
                             "names": r.names, "names_added": r.names_added, "note": r.note, "model": r.model,
                             "render": with_worker(render_state(conn, pk, cfg=cfg))})

    @app.post("/moment/{pk}/names")
    def subs_names(pk: int, body: NamesIn, conn=Depends(get_conn)):
        """The names Claude must spell right, for every stream of this moment's channel."""
        from chatwatcher.edit import regions
        from chatwatcher.subs import names
        try:
            scope, _label = regions.scope_for(conn, pk)
        except regions.RegionError:
            raise HTTPException(404)
        if body.terms is not None:
            return JSONResponse({"ok": True, "names": names.put(conn, scope, body.terms), "added": []})
        have, added = names.add(conn, scope, body.add or [])
        return JSONResponse({"ok": True, "names": have, "added": added})

    @app.post("/moment/{pk}/transcribe")
    def edit_transcribe(pk: int, conn=Depends(get_conn)):
        """Queue Whisper on this clip (again). It goes behind the job that's running."""
        from chatwatcher.edit.service import EditError, clip_ref, transcript_state
        from chatwatcher.subs.transcribe import available
        try:
            clip = clip_ref(conn, pk)
        except EditError as e:
            return edit_error(e)
        if not cfg.transcribe.enabled or not (available() or (deps and deps.whisper)):
            return JSONResponse({"ok": False, "error": "Whisper isn't installed: run `pip install faster-whisper` "
                                                       "in the app's environment, then restart."}, status_code=400)
        m = conn.execute("SELECT video_id FROM moments WHERE id = ?", (pk,)).fetchone()
        jobs.enqueue(conn, m["video_id"], "transcribe", moment_pk=pk)
        if state["worker"]:
            state["worker"].poke()
        return JSONResponse({"ok": True, "transcript": transcript_state(cfg, conn, pk, clip)})

    @app.get("/api/moment/{pk}/transcript")
    def api_transcript(pk: int, conn=Depends(get_conn)):
        from chatwatcher.edit.service import EditError, clip_ref, subtitles, transcript_lines, transcript_state
        try:
            clip = clip_ref(conn, pk)
        except EditError as e:
            return edit_error(e)
        return JSONResponse({"ok": True, "transcript": transcript_state(cfg, conn, pk, clip),
                             "lines": transcript_lines(clip.folder), **subtitles(cfg, conn, pk, clip)})

    @app.post("/moment/{pk}/regions")
    def edit_regions(pk: int, body: RegionsIn, conn=Depends(get_conn)):
        """Replace the boxes zooms aim at, for every stream of this moment's channel."""
        from chatwatcher.edit import regions
        from chatwatcher.edit.service import render_state
        try:
            scope, label = regions.scope_for(conn, pk)
            boxes = regions.replace(conn, scope, body.regions)
        except regions.RegionError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "regions": boxes, "scope": label,
                             "render": with_worker(render_state(conn, pk, cfg=cfg))})

    @app.get("/api/moment/{pk}/render")
    def api_render(pk: int, conn=Depends(get_conn)):
        from chatwatcher.edit.service import render_state
        return JSONResponse({"ok": True, "render": with_worker(render_state(conn, pk, cfg=cfg))})

    @app.post("/moment/{pk}/{action}")
    def moment_action(pk: int, action: str, start: str = Form(""), end: str = Form(""),
                      model: str = Form(""), effort: str = Form(""), conn=Depends(get_conn)):
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
        if action == "reclip":
            jobs.enqueue(conn, m["video_id"], "clip", moment_pk=pk)
            return _redirect(back, msg="Queued.")
        if action == "reanalyze":
            params = claude_choice(model, effort)
            jobs.enqueue(conn, m["video_id"], "analyze", moment_pk=pk, params=params)
            return _redirect(back, msg=f"Queued for {params.get('model', cfg.claude.model)} "
                                       f"({params.get('effort', cfg.claude.effort)} effort).")
        if action == "upload":
            if not cfg.drive.enabled:
                return _redirect(back, error="Google Drive isn't set up (drive.enabled = false in config.toml).")
            jobs.enqueue(conn, m["video_id"], "upload", moment_pk=pk)
            return _redirect(back, msg="Upload of this clip queued.")
        if action == "clear-chat":
            conn.execute("DELETE FROM moment_chats WHERE moment_id = ?", (pk,))
            return _redirect(back + "#ask", msg="Conversation cleared.")
        raise HTTPException(400)

    # ── storage ──────────────────────────────────────────────────────────────

    drive_cache: dict[str, Any] = {"usage": None}
    DRIVE_TTL_S = 15 * 60

    def fresh_drive() -> storage.DriveUsage | None:
        u = drive_cache["usage"]
        return u if u is not None and time.time() - u.checked_at < DRIVE_TTL_S else None

    @app.get("/storage", response_class=HTMLResponse)
    def storage_page(request: Request, conn=Depends(get_conn)):
        local = storage.local_usage(cfg, conn)
        drive = fresh_drive() if cfg.drive.enabled else None
        storage.merge(local, drive)
        channels = sorted(local.channels, key=lambda b: -max(b.local, b.drive or 0))
        streams = sorted(local.streams, key=lambda b: (-b.local, -(b.drive or 0)))
        scale = max([1, *(b.local for b in channels), *((b.drive or 0) for b in channels)])
        disk_parts = _segments([("chat-watcher", local.ours, "s1"), ("other files", local.other, "other"),
                                ("free", local.disk_free, "free")], local.disk_total)
        drive_parts = []
        if drive and not drive.error and drive.total:
            drive_parts = _segments([("chat-watcher", drive.ours, "s2"),
                                     ("other files", drive.your_other + (drive.other or 0), "other"),
                                     ("free", drive.free, "free")], drive.total)
        return render(request, "storage.html", local=local, drive=drive, channels=channels, streams=streams,
                      scale=scale, disk_parts=disk_parts, drive_parts=drive_parts, now=time.time(),
                      drive_pending=cfg.drive.enabled and drive is None)

    @app.post("/storage/delete")
    def storage_delete(key: list[str] = Form([]), mode: str = Form(""), conn=Depends(get_conn)):
        if mode not in ("videos", "all"):
            raise HTTPException(400)
        if not key:
            return _redirect("/storage", error="Tick at least one stream first.")
        r = storage.delete_streams(cfg, conn, key, videos_only=mode == "videos")
        what = "clip videos of" if mode == "videos" else ""
        msg = f"Freed {storage.fmt_bytes(r.freed)}: deleted the {what} {r.streams} stream(s)".replace("  ", " ")
        msg += " from this computer." + (" Google Drive wasn't touched." if cfg.drive.enabled else "")
        if r.skipped:
            msg += " Skipped: " + "; ".join(r.skipped)
        return _redirect("/storage", msg=msg) if r.streams else _redirect("/storage", error=msg)

    @app.post("/storage/drive")
    def storage_drive_refresh():
        if not cfg.drive.enabled:
            return _redirect("/storage", error="Google Drive isn't set up (drive.enabled = false in config.toml).")
        drive_cache["usage"] = storage.drive_usage(cfg, rclone=deps.rclone if deps else None)
        return _redirect("/storage")

    # ── sound library ────────────────────────────────────────────────────────

    @app.get("/sounds", response_class=HTMLResponse)
    def sounds_page(request: Request, conn=Depends(get_conn)):
        from chatwatcher.edit import sounds
        rows = sounds.list_sounds(conn)
        used: dict[str, int] = {}
        for r in conn.execute("SELECT effects_json FROM edits WHERE effects_json != '[]'"):
            for fx in json.loads(r["effects_json"]):
                used[fx.get("sound", "")] = used.get(fx.get("sound", ""), 0) + 1
        return render(request, "sounds.html", sounds=rows, used=used, pack=sounds.starter_status(conn))

    @app.post("/sounds/upload")
    def sounds_upload(files: list[UploadFile] = File(...), description: str = Form(""), tags: str = Form(""),
                      source: str = Form(""), conn=Depends(get_conn)):
        from chatwatcher.edit import sounds
        tmp_dir = sounds.sounds_dir(cfg) / ".upload"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        added, failed, skipped = [], [], []
        for f in files:
            if not f.filename:
                continue
            suffix = Path(f.filename).suffix[:10]
            tmp = tmp_dir / f"in_{time.time_ns()}{suffix}"
            try:
                size = 0
                with open(tmp, "wb") as out:
                    while chunk := f.file.read(1 << 20):
                        size += len(chunk)
                        if size > sounds.MAX_UPLOAD_BYTES:
                            raise sounds.SoundError("bigger than 25 MB")
                        out.write(chunk)
                known = sounds.starter_for(f.filename, size)    # a starter-pack file: details filled in
                if known and sounds.already_imported(conn, known):
                    skipped.append(known["name"])
                    continue
                k = known or {}
                original = known["file"] + suffix.lower() if known else f.filename
                s = sounds.import_sound(cfg, conn, tmp, original=original, name=k.get("name", ""),
                                        description=description or k.get("description", ""),
                                        tags=tags or k.get("tags", ""), source=source or k.get("source", ""))
                added.append(s["id"])
            except (sounds.SoundError, MediaError) as e:
                failed.append(f"{f.filename}: {e}")
            finally:
                tmp.unlink(missing_ok=True)
        if not added and not failed and not skipped:
            return _redirect("/sounds", error="Pick one or more sound files first.")
        msg = f"Added {len(added)} sound(s): {', '.join(added)}." if added else ""
        if skipped:
            msg += f" Already in your library: {', '.join(skipped)}."
        if added and not description and conn.execute(
                "SELECT 1 FROM sounds WHERE description = '' AND id IN (%s)" % ",".join("?" * len(added)),
                added).fetchone():
            msg += " Give each one a description, so Claude knows when to use it."
        if failed:
            return _redirect("/sounds", error=(msg + "\nCouldn't add: " + "; ".join(failed)).strip())
        return _redirect("/sounds", msg=msg.strip())

    @app.post("/sounds/{sound_id}/save")
    def sounds_save(sound_id: str, name: str = Form(""), description: str = Form(""), tags: str = Form(""),
                    source: str = Form(""), gain_db: float = Form(0.0), conn=Depends(get_conn)):
        from chatwatcher.edit import sounds
        try:
            sounds.update_sound(conn, sound_id, name=name, description=description, tags=tags,
                                source=source, gain_db=gain_db)
        except sounds.SoundError as e:
            return _redirect("/sounds", error=str(e))
        return _redirect(f"/sounds#s-{sound_id}", msg=f"Saved {sound_id}.")

    @app.post("/sounds/{sound_id}/delete")
    def sounds_delete(sound_id: str, conn=Depends(get_conn)):
        from chatwatcher.edit import sounds
        sounds.delete_sound(cfg, conn, sound_id)
        return _redirect("/sounds", msg=f"Deleted {sound_id}.")

    @app.get("/media/sound/{sound_id}")
    def media_sound(sound_id: str, conn=Depends(get_conn)):
        from chatwatcher.edit import sounds
        s = sounds.get_sound(conn, sound_id) if sounds.valid_id(sound_id) else None
        p = sounds.sound_path(cfg, s) if s else None
        if not p or not p.is_file() or p.parent != sounds.sounds_dir(cfg):
            raise HTTPException(404)
        return FileResponse(p, media_type="audio/flac")

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
    def settings(request: Request, conn=Depends(get_conn)):
        from chatwatcher.doctor import run_checks
        config = cfg.model_dump(mode="json")
        if config["youtube"].get("po_token"):
            config["youtube"]["po_token"] = "•••• (hidden)"
        return render(request, "settings.html", checks=run_checks(cfg), config=config, source=cfg.source,
                      auto_upload=prefs.auto_upload(cfg, conn))

    @app.post("/settings/auto-upload")
    def set_auto_upload(on: str = Form(""), conn=Depends(get_conn)):
        prefs.put(conn, prefs.AUTO_UPLOAD, bool(on))
        return _redirect("/settings", msg="Streams will be uploaded to Drive automatically." if on else
                         "Auto-upload is off. Use the Upload buttons on a stream or clip page.")

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

    @app.get("/media/edit/{moment_id}")
    def media_edit(moment_id: int, dl: int = 0, conn=Depends(get_conn)):
        from chatwatcher.edit.service import EDIT_FILE
        c = conn.execute(
            """SELECT c.folder, v.video_id FROM clips c JOIN moments m ON m.id = c.moment_id
               JOIN videos v ON v.id = m.video_id WHERE c.moment_id = ?""", (moment_id,)).fetchone()
        if not c or not c["folder"] or not (Path(c["folder"]) / EDIT_FILE).is_file():
            raise HTTPException(404)
        p = Path(c["folder"]) / EDIT_FILE
        name = f"{c['video_id']}_{p.parent.name}_edit.mp4" if dl else None
        return FileResponse(p, media_type="video/mp4", filename=name)

    @app.get("/media/edit-srt/{moment_id}")
    def media_edit_srt(moment_id: int, conn=Depends(get_conn)):
        from chatwatcher.edit.service import SRT_FILE
        c = conn.execute(
            """SELECT c.folder, v.video_id FROM clips c JOIN moments m ON m.id = c.moment_id
               JOIN videos v ON v.id = m.video_id WHERE c.moment_id = ?""", (moment_id,)).fetchone()
        if not c or not c["folder"] or not (Path(c["folder"]) / SRT_FILE).is_file():
            raise HTTPException(404)
        p = Path(c["folder"]) / SRT_FILE
        return FileResponse(p, media_type="application/x-subrip", filename=f"{c['video_id']}_{p.parent.name}_edit.srt")

    @app.get("/media/preview/{moment_id}/{name}")
    def media_preview_frame(moment_id: int, name: str, conn=Depends(get_conn)):
        p_row = conn.execute("SELECT folder FROM previews WHERE moment_id = ?", (moment_id,)).fetchone()
        if not p_row:
            raise HTTPException(404)
        frames = (Path(p_row["folder"]) / "frames").resolve()
        p = (frames / name).resolve()
        if p.parent != frames or p.suffix != ".jpg" or not p.is_file():
            raise HTTPException(404)
        return FileResponse(p, media_type="image/jpeg")

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


def _segments(parts: list[tuple[str, int | None, str]], total: int) -> list[dict[str, Any]]:
    """Blocks of a segmented bar; zero-sized parts draw nothing."""
    out = []
    for name, value, cls in parts:
        if value and value > 0 and total:
            share = value / total
            out.append({"name": name, "value": value, "cls": cls, "pct": share * 100,
                        "label": f"{share * 100:.0f}%" if share >= 0.12 else ""})
    return out


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
