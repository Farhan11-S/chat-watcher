"""The Storage page: disk and Drive usage, deleting streams, the auto-upload switch."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from chatwatcher import prefs, storage
from chatwatcher.web.routes import Ctx, redirect

DRIVE_TTL_S = 15 * 60


def router(ctx: Ctx) -> APIRouter:
    r = APIRouter()
    cfg, deps, render, get_conn = ctx.cfg, ctx.deps, ctx.render, ctx.get_conn
    drive_cache = ctx.drive_cache            # {"usage": DriveUsage | None}, one per app

    def fresh_drive() -> storage.DriveUsage | None:
        u = drive_cache["usage"]
        return u if u is not None and time.time() - u.checked_at < DRIVE_TTL_S else None

    @r.get("/storage", response_class=HTMLResponse)
    def storage_page(request: Request, conn=Depends(get_conn)):
        from chatwatcher.doctor import run_checks
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
                      drive_pending=cfg.drive.enabled and drive is None, checks=run_checks(cfg),
                      auto_upload=prefs.auto_upload(cfg, conn))

    @r.post("/storage/delete")
    def storage_delete(key: list[str] = Form([]), mode: str = Form(""), conn=Depends(get_conn)):
        if mode not in ("videos", "all"):
            raise HTTPException(400)
        if not key:
            return redirect("/storage", error="Tick at least one stream first.")
        r = storage.delete_streams(cfg, conn, key, videos_only=mode == "videos")
        what = "clip videos of" if mode == "videos" else ""
        msg = f"Freed {storage.fmt_bytes(r.freed)}: deleted the {what} {r.streams} stream(s)".replace("  ", " ")
        msg += " from this computer." + (" Google Drive wasn't touched." if cfg.drive.enabled else "")
        if r.skipped:
            msg += " Skipped: " + "; ".join(r.skipped)
        return redirect("/storage", msg=msg) if r.streams else redirect("/storage", error=msg)

    @r.post("/storage/drive")
    def storage_drive_refresh():
        if not cfg.drive.enabled:
            return redirect("/storage", error="Google Drive isn't set up (drive.enabled = false in config.toml).")
        drive_cache["usage"] = storage.drive_usage(cfg, rclone=deps.rclone if deps else None)
        return redirect("/storage")

    @r.get("/settings")
    def settings():
        return RedirectResponse("/storage", status_code=303)       # merged into the Storage page

    @r.post("/storage/auto-upload")
    def set_auto_upload(on: str = Form(""), conn=Depends(get_conn)):
        prefs.put(conn, prefs.AUTO_UPLOAD, bool(on))
        return redirect("/storage", msg="Streams will be uploaded to Drive automatically." if on else
                         "Auto-upload is off. Use the Upload buttons on a stream or clip page.")

    return r


def _segments(parts: list[tuple[str, int | None, str]], total: int) -> list[dict[str, Any]]:
    """Blocks of a segmented bar; zero-sized parts draw nothing."""
    out = []
    for name, value, cls in parts:
        if value and value > 0 and total:
            share = value / total
            out.append({"name": name, "value": value, "cls": cls, "pct": share * 100,
                        "label": f"{share * 100:.0f}%" if share >= 0.12 else ""})
    return out
