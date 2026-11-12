"""Media files: only files the database points at (clips, edits, subtitles, intros,
thumbnails, frames, library sounds and overlays)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from chatwatcher import db
from chatwatcher.web.routes import Ctx, kind_ok


def router(ctx: Ctx) -> APIRouter:
    r = APIRouter()
    cfg, get_conn = ctx.cfg, ctx.get_conn

    @r.get("/media/clip/{clip_id}")
    def media_clip(clip_id: int, edit: int = 0, conn=Depends(get_conn)):
        """edit=1: the editor's fast-seeking copy when there is one (edit.proxy)."""
        c = conn.execute("SELECT path, folder FROM clips WHERE id = ?", (clip_id,)).fetchone()
        if not c or not Path(c["path"]).is_file():
            raise HTTPException(404)
        p = Path(c["path"])
        if edit:
            from chatwatcher.edit import proxy
            p = proxy.fresh(Path(c["folder"] or p.parent), p) or p
        kind = "video/mp4" if p.suffix == ".mp4" else "video/x-matroska"
        return FileResponse(p, media_type=kind)

    @r.get("/media/edit/{moment_id}")
    def media_edit(moment_id: int, dl: int = 0, kind: str = "long", conn=Depends(get_conn)):
        from chatwatcher.edit.service import EDIT_FILE, SHORT_FILE
        file = SHORT_FILE if kind_ok(kind) == "short" else EDIT_FILE
        c = conn.execute(
            """SELECT c.folder, v.video_id FROM clips c JOIN moments m ON m.id = c.moment_id
               JOIN videos v ON v.id = m.video_id WHERE c.moment_id = ?""", (moment_id,)).fetchone()
        if not c or not c["folder"] or not (Path(c["folder"]) / file).is_file():
            raise HTTPException(404)
        p = Path(c["folder"]) / file
        name = f"{c['video_id']}_{p.parent.name}_{p.name}" if dl else None
        return FileResponse(p, media_type="video/mp4", filename=name)

    @r.get("/media/edit-srt/{moment_id}")
    def media_edit_srt(moment_id: int, kind: str = "long", conn=Depends(get_conn)):
        from chatwatcher.edit.service import SHORT_SRT, SRT_FILE
        file = SHORT_SRT if kind_ok(kind) == "short" else SRT_FILE
        c = conn.execute(
            """SELECT c.folder, v.video_id FROM clips c JOIN moments m ON m.id = c.moment_id
               JOIN videos v ON v.id = m.video_id WHERE c.moment_id = ?""", (moment_id,)).fetchone()
        if not c or not c["folder"] or not (Path(c["folder"]) / file).is_file():
            raise HTTPException(404)
        p = Path(c["folder"]) / file
        return FileResponse(p, media_type="application/x-subrip", filename=f"{c['video_id']}_{p.parent.name}_{p.name}")

    def _clip_folder(moment_id: int) -> tuple[Path, str] | None:
        conn = db.connect(cfg.paths.db_path, check_same_thread=False)
        try:
            c = conn.execute(
                """SELECT c.folder, v.video_id FROM clips c JOIN moments m ON m.id = c.moment_id
                   JOIN videos v ON v.id = m.video_id WHERE c.moment_id = ?""", (moment_id,)).fetchone()
        finally:
            conn.close()
        return (Path(c["folder"]), c["video_id"]) if c and c["folder"] else None

    @r.get("/media/intro/{moment_id}")
    def media_intro(moment_id: int, dl: int = 0):
        from chatwatcher.brand.service import INTRO_FILE
        found = _clip_folder(moment_id)
        if not found or not (found[0] / INTRO_FILE).is_file():
            raise HTTPException(404)
        p = found[0] / INTRO_FILE
        return FileResponse(p, media_type="video/mp4", filename=f"{found[1]}_{p.parent.name}_intro.mp4" if dl else None)

    @r.get("/media/thumb/{moment_id}")
    def media_thumb(moment_id: int, dl: int = 0, shape: str = "wide"):
        from chatwatcher.brand.thumb import COVER_FILE, THUMB_FILE
        file = COVER_FILE if shape == "tall" else THUMB_FILE
        found = _clip_folder(moment_id)
        if not found or not (found[0] / file).is_file():
            raise HTTPException(404)
        p = found[0] / file
        return FileResponse(p, media_type="image/jpeg", filename=f"{found[1]}_{p.parent.name}_{p.name}" if dl else None)

    @r.get("/media/preview/{moment_id}/{name}")
    def media_preview_frame(moment_id: int, name: str, conn=Depends(get_conn)):
        p_row = conn.execute("SELECT folder FROM previews WHERE moment_id = ?", (moment_id,)).fetchone()
        if not p_row:
            raise HTTPException(404)
        frames = (Path(p_row["folder"]) / "frames").resolve()
        p = (frames / name).resolve()
        if p.parent != frames or p.suffix != ".jpg" or not p.is_file():
            raise HTTPException(404)
        return FileResponse(p, media_type="image/jpeg")

    @r.get("/media/frame/{clip_id}/{name}")
    def media_frame(clip_id: int, name: str, conn=Depends(get_conn)):
        c = conn.execute("SELECT folder FROM clips WHERE id = ?", (clip_id,)).fetchone()
        if not c or not c["folder"]:
            raise HTTPException(404)
        frames = (Path(c["folder"]) / "frames").resolve()
        p = (frames / name).resolve()
        if p.parent != frames or p.suffix != ".jpg" or not p.is_file():
            raise HTTPException(404)
        return FileResponse(p, media_type="image/jpeg")

    @r.get("/media/sound/{sound_id}")
    def media_sound(sound_id: str, conn=Depends(get_conn)):
        from chatwatcher.edit import sounds
        s = sounds.get_sound(conn, sound_id) if sounds.valid_id(sound_id) else None
        p = sounds.sound_path(cfg, s) if s else None
        if not p or not p.is_file() or p.parent != sounds.sounds_dir(cfg):
            raise HTTPException(404)
        return FileResponse(p, media_type="audio/flac")

    @r.get("/media/overlay/{oid}")
    def media_overlay(oid: str, conn=Depends(get_conn)):
        from chatwatcher.edit import overlays
        o = overlays.get_overlay(conn, oid)
        p = overlays.video_path(cfg, o) if o else None
        if not p or not p.is_file() or p.parent != overlays.overlays_dir(cfg):
            raise HTTPException(404)
        return FileResponse(p, media_type="video/webm")

    @r.get("/media/overlay/{oid}/sound")
    def media_overlay_sound(oid: str, conn=Depends(get_conn)):
        from chatwatcher.edit import overlays
        o = overlays.get_overlay(conn, oid)
        p = overlays.sound_path(cfg, o) if o else None
        if not p or not p.is_file() or p.parent != overlays.overlays_dir(cfg):
            raise HTTPException(404)
        return FileResponse(p, media_type="audio/flac")

    return r
