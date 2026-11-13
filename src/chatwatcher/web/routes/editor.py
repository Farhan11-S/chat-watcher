"""The clip editor's API: load and save an edit, render, AI edit and Direct, subtitles,
names, voices, transcription and zoom regions."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from chatwatcher import jobs
from chatwatcher.clip.media import MediaError
from chatwatcher.web.routes import Ctx, edit_error, kind_ok


class EditIn(BaseModel):
    segments: Any = None      # [[start, end, gain_db], …] clip time; checked by edit.service
    source: str | None = None
    effects: Any = None       # [{t, sound, gain, duck, …}] clip time; None = keep the saved ones
    subs: Any = None          # [{t0, t1, text, by}] clip time; None = keep (or keep building them from the words)
    subs_style: Any = None    # {on, size, position}
    kind: str = "long"        # long (edit.mp4) | short (short.mp4, 9:16)
    layout: Any = None        # a Short's layout (edit.layout); None = keep


class InsertIn(BaseModel):
    url: str


class RegionsIn(BaseModel):
    regions: Any = None       # [{name, x, y, w, h}] as fractions of the frame
    kind: str = "long"        # which edit's render state to answer with


class NamesIn(BaseModel):
    terms: Any = None         # the whole list (replaces it)
    add: Any = None           # or terms to add


class VoicesIn(BaseModel):
    voices: Any = None        # {"0": {"name": "Gigi", "color": "FF8FC8"}, ...}
    recolor: int | None = None    # whose colour you just changed (it changes on the VTubers list)


class AutoEditIn(EditIn):
    intensity: str = "normal"
    message: str = ""         # the director's words to the AI edit's session
    new: bool = False         # start a new session


def router(ctx: Ctx) -> APIRouter:
    r = APIRouter()
    cfg, deps, get_conn = ctx.cfg, ctx.deps, ctx.get_conn

    def save_body(conn: Any, pk: int, body: EditIn) -> list[list[float]]:
        from chatwatcher.edit.service import save
        return save(cfg, conn, pk, body.segments, body.source, body.effects, body.subs, body.subs_style,
                    kind=body.kind, layout=body.layout)

    @r.get("/api/moment/{pk}/edit")
    def api_edit(pk: int, kind: str = "long", conn=Depends(get_conn)):
        """Waveform, pauses, Claude's cuts, your saved cut and the render's state (kind=short:
        the 9:16 Short and its layout)."""
        from chatwatcher.edit.service import EditError, load
        try:
            d = load(cfg, conn, pk, kind_ok(kind))
        except (EditError, MediaError) as e:
            return edit_error(e)
        d["render"] = ctx.with_worker(d["render"])
        return JSONResponse({"ok": True, **d})

    @r.post("/moment/{pk}/edit")
    def edit_save(pk: int, body: EditIn, conn=Depends(get_conn)):
        from chatwatcher.edit.service import EditError, render_state
        try:
            segs = save_body(conn, pk, body)
        except (EditError, MediaError) as e:
            return edit_error(e)
        return JSONResponse({"ok": True, "segments": segs,
                             "render": ctx.with_worker(render_state(conn, pk, cfg=cfg, kind=body.kind))})

    @r.post("/moment/{pk}/render")
    def edit_render(pk: int, body: EditIn, conn=Depends(get_conn)):
        """Save the cut and queue the render (it goes ahead of other queued work)."""
        from chatwatcher.edit.service import STAGE, EditError, render_state
        m = conn.execute("SELECT video_id FROM moments WHERE id = ?", (pk,)).fetchone()
        if m is None:
            raise HTTPException(404)
        try:
            save_body(conn, pk, body)
        except (EditError, MediaError) as e:
            return edit_error(e)
        if jobs.enqueue(conn, m["video_id"], STAGE[body.kind], moment_pk=pk) is None:
            return JSONResponse({"ok": False, "error": "A render of this clip is running. Press Render again "
                                                       "when it's done to include your latest changes."},
                                status_code=409)
        ctx.poke()
        return JSONResponse({"ok": True, "render": ctx.with_worker(render_state(conn, pk, cfg=cfg, kind=body.kind))})

    def run_direct(pk: int, body: AutoEditIn, conn: Any, new: bool) -> JSONResponse:
        from chatwatcher.edit.autoedit import AutoEditError
        from chatwatcher.edit.direct import direct
        from chatwatcher.edit.service import EditError, render_state
        try:
            if body.segments is not None:          # the page sends its edit; a bare message keeps the saved one
                save_body(conn, pk, body)
            t = direct(cfg, conn, pk, body.message, kind=body.kind, new=new, intensity=body.intensity,
                       runner=deps.claude if deps else None)
        except (EditError, MediaError) as e:
            return edit_error(e)
        except AutoEditError as e:
            return JSONResponse({"ok": False, "error": str(e), "kind": e.kind},
                                status_code=503 if e.kind in ("usage_limit", "timeout") else 400)
        out: dict[str, Any] = {"ok": True, "applied": t.result is not None, "reply": t.reply, "fresh": t.fresh,
                               "messages": t.messages,
                               "render": ctx.with_worker(render_state(conn, pk, cfg=cfg, kind=body.kind))}
        if t.result is not None:
            r, p = t.result, t.result.plan
            out |= {"segments": r.segments, "note": r.note, "hook_title": r.hook_title, "cut_s": p.cut_s,
                    "kept": p.pieces_kept, "pieces": p.pieces_total, "cold_open": p.cold_open,
                    "warnings": p.warnings, "counts": r.counts, "model": r.model}
        return JSONResponse(out)

    @r.post("/moment/{pk}/autoedit")
    def edit_auto(pk: int, body: AutoEditIn, conn=Depends(get_conn)):
        """Save the edit, then start a new AI edit session: Claude cuts the clip, plans the
        camera, places effects and sets word-by-word captions and a Short's hook title
        (30-90 s; the page waits). Directing it goes on at /direct."""
        return run_direct(pk, body, conn, new=True)

    @r.post("/moment/{pk}/direct")
    def edit_direct(pk: int, body: AutoEditIn, conn=Depends(get_conn)):
        """Save the edit, then send the director's message to the AI edit's session (or
        start one: body.new, or none yet). Claude answers, and may remake the edit."""
        return run_direct(pk, body, conn, new=body.new)

    @r.get("/api/moment/{pk}/direct")
    def api_direct(pk: int, kind: str = "long", conn=Depends(get_conn)):
        from chatwatcher.edit.direct import thread
        session, messages = thread(conn, pk, kind_ok(kind))
        return JSONResponse({"ok": True, "session": bool(session), "messages": messages})

    @r.post("/api/moment/{pk}/insert")
    def api_insert(pk: int, body: InsertIn, conn=Depends(get_conn)):
        """Download a link (an X post, a picture, a video) into this clip's inserts, for a
        cutaway (a few seconds; a video up to a minute: the page waits, like Direct)."""
        from chatwatcher.edit import inserts
        from chatwatcher.edit.editstore import overlay_list
        from chatwatcher.edit.service import EditError, clip_ref
        try:
            clip_ref(conn, pk)
            got = inserts.fetch(cfg, conn, pk, body.url)
        except EditError as e:
            return edit_error(e)
        except inserts.InsertError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "added": [g["id"] for g in got], "overlays": overlay_list(conn, pk)})

    @r.post("/moment/{pk}/subs/polish")
    def subs_polish(pk: int, body: EditIn, conn=Depends(get_conn)):
        """Save the edit, then have Claude fix (and translate) its subtitle lines (10-60 s; the page waits)."""
        from chatwatcher.edit.service import EditError, render_state
        from chatwatcher.subs.polish import PolishError, polish
        try:
            save_body(conn, pk, body)
            r = polish(cfg, conn, pk, runner=deps.claude if deps else None, kind=body.kind)
        except (EditError, MediaError) as e:
            return edit_error(e)
        except PolishError as e:
            return JSONResponse({"ok": False, "error": str(e), "kind": e.kind},
                                status_code=503 if e.kind in ("usage_limit", "timeout") else 400)
        return JSONResponse({"ok": True, "subs": r.subs, "changed": r.changed, "joined": r.joined, "dropped": r.dropped,
                             "names": r.names, "names_added": r.names_added, "note": r.note, "model": r.model,
                             "render": ctx.with_worker(render_state(conn, pk, cfg=cfg, kind=body.kind))})

    @r.post("/moment/{pk}/names")
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

    @r.post("/moment/{pk}/voices")
    def subs_voices(pk: int, body: VoicesIn, conn=Depends(get_conn)):
        """Who's in the clip, and their colour. Names join the channel's names (spelled right
        in subtitles) and the VTubers list (their colour on every clip)."""
        from chatwatcher.edit import regions
        from chatwatcher.subs import names, speakers
        try:
            scope, _label = regions.scope_for(conn, pk)
        except regions.RegionError:
            raise HTTPException(404)
        try:
            voices = speakers.put(conn, pk, body.voices, body.recolor)
        except speakers.SpeakerError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        names.add(conn, scope, [v["name"] for v in voices.values() if v["name"]])
        master = speakers.vtubers(conn)
        return JSONResponse({"ok": True, "voices": voices, "people": {k: v["color"] for k, v in master.items()},
                             "names": [v["name"] for v in master.values() if v["status"] not in ("Retirement",)]})

    @r.post("/moment/{pk}/transcribe")
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
        ctx.poke()
        return JSONResponse({"ok": True, "transcript": transcript_state(cfg, conn, pk, clip)})

    @r.get("/api/moment/{pk}/transcript")
    def api_transcript(pk: int, kind: str = "long", conn=Depends(get_conn)):
        """kind: whose subtitles come with it (the Short's are shorter, and may be saved apart)."""
        from chatwatcher.edit.service import EditError, check_kind, clip_ref, subtitles, transcript_lines, transcript_state
        try:
            clip = clip_ref(conn, pk)
            check_kind(kind)
        except EditError as e:
            return edit_error(e)
        return JSONResponse({"ok": True, "transcript": transcript_state(cfg, conn, pk, clip),
                             "lines": transcript_lines(clip.folder), **subtitles(cfg, conn, pk, clip, kind)})

    @r.post("/moment/{pk}/regions")
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
                             "render": ctx.with_worker(render_state(conn, pk, cfg=cfg, kind=kind_ok(body.kind)))})

    @r.get("/api/moment/{pk}/render")
    def api_render(pk: int, kind: str = "long", conn=Depends(get_conn)):
        from chatwatcher.edit.service import render_state
        return JSONResponse({"ok": True, "render": ctx.with_worker(render_state(conn, pk, cfg=cfg, kind=kind_ok(kind)))})

    return r
