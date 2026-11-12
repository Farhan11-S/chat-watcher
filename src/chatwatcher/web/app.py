"""FastAPI app for the local dashboard. Long work never runs in a request: buttons queue
jobs, and the worker (in this process, or a separate `cw worker`) does them.

This file is setup only. The pages and endpoints are in web/routes/, one module per feature."""

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
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from chatwatcher import db, storage
from chatwatcher.config import Config
from chatwatcher.pipeline import Deps
from chatwatcher.scoring.moments import fmt_ts
from chatwatcher.web import listing
from chatwatcher.web.routes import (Ctx, brand, channels, clips, editor, library, media, queue,
                                    storage as storage_routes, streams, vtubers)
from chatwatcher.web.routes.clips import _parse_ts  # noqa: F401  (tests import it from here)

log = logging.getLogger("chatwatcher.web")
HERE = Path(__file__).parent

# The order routes are matched in. clips goes after editor and brand: its POST
# /moment/{pk}/{action} would otherwise take /moment/{pk}/edit, /render, /intro, /thumb, …
ROUTERS = (streams, channels, editor, brand, clips, storage_routes, vtubers, library, media, queue)


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

    def asset(name: str) -> str:
        """/static/<name>?v=<when it last changed>: a new version is fetched as soon as the
        file changes, instead of the browser reusing its cached copy for a while."""
        try:
            return f"/static/{name}?v={int((HERE / 'static' / name).stat().st_mtime)}"
        except OSError:
            return f"/static/{name}"
    templates.env.globals["asset"] = asset

    def qs(request: Request, **changes: Any) -> str:
        """This page's URL with some query values changed (None or "" drops one): for
        pagers, tabs and sort links, so every other filter stays as it is."""
        params = {k: v for k, v in request.query_params.items() if k not in ("msg", "error") and v != ""}
        for k, v in changes.items():
            k = k.rstrip("_")
            if v is None or v == "":
                params.pop(k, None)
            else:
                params[k] = str(v)
        return request.url.path + ("?" + urlencode(params) if params else "")
    templates.env.globals["qs"] = qs
    templates.env.globals["stream_thumb"] = listing.stream_thumb

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

    def poke() -> None:
        if state["worker"]:
            state["worker"].poke()

    ctx = Ctx(cfg=cfg, deps=deps, tz=tz, render=render, get_conn=get_conn,
              worker_status=worker_status, poke=poke)
    for module in ROUTERS:
        app.include_router(module.router(ctx))
    return app
