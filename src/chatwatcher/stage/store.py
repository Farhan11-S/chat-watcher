"""Table `stages`: per moment, which model, set and shape the 3D stage uses, the cut it
re-enacts (its own copy, taken from the Short or the long edit when you set it up), and the
scene. Also what the page loads (state)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from chatwatcher.config import Config
from chatwatcher.edit import clipref as cr
from chatwatcher.stage import models, script

SHAPES = ("short", "long")
STAGE_FILE = "stage.mp4"


class StageError(Exception):
    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind       # not_clipped | invalid | not_found | error


def get(conn: sqlite3.Connection, moment_pk: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM stages WHERE moment_id = ?", (moment_pk,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    d["segments"] = json.loads(d.pop("segments_json"))
    d["scene"] = json.loads(d.pop("scene_json")) if d["scene_json"] else None
    d["rendered"] = json.loads(d.pop("rendered_json")) if d["rendered_json"] else None
    return d


def cut_of(conn: sqlite3.Connection, moment_pk: int, shape: str, clip: cr.ClipRef) -> list[list[float]]:
    """The edit's cut for this shape, in stream time: the Short's (or else the long edit's)
    for a Short, the long edit's for 16:9; the whole clip when there is none."""
    rows = [cr.stored(conn, moment_pk, "short"), cr.stored(conn, moment_pk, "long")] if shape == "short" \
        else [cr.stored(conn, moment_pk, "long")]
    for row in rows:
        if row is not None:
            segs = [[float(s[0]), float(s[1])] for s in json.loads(row["segments_json"] or "[]") if s[1] > s[0]]
            if segs:
                return segs
    return [[clip.zero_s, clip.zero_s + clip.duration_s]]


def duration(segments: list[list[float]]) -> float:
    return round(sum(b - a for a, b in segments), 3)


def setup(cfg: Config, conn: sqlite3.Connection, moment_pk: int, *, model: str | None = None,
          set_name: str | None = None, shape: str | None = None, recut: bool = False) -> dict[str, Any]:
    """Choose the model, set and shape (None: keep, or a first guess). The cut is taken from
    the edit the first time, when the shape changes, and when you ask (recut)."""
    clip = cr.clip_ref(conn, moment_pk)
    old = get(conn, moment_pk)
    if model is not None and model != "":
        models.load(cfg, model)                # raises when there is no such model
    model = model or (old or {}).get("model") or models.pick_for(cfg, conn, moment_pk)
    set_name = set_name or (old or {}).get("set_name") or "room"
    if set_name not in script.SETS:
        raise StageError(f"No set called {set_name!r}.", "invalid")
    shape = shape or (old or {}).get("shape") or "short"
    if shape not in SHAPES:
        raise StageError("shape must be short or long.", "invalid")
    segments = old["segments"] if old and not recut and old["shape"] == shape else cut_of(conn, moment_pk, shape, clip)
    scene = (old or {}).get("scene")
    if scene is not None:                      # a new set keeps the shots, poses and faces it can use
        scene = script.validate(scene, duration(segments), set_name)[0]
    conn.execute(
        """INSERT INTO stages (moment_id, model, set_name, shape, segments_json, scene_json, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT (moment_id) DO UPDATE SET model = excluded.model, set_name = excluded.set_name,
               shape = excluded.shape, segments_json = excluded.segments_json, scene_json = excluded.scene_json,
               updated_at = excluded.updated_at""",
        (moment_pk, model, set_name, shape, json.dumps(segments), json.dumps(scene) if scene else None))
    return get(conn, moment_pk) or {}


def save_scene(conn: sqlite3.Connection, moment_pk: int, scene: Any, *, by: str = "you") -> tuple[dict[str, Any], list[str]]:
    st = get(conn, moment_pk)
    if st is None:
        raise StageError("Set the stage up first.", "invalid")
    clean, warnings = script.validate(scene, duration(st["segments"]), st["set_name"], by=by)
    conn.execute("UPDATE stages SET scene_json = ?, updated_at = datetime('now') WHERE moment_id = ?",
                 (json.dumps(clean), moment_pk))
    return clean, warnings


def render_state(conn: sqlite3.Connection, moment_pk: int, st: dict[str, Any] | None = None) -> dict[str, Any]:
    """Is stage.mp4 there and made from what the page shows now (scene and voice track)?"""
    st = st if st is not None else get(conn, moment_pk)
    job = conn.execute("SELECT status, error, message FROM jobs WHERE moment_id = ? AND stage = '3d'",
                       (moment_pk,)).fetchone()
    c = conn.execute("SELECT folder FROM clips WHERE moment_id = ?", (moment_pk,)).fetchone()
    from pathlib import Path
    have = bool(c and c["folder"] and (Path(c["folder"]) / STAGE_FILE).is_file())
    fresh = bool(have and st and st.get("rendered") == rendered_key(st, track_key(conn, moment_pk, st)))
    return {"have": have, "fresh": fresh, "progress": (st or {}).get("render_progress"),
            "rendered_at": (st or {}).get("rendered_at"),
            "job": dict(job) if job else None}


def rendered_key(st: dict[str, Any], track: str | None = None) -> dict[str, Any]:
    return {k: st.get(k) for k in ("model", "set_name", "shape", "segments", "scene")} | {"track": track}


def track_key(conn: sqlite3.Connection, moment_pk: int, st: dict[str, Any]) -> str | None:
    """The key the stage's voice track has now (a better mouth makes stage.mp4 out of date)."""
    from chatwatcher.stage import track
    try:
        return track.current(conn, moment_pk, st)[1]
    except (track.TrackError, cr.EditError):
        return None


def screen_region(conn: sqlite3.Connection, moment_pk: int, width: int = 16, height: int = 9) -> dict[str, float] | None:
    """The part of the stream picture the room's screen shows: the channel's 'game' (or
    'screen', 'content') region when there is one and it is landscape (a tall one is a
    Short's crop, which would sit in bars on a TV), else the whole picture."""
    from chatwatcher.edit import regions
    try:
        boxes = regions.get(conn, regions.scope_for(conn, moment_pk)[0])
    except regions.RegionError:
        return None
    for name in ("game", "screen", "content"):
        b = boxes.get(name)
        if b and b["h"] > 0 and (b["w"] * width) / (b["h"] * height) >= 1.3:
            return b
    return None


def page_state(cfg: Config, conn: sqlite3.Connection, moment_pk: int) -> dict[str, Any]:
    """Everything the 3D page loads."""
    from urllib.parse import quote
    from chatwatcher.stage import track
    clip = cr.clip_ref(conn, moment_pk)
    clip_id = conn.execute("SELECT id FROM clips WHERE moment_id = ?", (moment_pk,)).fetchone()["id"]
    st = get(conn, moment_pk)
    profile, model_url = None, None
    slug = (st or {}).get("model") or models.pick_for(cfg, conn, moment_pk)
    if slug:
        try:
            profile = models.load(cfg, slug)
            model_url = f"/media/mmd/{slug}/" + "/".join(quote(p) for p in profile["pmx"].split("/"))
        except models.ModelError:
            profile = None
    tr = None
    if st:
        try:
            _, key, have = track.current(conn, moment_pk, st)
            tr = {"ready": have is not None, "key": key,
                  "url": f"/media/stage/{moment_pk}/track?v={key}", "audio": f"/media/stage/{moment_pk}/audio?v={key}",
                  "warnings": (have or {}).get("warnings", [])}
        except track.TrackError as e:
            tr = {"ready": False, "error": str(e)}
    from chatwatcher.stage import run as stage_run
    return {"moment": moment_pk, "clip_id": clip_id, "video": f"/media/clip/{clip_id}?edit=1",
            "clip": {"duration": clip.duration_s, "zero": clip.zero_s, "width": clip.width, "height": clip.height},
            "stage": st, "guess": {"model": slug, "set_name": "room", "shape": "short"},
            "profile": profile, "model_url": model_url,
            "models": [{"slug": p["slug"], "name": p["name"], "vtuber": p.get("vtuber", ""),
                        "checked": p.get("checked", False)} for p in models.list_models(cfg)],
            "vocab": script.vocab(), "track": tr, "region": screen_region(conn, moment_pk, clip.width or 16, clip.height or 9),
            "render": render_state(conn, moment_pk, st), "thread": stage_run.thread(conn, moment_pk)[1],
            "claude": bool(cfg.claude.enabled)}
