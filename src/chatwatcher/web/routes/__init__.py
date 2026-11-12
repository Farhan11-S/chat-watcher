"""The dashboard's routes, one module per feature. Each has `router(ctx)`, which returns an
APIRouter; web/app.py builds the Ctx and includes them (ROUTERS, in app.py, sets the order)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from chatwatcher.config import Config
from chatwatcher.pipeline import Deps


@dataclass
class Ctx:
    """What one app's routes share. One per create_app(): tests build several apps with
    different configs, so none of this may live in a module global."""
    cfg: Config
    deps: Deps | None                                   # fakes in tests; None = the real tools
    tz: Any                                             # general.timezone (clock.zone)
    render: Callable[..., HTMLResponse]                 # render(request, "page.html", **values)
    get_conn: Callable[[], Iterator[sqlite3.Connection]]   # Depends(ctx.get_conn): one per request
    worker_status: Callable[[], str]
    poke: Callable[[], None]                            # wake this process's worker, if it has one
    drive_cache: dict[str, Any] = field(default_factory=lambda: {"usage": None})   # storage page

    def with_worker(self, state: dict[str, Any]) -> dict[str, Any]:
        return state | {"worker": self.worker_status()}


def redirect(url: str, msg: str | None = None, error: str | None = None) -> RedirectResponse:
    url, hash_, frag = url.partition("#")           # the message goes before "#…", or the page never sees it
    sep = "&" if "?" in url else "?"
    if msg:
        url += f"{sep}msg={quote(msg)}"
    elif error:
        url += f"{sep}error={quote(error)}"
    return RedirectResponse(url + hash_ + frag, status_code=303)


def edit_error(e: Exception) -> JSONResponse:
    from chatwatcher.edit.service import EditError
    status = 500
    if isinstance(e, EditError):
        status = 404 if e.kind == "not_clipped" else 400
    return JSONResponse({"ok": False, "error": str(e)}, status_code=status)


def kind_ok(kind: str) -> str:
    from chatwatcher.edit.service import KINDS
    if kind not in KINDS:
        raise HTTPException(400, "kind must be long or short")
    return kind
