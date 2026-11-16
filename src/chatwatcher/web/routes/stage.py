"""The 3D stage (stage/): a moment's 3D page and its API, its files, and the MMD model
library (/models). Listed in ROUTERS before clips: its /moment/{pk}/… pages would otherwise
meet clips' catch-all."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from chatwatcher import jobs
from chatwatcher.web.routes import Ctx, redirect


class SetupIn(BaseModel):
    model: str | None = None
    set: str | None = None
    shape: str | None = None
    recut: bool = False


class SceneIn(BaseModel):
    scene: Any = None


class DirectIn(BaseModel):
    message: str = ""
    new: bool = False
    start_over: bool = False


class ProfileIn(BaseModel):
    changes: Any = None


def router(ctx: Ctx) -> APIRouter:
    r = APIRouter()
    cfg, deps, get_conn = ctx.cfg, ctx.deps, ctx.get_conn

    def fail(e: Exception, status: int = 400) -> JSONResponse:
        kind = getattr(e, "kind", "error")
        if kind == "not_found":
            status = 404
        elif kind in ("usage_limit", "timeout"):
            status = 503
        return JSONResponse({"ok": False, "error": str(e), "kind": kind}, status_code=status)

    def state(conn: Any, pk: int) -> dict[str, Any]:
        from chatwatcher.stage.store import page_state
        s = page_state(cfg, conn, pk)
        s["render"] = ctx.with_worker(s["render"])
        return s

    def errors() -> tuple[type[Exception], ...]:
        from chatwatcher.edit.clipref import EditError
        from chatwatcher.stage.models import ModelError
        from chatwatcher.stage.run import StageRunError
        from chatwatcher.stage.store import StageError
        from chatwatcher.stage.track import TrackError
        return (EditError, ModelError, StageError, StageRunError, TrackError)

    @r.get("/moment/{pk}/3d", response_class=HTMLResponse)
    def stage_page(request: Request, pk: int, conn=Depends(get_conn)):
        m = conn.execute("""SELECT m.*, v.video_id AS yt_id, v.title AS video_title FROM moments m
                            JOIN videos v ON v.id = m.video_id WHERE m.id = ?""", (pk,)).fetchone()
        if m is None:
            raise HTTPException(404)
        clipped = conn.execute("SELECT 1 FROM clips WHERE moment_id = ?", (pk,)).fetchone() is not None
        return ctx.render(request, "stage.html", moment=dict(m), clipped=clipped)

    @r.get("/api/moment/{pk}/stage")
    def api_stage(pk: int, conn=Depends(get_conn)):
        try:
            return JSONResponse({"ok": True, **state(conn, pk)})
        except errors() as e:
            return fail(e)

    @r.post("/moment/{pk}/stage/setup")
    def stage_setup(pk: int, body: SetupIn, conn=Depends(get_conn)):
        """Choose the model, set and shape; the voice track is made now if the cut changed
        (10-40 s, the page waits)."""
        from chatwatcher.stage import store, track
        try:
            st = store.setup(cfg, conn, pk, model=body.model, set_name=body.set, shape=body.shape, recut=body.recut)
            track.ensure(cfg, conn, pk, st)
            return JSONResponse({"ok": True, **state(conn, pk)})
        except errors() as e:
            return fail(e)

    @r.post("/moment/{pk}/stage/scene")
    def stage_scene(pk: int, body: SceneIn, conn=Depends(get_conn)):
        from chatwatcher.stage import store
        try:
            scene, warnings = store.save_scene(conn, pk, body.scene)
        except errors() as e:
            return fail(e)
        return JSONResponse({"ok": True, "scene": scene, "warnings": warnings})

    @r.post("/moment/{pk}/stage/direct")
    def stage_direct(pk: int, body: DirectIn, conn=Depends(get_conn)):
        """Claude writes the scene (new) or answers your message about it (30-90 s, the page waits)."""
        from chatwatcher.stage.run import direct
        try:
            t = direct(cfg, conn, pk, body.message, new=body.new, start_over=body.start_over,
                       runner=deps.claude if deps else None)
        except errors() as e:
            return fail(e)
        return JSONResponse({"ok": True, "applied": t.scene is not None, "scene": t.scene, "reply": t.reply,
                             "warnings": t.warnings, "messages": t.messages, "fresh": t.fresh})

    @r.post("/moment/{pk}/stage/render")
    def stage_render(pk: int, conn=Depends(get_conn)):
        from chatwatcher.stage import store
        m = conn.execute("SELECT video_id FROM moments WHERE id = ?", (pk,)).fetchone()
        st = store.get(conn, pk)
        if m is None or st is None:
            return JSONResponse({"ok": False, "error": "Set the stage up first."}, status_code=400)
        if jobs.enqueue(conn, m["video_id"], "3d", moment_pk=pk) is None:
            return JSONResponse({"ok": False, "error": "An export of this stage is running; export again when it's done."},
                                status_code=409)
        conn.execute("UPDATE stages SET render_progress = 0 WHERE moment_id = ?", (pk,))
        ctx.poke()
        return JSONResponse({"ok": True, "render": ctx.with_worker(store.render_state(conn, pk))})

    @r.get("/api/moment/{pk}/stage/render")
    def api_stage_render(pk: int, conn=Depends(get_conn)):
        from chatwatcher.stage import store
        return JSONResponse(ctx.with_worker(store.render_state(conn, pk)))

    # ── files ────────────────────────────────────────────────────────────────

    @r.get("/media/stage/{pk}/screen/{n}")
    def media_stage_screen(pk: int, n: int, conn=Depends(get_conn)):
        """A still of the room's screen footage while an export runs (stage.export)."""
        from chatwatcher.stage.export import SCREEN_DIR
        c = conn.execute("SELECT folder FROM clips WHERE moment_id = ?", (pk,)).fetchone()
        p = Path(c["folder"]) / SCREEN_DIR / f"{n:06d}.jpg" if c and c["folder"] else None
        if p is None or not p.is_file():
            raise HTTPException(404)
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @r.get("/media/stage/{pk}/{what}")
    def media_stage(pk: int, what: str, dl: int = 0, conn=Depends(get_conn)):
        from chatwatcher.stage.store import STAGE_FILE
        from chatwatcher.stage.track import AUDIO_FILE, TRACK_FILE
        files = {"track": (TRACK_FILE, "application/json"), "audio": (AUDIO_FILE, "audio/mp4"),
                 "video": (STAGE_FILE, "video/mp4")}
        if what not in files:
            raise HTTPException(404)
        c = conn.execute("""SELECT c.folder, v.video_id FROM clips c JOIN moments m ON m.id = c.moment_id
                            JOIN videos v ON v.id = m.video_id WHERE c.moment_id = ?""", (pk,)).fetchone()
        name, kind = files[what]
        if not c or not c["folder"] or not (Path(c["folder"]) / name).is_file():
            raise HTTPException(404)
        p = Path(c["folder"]) / name
        return FileResponse(p, media_type=kind, filename=f"{c['video_id']}_{p.parent.name}_{name}" if dl else None,
                            headers={"Cache-Control": "no-cache"})

    @r.get("/media/mmd/{slug}/{path:path}")
    def media_mmd(slug: str, path: str):
        """A model's files, only to this dashboard (COVER's terms: no redistribution)."""
        from chatwatcher.stage.models import file
        p = file(cfg, slug, path)
        if p is None:
            raise HTTPException(404)
        return FileResponse(p, headers={"Cache-Control": "private, max-age=86400"})

    # ── the model library ─────────────────────────────────────────────────────

    @r.get("/models", response_class=HTMLResponse)
    def models_page(request: Request, conn=Depends(get_conn)):
        from chatwatcher.stage.models import list_models, root
        guess = Path(cfg.paths.data_dir).resolve().parent / "proto" / "mmd" / "model"
        return ctx.render(request, "models.html", models=list_models(cfg), folder=str(root(cfg)),
                          default_folder=str(guess if guess.is_dir() else Path.home() / "Downloads"))

    @r.post("/models/import")
    async def models_import(file: UploadFile = File(...), conn=Depends(get_conn)):
        """A model zip from mmd.hololive.tv, unpacked into <data_dir>/mmd."""
        from chatwatcher.stage.models import ModelError, import_zip
        tmp = Path(tempfile.mkdtemp(prefix="mmd-", dir=cfg.paths.data_dir))
        try:
            dest = tmp / Path(file.filename or "model.zip").name
            with open(dest, "wb") as out:
                shutil.copyfileobj(file.file, out, 1 << 20)
            p = import_zip(cfg, dest, conn=conn)
        except ModelError as e:
            return redirect("/models", error=str(e))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return redirect(f"/models/{p['slug']}", msg=f"Imported {p['name']}. Check how she sits and looks below.")

    @r.post("/models/import-folder")
    def models_import_folder(folder: str = Form(""), conn=Depends(get_conn)):
        """Every new zip in a folder on this computer (zips over 100 MB can't come through the tunnel)."""
        from chatwatcher.stage.models import ModelError, import_folder
        try:
            done, problems = import_folder(cfg, Path(folder.strip() or "."), conn=conn)
        except ModelError as e:
            return redirect("/models", error=str(e))
        msg = f"Imported {len(done)} model(s)" + (f": {', '.join(done)}" if done else " (no new zips there)") + "."
        if problems:
            return redirect("/models", error=msg + " Problems: " + "; ".join(problems))
        return redirect("/models", msg=msg)

    @r.get("/models/{slug}", response_class=HTMLResponse)
    def model_page(request: Request, slug: str):
        from chatwatcher.stage import script
        from chatwatcher.stage.models import ModelError, load
        from urllib.parse import quote
        try:
            p = load(cfg, slug)
        except ModelError:
            raise HTTPException(404)
        url = f"/media/mmd/{slug}/" + "/".join(quote(x) for x in p["pmx"].split("/"))
        return ctx.render(request, "model.html", profile=p, model_url=url, vocab=script.vocab())

    @r.post("/models/{slug}/profile")
    def model_profile(slug: str, body: ProfileIn):
        from chatwatcher.stage.models import ModelError, save
        try:
            p = save(cfg, slug, body.changes or {})
        except ModelError as e:
            return fail(e)
        return JSONResponse({"ok": True, "profile": p})

    return r
