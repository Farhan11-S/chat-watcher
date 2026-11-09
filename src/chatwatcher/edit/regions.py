"""Named boxes on the picture ("face", "game", "chat") that zoom effects aim at.

VTuber layouts rarely change, so a box is drawn once per channel and every stream of
that channel uses it. A stream that isn't linked to a channel gets its own boxes.
Effects name their region, so redrawing a box moves every zoom that uses it.
Boxes are fractions of the frame: x, y is the top left corner, w, h the size."""

from __future__ import annotations

import math
import re
import sqlite3
from typing import Any

MAX_REGIONS = 8
MIN_SIZE = 0.03
_NAME = re.compile(r"^[a-z0-9][a-z0-9 _-]{0,19}$")


class RegionError(Exception):
    pass


def normal_name(name: Any) -> str:
    """'Face ' → 'face'; '' if it can't be a region name."""
    n = re.sub(r"\s+", " ", str(name or "")).strip().lower()
    return n if _NAME.match(n) else ""


def scope_for(conn: sqlite3.Connection, moment_pk: int) -> tuple[str, str]:
    """(scope, who it's for): the moment's channel, or its stream when there's no channel."""
    r = conn.execute(
        """SELECT v.video_id, c.channel_id, c.title, c.handle FROM moments m
           JOIN videos v ON v.id = m.video_id LEFT JOIN channels c ON c.id = v.channel_id
           WHERE m.id = ?""", (moment_pk,)).fetchone()
    if r is None:
        raise RegionError("No such moment.")
    if r["channel_id"]:
        return f"channel:{r['channel_id']}", r["title"] or r["handle"] or "this channel"
    return f"video:{r['video_id']}", "this stream"


def get(conn: sqlite3.Connection, scope: str) -> dict[str, dict[str, float]]:
    return {r["name"]: {"x": r["x"], "y": r["y"], "w": r["w"], "h": r["h"]}
            for r in conn.execute("SELECT * FROM regions WHERE scope = ? ORDER BY rowid", (scope,))}


def clean(raw: Any) -> list[dict[str, Any]]:
    """Check boxes from the browser. Raises RegionError."""
    if not isinstance(raw, list):
        raise RegionError("Regions must be a list.")
    if len(raw) > MAX_REGIONS:
        raise RegionError(f"At most {MAX_REGIONS} regions.")
    out, names = [], set()
    for i, r in enumerate(raw, 1):
        if not isinstance(r, dict):
            raise RegionError(f"Region {i} isn't a box.")
        name = normal_name(r.get("name"))
        if not name:
            raise RegionError(f"Region {i} needs a name: letters, numbers, spaces, - or _, up to 20.")
        if name in names:
            raise RegionError(f"Two regions are called {name}.")
        try:
            x, y, w, h = (float(r[k]) for k in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            raise RegionError(f"Region {name} needs x, y, w and h.")
        if not all(math.isfinite(v) for v in (x, y, w, h)):
            raise RegionError(f"Region {name} has a bad number.")
        x, y = min(max(x, 0.0), 1 - MIN_SIZE), min(max(y, 0.0), 1 - MIN_SIZE)
        w, h = min(max(w, MIN_SIZE), 1 - x), min(max(h, MIN_SIZE), 1 - y)
        names.add(name)
        out.append({"name": name, "x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)})
    return out


def replace(conn: sqlite3.Connection, scope: str, raw: Any) -> dict[str, dict[str, float]]:
    """Store this scope's boxes (all of them; ones left out are removed)."""
    boxes = clean(raw)
    conn.execute("DELETE FROM regions WHERE scope = ?", (scope,))
    conn.executemany("INSERT INTO regions (scope, name, x, y, w, h) VALUES (?, ?, ?, ?, ?, ?)",
                     [(scope, b["name"], b["x"], b["y"], b["w"], b["h"]) for b in boxes])
    return get(conn, scope)
