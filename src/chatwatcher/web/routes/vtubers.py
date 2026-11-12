"""The VTubers page: each VTuber's subtitle colour, for every clip (subs.speakers)."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse

from chatwatcher.web.routes import Ctx, redirect


def router(ctx: Ctx) -> APIRouter:
    r = APIRouter()
    render, get_conn = ctx.render, ctx.get_conn

    @r.get("/vtubers", response_class=HTMLResponse)
    def vtubers_page(request: Request, conn=Depends(get_conn)):
        """Each VTuber's subtitle colour, for every clip (subs.speakers)."""
        from chatwatcher.subs import speakers
        used: dict[str, int] = {}
        for (raw,) in conn.execute("SELECT voices_json FROM voices"):
            for v in speakers.clean(json.loads(raw)).values():
                if v["name"]:
                    used[v["name"].lower()] = used.get(v["name"].lower(), 0) + 1
        master = speakers.vtubers(conn)
        return render(request, "vtubers.html", vtubers=master, used=used,
                      suggest=speakers.PALETTE[len(master) % len(speakers.PALETTE)])

    @r.post("/vtubers/import")
    async def vtubers_import(file: UploadFile = File(...), conn=Depends(get_conn)):
        from chatwatcher.subs import speakers
        raw = await file.read(2_000_000)
        try:
            added, updated, problems = speakers.import_csv(conn, raw.decode("utf-8", errors="replace"))
        except speakers.SpeakerError as e:
            return redirect("/vtubers", error=str(e))
        msg = f"Imported {file.filename}: {added} added, {updated} updated."
        if problems:
            msg += f" Skipped {len(problems)}: " + "; ".join(problems[:5])
        return redirect("/vtubers", msg=msg)

    @r.post("/vtubers/save")
    def vtubers_save(name: str = Form(""), color: str = Form(""), old: str = Form(""), conn=Depends(get_conn)):
        from chatwatcher.subs import speakers
        try:
            v = speakers.save_vtuber(conn, name, color, old or None)
        except speakers.SpeakerError as e:
            return redirect("/vtubers", error=str(e))
        return redirect("/vtubers", msg=f"Saved {v['name']} (#{v['color']}).")

    @r.post("/vtubers/delete")
    def vtubers_delete(name: str = Form(""), conn=Depends(get_conn)):
        from chatwatcher.subs import speakers
        speakers.delete_vtuber(conn, name)
        return redirect("/vtubers", msg=f"Removed {name}. Clips that named her keep her name, in a default colour.")

    return r
