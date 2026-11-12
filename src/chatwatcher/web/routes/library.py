"""The Sounds page: the sound-effect library and the green-screen overlays.
The files themselves are served by routes/media.py."""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse

from chatwatcher.clip.media import MediaError
from chatwatcher.web.routes import Ctx, redirect


def router(ctx: Ctx) -> APIRouter:
    r = APIRouter()
    cfg, render, get_conn = ctx.cfg, ctx.render, ctx.get_conn

    @r.get("/sounds", response_class=HTMLResponse)
    def sounds_page(request: Request, conn=Depends(get_conn)):
        from chatwatcher.edit import sounds
        rows = sounds.list_sounds(conn)
        used: dict[str, int] = {}
        for r in conn.execute("SELECT effects_json FROM edits WHERE effects_json != '[]' "
                              "UNION ALL SELECT effects_json FROM shorts WHERE effects_json != '[]'"):
            for fx in json.loads(r["effects_json"]):
                used[fx.get("sound", "")] = used.get(fx.get("sound", ""), 0) + 1
        from chatwatcher.edit import overlays
        for r in conn.execute("SELECT effects_json FROM edits WHERE effects_json != '[]' "
                              "UNION ALL SELECT effects_json FROM shorts WHERE effects_json != '[]'"):
            for oid in {fx.get("overlay") for fx in json.loads(r["effects_json"]) if fx.get("overlay")}:
                used[oid] = used.get(oid, 0) + 1
        return render(request, "sounds.html", sounds=rows, used=used, pack=sounds.starter_status(conn),
                      overlays=overlays.list_overlays(conn))

    @r.post("/sounds/upload")
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
            return redirect("/sounds", error="Pick one or more sound files first.")
        msg = f"Added {len(added)} sound(s): {', '.join(added)}." if added else ""
        if skipped:
            msg += f" Already in your library: {', '.join(skipped)}."
        if added and not description and conn.execute(
                "SELECT 1 FROM sounds WHERE description = '' AND id IN (%s)" % ",".join("?" * len(added)),
                added).fetchone():
            msg += " Give each one a description, so Claude knows when to use it."
        if failed:
            return redirect("/sounds", error=(msg + "\nCouldn't add: " + "; ".join(failed)).strip())
        return redirect("/sounds", msg=msg.strip())

    @r.post("/sounds/{sound_id}/save")
    def sounds_save(sound_id: str, name: str = Form(""), description: str = Form(""), tags: str = Form(""),
                    source: str = Form(""), gain_db: float = Form(0.0), conn=Depends(get_conn)):
        from chatwatcher.edit import sounds
        try:
            sounds.update_sound(conn, sound_id, name=name, description=description, tags=tags,
                                source=source, gain_db=gain_db)
        except sounds.SoundError as e:
            return redirect("/sounds", error=str(e))
        return redirect(f"/sounds#s-{sound_id}", msg=f"Saved {sound_id}.")

    @r.post("/sounds/{sound_id}/delete")
    def sounds_delete(sound_id: str, conn=Depends(get_conn)):
        from chatwatcher.edit import sounds
        sounds.delete_sound(cfg, conn, sound_id)
        return redirect("/sounds", msg=f"Deleted {sound_id}.")

    @r.post("/overlays/upload")
    def overlays_upload(files: list[UploadFile] = File(...), description: str = Form(""), tags: str = Form(""),
                        source: str = Form(""), conn=Depends(get_conn)):
        from chatwatcher.edit import overlays
        tmp_dir = overlays.overlays_dir(cfg) / ".upload"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        added, failed = [], []
        for f in files:
            if not f.filename:
                continue
            tmp = tmp_dir / f"in_{time.time_ns()}{Path(f.filename).suffix[:10]}"
            try:
                size = 0
                with open(tmp, "wb") as out:
                    while chunk := f.file.read(1 << 20):
                        size += len(chunk)
                        if size > overlays.MAX_UPLOAD_BYTES:
                            raise overlays.OverlayError("bigger than 80 MB")
                        out.write(chunk)
                o = overlays.import_overlay(cfg, conn, tmp, original=f.filename, description=description,
                                            tags=tags, source=source)
                added.append(o["id"])
            except (overlays.OverlayError, MediaError) as e:
                failed.append(f"{f.filename}: {e}")
            finally:
                tmp.unlink(missing_ok=True)
        if not added and not failed:
            return redirect("/sounds#overlays", error="Pick one or more green-screen clips first.")
        msg = f"Added {len(added)} overlay(s): {', '.join(added)}." if added else ""
        if added and not description:
            msg += " Give each one a description, so Claude knows when to use it."
        if failed:
            return redirect("/sounds#overlays", error=(msg + "\nCouldn't add: " + "; ".join(failed)).strip())
        return redirect("/sounds#overlays", msg=msg.strip())

    @r.post("/overlays/{oid}/save")
    def overlays_save(oid: str, name: str = Form(""), description: str = Form(""), tags: str = Form(""),
                      source: str = Form(""), gain_db: float = Form(0.0), conn=Depends(get_conn)):
        from chatwatcher.edit import overlays
        try:
            overlays.update_overlay(conn, oid, name=name, description=description, tags=tags, source=source,
                                    gain_db=gain_db)
        except overlays.OverlayError as e:
            return redirect("/sounds#overlays", error=str(e))
        return redirect(f"/sounds#o-{oid}", msg=f"Saved {oid}.")

    @r.post("/overlays/{oid}/delete")
    def overlays_delete(oid: str, conn=Depends(get_conn)):
        from chatwatcher.edit import overlays
        overlays.delete_overlay(cfg, conn, oid)
        return redirect("/sounds#overlays", msg=f"Deleted {oid}.")

    return r
