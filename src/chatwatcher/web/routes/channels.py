"""The Channels page: watched channels and what the watcher has seen."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from chatwatcher import discover
from chatwatcher.web import listing
from chatwatcher.web.routes import Ctx, redirect


def router(ctx: Ctx) -> APIRouter:
    r = APIRouter()
    cfg, deps, render, get_conn = ctx.cfg, ctx.deps, ctx.render, ctx.get_conn

    @r.get("/channels", response_class=HTMLResponse)
    def channels(request: Request, seen: str = "", sq: str = "", sp: str = "", conn=Depends(get_conn)):
        seen = seen if seen in ("queued", "skipped") else ""
        return render(request, "channels.html", channels=discover.list_channels(conn, include_disabled=True),
                      seen=listing.seen(conn, q=sq, action=seen, page=listing.to_int(sp, 1)),
                      flt={"seen": seen, "sq": sq.strip()})

    @r.post("/channels")
    def channel_add(channel: str = Form(...), stream_type: str = Form("auto"), backfill: int = Form(1),
                    conn=Depends(get_conn)):
        try:
            row, res = discover.add_channel(cfg, conn, channel, stream_type=stream_type,
                                            backfill=backfill, ytdlp=deps.ytdlp if deps else None)
        except discover.DiscoverError as e:
            return redirect("/channels", error=str(e))
        return redirect("/channels", msg=f"Watching {row['title']}: queued {len(res.queued)}, "
                                          f"skipped {len(res.skipped)}, {res.waiting} upcoming/live.")

    @r.post("/channels/{pk}/{action}")
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
                return redirect("/channels", error=res.error)
            return redirect("/channels", msg=f"{res.channel}: queued {len(res.queued)} new stream(s).")
        else:
            raise HTTPException(400)
        return redirect("/channels", msg="Saved.")

    return r
