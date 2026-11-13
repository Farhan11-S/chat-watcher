"""The Brand page (name, colours, logo, fonts) and a clip's intro and thumbnail."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from chatwatcher import jobs
from chatwatcher.clip.media import MediaError
from chatwatcher.web.routes import Ctx, edit_error, redirect


class IntroIn(BaseModel):
    teasers: Any = None       # [[start, end, gain_db, style], …] clip time; "auto"; None = keep
    on_edit: bool | None = None


class ThumbIn(BaseModel):
    layers: Any = None        # brand.layers: [{id, kind, x, y, w, rot, …}], t in clip time
    shape: str = "wide"       # wide: thumbnail.jpg 1280×720 | tall: the Short's cover, short_cover.jpg 1080×1920


class SpriteIn(BaseModel):
    layer: Any = None
    shape: str = "wide"


class ThumbAIIn(BaseModel):
    shape: str = "wide"


def router(ctx: Ctx) -> APIRouter:
    r = APIRouter()
    cfg, render, get_conn = ctx.cfg, ctx.render, ctx.get_conn

    # ── intro and thumbnail ──────────────────────────────────────────────────

    @r.get("/api/moment/{pk}/intro")
    def api_intro(pk: int, conn=Depends(get_conn)):
        from chatwatcher.brand.service import load_intro
        from chatwatcher.edit.service import EditError
        try:
            d = load_intro(cfg, conn, pk)
        except (EditError, MediaError) as e:
            return edit_error(e)
        d["state"] = ctx.with_worker(d["state"])
        return JSONResponse({"ok": True, **d})

    @r.post("/moment/{pk}/intro")
    def intro_save(pk: int, body: IntroIn, conn=Depends(get_conn)):
        from chatwatcher.brand.service import load_intro, save_intro
        from chatwatcher.edit.service import EditError, render_state
        try:
            save_intro(cfg, conn, pk, body.teasers, body.on_edit)
            d = load_intro(cfg, conn, pk)
        except (EditError, MediaError) as e:
            return edit_error(e)
        d["state"] = ctx.with_worker(d["state"])
        return JSONResponse({"ok": True, **d, "render": ctx.with_worker(render_state(conn, pk, cfg=cfg))})

    @r.post("/moment/{pk}/intro/render")
    def intro_render(pk: int, body: IntroIn, conn=Depends(get_conn)):
        """Save the teasers and queue the intro on its own (it goes ahead of other queued work)."""
        from chatwatcher.brand.service import intro_state, save_intro
        from chatwatcher.edit.service import EditError
        m = conn.execute("SELECT video_id FROM moments WHERE id = ?", (pk,)).fetchone()
        if m is None:
            raise HTTPException(404)
        try:
            save_intro(cfg, conn, pk, body.teasers, body.on_edit)
        except (EditError, MediaError) as e:
            return edit_error(e)
        if jobs.enqueue(conn, m["video_id"], "intro", moment_pk=pk) is None:
            return JSONResponse({"ok": False, "error": "The intro is rendering right now. Press again when it's "
                                                       "done to include your latest changes."}, status_code=409)
        ctx.poke()
        return JSONResponse({"ok": True, "state": ctx.with_worker(intro_state(cfg, conn, pk))})

    @r.get("/api/moment/{pk}/intro/state")
    def api_intro_state(pk: int, conn=Depends(get_conn)):
        from chatwatcher.brand.service import intro_state
        return JSONResponse({"ok": True, "state": ctx.with_worker(intro_state(cfg, conn, pk))})

    @r.get("/api/moment/{pk}/thumb")
    def api_thumb(pk: int, shape: str = "wide", conn=Depends(get_conn)):
        from chatwatcher.brand.service import load_thumb
        from chatwatcher.edit.service import EditError
        try:
            return JSONResponse({"ok": True, **load_thumb(cfg, conn, pk, shape)})
        except (EditError, MediaError) as e:
            return edit_error(e)

    @r.post("/moment/{pk}/thumb/sprite")
    def thumb_sprite(pk: int, body: SpriteIn, conn=Depends(get_conn)):
        """One layer drawn on its own, for the thumbnail maker (brand.canvas.sprite)."""
        from fastapi.responses import Response
        from chatwatcher.brand.service import sprite_image
        from chatwatcher.edit.service import EditError
        try:
            data, mime = sprite_image(cfg, conn, pk, body.layer, body.shape)
        except (EditError, MediaError) as e:
            return edit_error(e)
        return Response(data, media_type=mime, headers={"Cache-Control": "no-store"})

    @r.post("/moment/{pk}/thumb/preview")
    def thumb_preview(pk: int, body: ThumbIn, conn=Depends(get_conn)):
        """The whole thumbnail for these layers, drawn now and not saved."""
        from fastapi.responses import Response
        from chatwatcher.brand.service import draw_thumb
        from chatwatcher.edit.service import EditError
        try:
            data, _layers, _warnings = draw_thumb(cfg, conn, pk, body.layers or [], body.shape)
        except (EditError, MediaError) as e:
            return edit_error(e)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @r.post("/moment/{pk}/thumb")
    def thumb_save(pk: int, body: ThumbIn, conn=Depends(get_conn)):
        from chatwatcher.brand.service import save_thumb
        from chatwatcher.edit.service import EditError
        try:
            return JSONResponse({"ok": True, **save_thumb(cfg, conn, pk, body.layers or [], body.shape)})
        except (EditError, MediaError) as e:
            return edit_error(e)

    @r.post("/moment/{pk}/thumb/ai")
    def thumb_ai(pk: int, body: ThumbAIIn, conn=Depends(get_conn)):
        """Queue Claude's thumbnail versions (the 'thumb' job; it goes ahead of other queued work)."""
        from chatwatcher.brand.service import ai_state
        m = conn.execute("SELECT video_id FROM moments WHERE id = ?", (pk,)).fetchone()
        if m is None:
            raise HTTPException(404)
        if body.shape not in ("wide", "tall"):
            return JSONResponse({"ok": False, "error": "shape must be wide or tall."}, status_code=400)
        if not cfg.claude.enabled:
            return JSONResponse({"ok": False, "error": "claude.enabled is false in the config."}, status_code=400)
        if jobs.enqueue(conn, m["video_id"], "thumb", moment_pk=pk, params={"shape": body.shape}) is None:
            return JSONResponse({"ok": False, "error": "Claude is making versions right now."}, status_code=409)
        ctx.poke()
        return JSONResponse({"ok": True, "ai": ctx.with_worker(ai_state(conn, pk))})

    @r.get("/api/moment/{pk}/thumb/ai")
    def api_thumb_ai(pk: int, shape: str = "wide", conn=Depends(get_conn)):
        from chatwatcher.brand.service import _ideas, ai_state
        from chatwatcher.edit.service import EditError, clip_ref
        try:
            clip = clip_ref(conn, pk)
        except EditError as e:
            return edit_error(e)
        if shape not in ("wide", "tall"):
            raise HTTPException(400)
        return JSONResponse({"ok": True, "ai": ctx.with_worker(ai_state(conn, pk)), "ideas": _ideas(conn, pk, clip, shape)})

    @r.get("/moment/{pk}/thumb/idea/{i}.jpg")
    def thumb_idea(pk: int, i: int, shape: str = "wide", conn=Depends(get_conn)):
        from chatwatcher.brand.service import idea_path
        from chatwatcher.edit.service import EditError, clip_ref
        try:
            p = idea_path(clip_ref(conn, pk), shape, i)
        except EditError:
            raise HTTPException(404)
        if not p.is_file():
            raise HTTPException(404)
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # ── brand page ───────────────────────────────────────────────────────────

    @r.get("/brand", response_class=HTMLResponse)
    def brand_page(request: Request, conn=Depends(get_conn)):
        from chatwatcher.brand import brand as br
        b = br.get(conn)
        fonts = {r: {"label": br.font_label(cfg, b, r), "mine": bool(getattr(b, f"{r}_font"))} for r in br.FONT_FILES}
        made = conn.execute("SELECT (SELECT COUNT(*) FROM intros WHERE rendered_json IS NOT NULL) AS intros, "
                            "(SELECT COUNT(*) FROM thumbs WHERE made_at IS NOT NULL) AS thumbs").fetchone()
        return render(request, "brand.html", brand=b, colours=br.COLOURS, fonts=fonts, made=made,
                      stamp=br.fingerprint(cfg, b), stickers=br.list_stickers(cfg))

    @r.post("/brand")
    def brand_save(name: str = Form(""), tagline: str = Form(""), deep: str = Form(""), purple: str = Form(""),
                   lavender: str = Form(""), light: str = Form(""), accent: str = Form(""),
                   logo: UploadFile | None = File(None), title_font: UploadFile | None = File(None),
                   text_font: UploadFile | None = File(None), conn=Depends(get_conn)):
        from chatwatcher.brand import brand as br
        try:
            br.update(conn, name=name, tagline=tagline,
                      colours={"deep": deep, "purple": purple, "lavender": lavender, "light": light, "accent": accent})
            tmp_dir = br.brand_dir(cfg) / ".upload"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            for role, up in (("logo", logo), ("title", title_font), ("text", text_font)):
                if up is None or not up.filename:
                    continue
                tmp = tmp_dir / f"in_{time.time_ns()}{Path(up.filename).suffix[:10]}"
                try:
                    size = 0
                    with open(tmp, "wb") as out:
                        while chunk := up.file.read(1 << 20):
                            size += len(chunk)
                            if size > br.MAX_FONT_BYTES:
                                raise br.BrandError(f"{up.filename} is too big.")
                            out.write(chunk)
                    if role == "logo":
                        br.import_logo(cfg, conn, tmp, up.filename)
                    else:
                        br.import_font(cfg, conn, role, tmp, up.filename)
                finally:
                    tmp.unlink(missing_ok=True)
        except br.BrandError as e:
            return redirect("/brand", error=str(e))
        return redirect("/brand", msg="Saved. Intros and thumbnails you make from now on use it.")

    @r.post("/brand/remove/{what}")
    def brand_remove(what: str, conn=Depends(get_conn)):
        from chatwatcher.brand import brand as br
        if what == "logo":
            br.remove_logo(cfg, conn)
        elif what in br.FONT_FILES:
            br.remove_font(cfg, conn, what)
        else:
            raise HTTPException(400)
        return redirect("/brand", msg="Removed.")

    @r.post("/brand/stickers")
    def brand_sticker_add(files: list[UploadFile] = File(...)):
        from chatwatcher.brand import brand as br
        tmp_dir = br.brand_dir(cfg) / ".upload"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        added = []
        try:
            for up in files:
                if not up.filename:
                    continue
                tmp = tmp_dir / f"in_{time.time_ns()}{Path(up.filename).suffix[:10]}"
                try:
                    size = 0
                    with open(tmp, "wb") as out:
                        while chunk := up.file.read(1 << 20):
                            size += len(chunk)
                            if size > br.MAX_LOGO_BYTES:
                                raise br.BrandError(f"{up.filename} is bigger than 10 MB.")
                            out.write(chunk)
                    added.append(br.import_sticker(cfg, tmp, up.filename))
                finally:
                    tmp.unlink(missing_ok=True)
        except br.BrandError as e:
            return redirect("/brand", error=str(e))
        return redirect("/brand", msg=f"Added {len(added)} sticker(s). Claude can place them on thumbnails by name.")

    @r.post("/brand/stickers/remove/{name}")
    def brand_sticker_remove(name: str):
        from chatwatcher.brand import brand as br
        br.remove_sticker(cfg, name)
        return redirect("/brand", msg="Sticker removed.")

    @r.get("/brand/stickers/{name}")
    def brand_sticker(name: str):
        from chatwatcher.brand import brand as br
        if name not in br.list_stickers(cfg):
            raise HTTPException(404)
        return FileResponse(br.sticker_dir(cfg) / name, media_type="image/png")

    @r.get("/brand/logo.png")
    def brand_logo(conn=Depends(get_conn)):
        from chatwatcher.brand import brand as br
        p = br.logo_path(cfg)
        if not br.get(conn).logo or not p.is_file():
            raise HTTPException(404)
        return FileResponse(p, media_type="image/png", headers={"Cache-Control": "no-store"})

    @r.get("/brand/card.jpg")
    def brand_card_img(conn=Depends(get_conn)):
        from fastapi.responses import Response
        from chatwatcher.brand.service import brand_card
        return Response(brand_card(cfg, conn), media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    return r
