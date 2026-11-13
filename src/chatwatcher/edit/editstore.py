"""Loading and saving an edit (tables `edits` and `shorts`): everything the editor page
needs (load), a Short's layout, whether the rendered file is up to date (render_state),
and storing what the browser or Claude sends (save)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from chatwatcher.config import Config
from chatwatcher.edit import captions as cap
from chatwatcher.edit import clipref as cr
from chatwatcher.edit import layers as lyr
from chatwatcher.edit import layout as lay
from chatwatcher.edit import overlays as ovl
from chatwatcher.edit import spots as spt
from chatwatcher.edit import texts as txt
from chatwatcher.edit import motion, regions
from chatwatcher.edit.audio import analyse
from chatwatcher.edit.clipref import (KINDS, STAGE, TABLE, WAVEFORM_FILE, ClipRef, EditError, Segment, check_kind, clip_ref,
                                      whole)
from chatwatcher.edit.substate import sub_default, subs_rendered, subtitles, transcript_lines, transcript_state
from chatwatcher.edit.validate import clean_effects, clean_segments, short_regions, used_regions


def overlay_list(conn: sqlite3.Connection, moment_pk: int) -> list[dict[str, Any]]:
    """The overlay library and this clip's inserts, as the editor gets them."""
    return [{"id": o["id"], "name": o["name"], "description": o["description"], "length": o["duration_s"],
             "aspect": o["width"] / o["height"], "gain": o["gain_db"], "sound": bool(o["sound"]),
             "insert": o["moment_id"] is not None}
            for o in ovl.list_overlays(conn, moment_pk)]


def current_layout(conn: sqlite3.Connection, moment_pk: int, clip: ClipRef,
                   row: sqlite3.Row | None = None) -> dict[str, Any]:
    """The Short's saved layout, or a first one from the channel's regions."""
    boxes = regions.get(conn, regions.scope_for(conn, moment_pk)[0])
    first = lay.default(boxes, cr.meta(clip).get("stream_type"))
    saved = None
    if row is not None and row["layout_json"]:
        try:
            saved = json.loads(row["layout_json"])
        except json.JSONDecodeError:
            saved = None
    return lay.clean(saved, first) if saved else first


def load(cfg: Config, conn: sqlite3.Connection, moment_pk: int, kind: str = "long") -> dict[str, Any]:
    """Everything the editor needs, in clip time. A Short that was never saved starts from
    the whole clip, with the long edit's effects and subtitle fixes."""
    check_kind(kind)
    clip = clip_ref(conn, moment_pk)
    wf = analyse(clip.path, clip.folder / WAVEFORM_FILE)
    a = cr.latest_analysis(conn, moment_pk)
    row = cr.stored(conn, moment_pk, kind)
    base = row if row is not None or kind == "long" else cr.stored(conn, moment_pk, "long")
    segments = cr.to_clip(json.loads(row["segments_json"]), clip) if row else []
    source = row["source"] if row and segments else None
    if not segments:
        source, segments = "whole", whole(clip)
    shift = float((a.get("_meta") or {}).get("zero_s", clip.zero_s)) - clip.zero_s
    beats = [{"start": round(float(b.get("start", 0)) + shift, 2), "end": round(float(b.get("end", 0)) + shift, 2),
              "beat": b.get("beat", "")}
             for b in a.get("storyboard") or []]
    from chatwatcher.edit.sounds import list_sounds
    sounds = [{"id": x["id"], "name": x["name"], "description": x["description"], "length": x["duration_s"],
               "gain": x["gain_db"]} for x in list_sounds(conn)]
    scope, scope_label = regions.scope_for(conn, moment_pk)
    out = {
        "kind": kind, "duration": clip.duration_s, "fps": clip.fps, **wf.to_json(),
        "segments": segments, "source": source, "saved": bool(row),
        "effects": cr.effects_to_clip(cr.effects(base), clip), "sounds": sounds,
        "overlays": overlay_list(conn, moment_pk),
        "overlay_info": {"places": ovl.PLACES, "size": ovl.SIZE_RANGE, "offset": ovl.OFFSET_RANGE, "out": list(cr.size(clip)) if kind == "long"
                         else [lay.OUT_W, lay.OUT_H]},
        "regions": regions.get(conn, scope), "region_scope": scope_label,
        "text_info": {"styles": txt.STYLES, "colors": txt.COLORS, "default_color": txt.DEFAULT_COLOR,
                      "size": txt.SIZE_RANGE, "line": txt.LINE, "label_line": txt.LABEL_LINE, "max": txt.MAX_CHARS, "tilt": txt.STAMP_TILT,
                      "intro_ms": txt.INTRO_MS, "outro_ms": txt.OUTRO_MS,
                      "default_size": {st: txt.default_size(st, kind == "short") for st in txt.STYLES}},
        "layer_info": {"size": lyr.SIZE_RANGE, "opacity": lyr.OPACITY_RANGE, "default_size": lyr.DEFAULT_SIZE,
                       "default_opacity": lyr.DEFAULT_OPACITY, "fade": lyr.FADE},
        "spot_info": {"styles": spt.STYLES, "colors": {k: "%02X%02X%02X" % v for k, v in spt.COLORS.items()},
                      "default_color": spt.DEFAULT_COLOR, "dim": spt.DIM, "feather": spt.FEATHER, "ring": spt.RING,
                      "fade": [spt.FADE_IN, spt.FADE_OUT], "min": spt.MIN_SIZE},
        "visual": {"types": motion.TYPES | {lay.FRAME: lay.FRAME_LIMITS, ovl.OVERLAY: (1.0, 0.1, ovl.MAX_EFFECT_S),
                                            txt.TEXT: txt.D_LIMITS, spt.SPOT: spt.D_LIMITS,
                                            cap.CAPTION: cap.D_LIMITS, lyr.LAYER: lyr.D_LIMITS},
                   "transitions": motion.TRANSITIONS,
                   "transition_d": motion.TRANSITION_D, "zoom_range": motion.ZOOM_RANGE, "shake": motion.SHAKE_AMP,
                   "flash": motion.FLASH, "margin": motion.MARGIN, "waves": [motion.WAVES_X, motion.WAVES_Y]},
        "beats": beats, "lines": transcript_lines(clip.folder),
        "snap_s": cfg.edit.snap_s, "render": render_state(conn, moment_pk, clip, cfg=cfg, kind=kind),
        "transcript": transcript_state(cfg, conn, moment_pk, clip),
        "other": {k: render_state(conn, moment_pk, clip, cfg=cfg, kind=k)["state"] for k in KINDS if k != kind},
        **subtitles(cfg, conn, moment_pk, clip, kind, row=base),
    }
    if kind == "short":
        sw, sh = cr.size(clip)
        out |= {"layout": current_layout(conn, moment_pk, clip, row),
                "layout_info": {"types": lay.TYPES, "out": [lay.OUT_W, lay.OUT_H], "scale": lay.SCALE_RANGE,
                                "split": lay.SPLIT_RANGE, "max_title": lay.MAX_TITLE, "dark": lay.BG_DARK,
                                "blur_down": lay.BLUR_DOWN, "src": [sw, sh],
                                "frame": {"margin": lay.FRAME_MARGIN, "wide": lay.FRAME_WIDE,
                                          "limits": lay.FRAME_LIMITS}},
                "titles": [t for t in (a.get("titles") or []) if isinstance(t, str)][:3]}
    return out


def render_state(conn: sqlite3.Connection, moment_pk: int, clip: ClipRef | None = None,
                 cfg: Config | None = None, kind: str = "long") -> dict[str, Any]:
    """none | queued | running | done | stale (changed since the last render) | error."""
    check_kind(kind)
    if clip is None:
        try:
            clip = clip_ref(conn, moment_pk)
        except EditError:
            return {"state": "none"}
    job = conn.execute("SELECT status, error FROM jobs WHERE moment_id = ? AND stage = ?",
                       (moment_pk, STAGE[kind])).fetchone()
    row = cr.stored(conn, moment_pk, kind)
    out: dict[str, Any] = {"state": "none"}
    f = clip.out_path(kind)
    rendered = json.loads(row["rendered_json"]) if row and row["rendered_json"] else None
    if f.is_file() and rendered:
        same = (rendered.get("segments") == json.loads(row["segments_json"])
                and rendered.get("effects", []) == cr.effects(row)
                and subs_rendered(cfg, row, clip, kind) == (rendered.get("subs", []), rendered.get("subs_style")))
        if kind == "short":
            layout = current_layout(conn, moment_pk, clip, row)
            same = (same and rendered.get("layout") == layout
                    and rendered.get("regions", {}) == short_regions(conn, moment_pk, cr.effects(row), layout))
        else:
            same = same and rendered.get("regions", {}) == used_regions(conn, moment_pk, cr.effects(row))
            if same and cfg is not None:
                from chatwatcher.brand.service import edit_intro_key
                same = rendered.get("intro") == edit_intro_key(cfg, conn, moment_pk, clip)
        q = "?kind=short&" if kind == "short" else "?"
        if clip.srt_for(kind).is_file():
            out["srt"] = f"/media/edit-srt/{moment_pk}{q}v={int(clip.srt_for(kind).stat().st_mtime)}"
        out |= {"url": f"/media/edit/{moment_pk}{q}v={int(f.stat().st_mtime)}", "size": f.stat().st_size,
                "path": str(f), "length": rendered.get("duration_s"), "at": row["rendered_at"],
                "state": "done" if same else "stale"}
    if job and job["status"] in ("pending", "running"):
        out["state"] = "queued" if job["status"] == "pending" else "running"
        out["progress"] = (row["render_progress"] if row else None) or 0.0
        if job["status"] == "pending":
            busy = conn.execute("SELECT stage FROM jobs WHERE status = 'running' LIMIT 1").fetchone()
            out["behind"] = busy["stage"] if busy else None
    elif job and job["status"] == "error" and out["state"] != "done":
        out |= {"state": "error", "error": (job["error"] or "")[:600]}
    return out


def save(cfg: Config, conn: sqlite3.Connection, moment_pk: int, raw: Any, source: str | None = None,
         effects: Any = None, subs: Any = None, subs_style: Any = None, kind: str = "long",
         layout: Any = None) -> list[Segment]:
    """Store the browser's segments (clip time), and its effects unless effects is None
    (a new Short keeps the long edit's then).
    layout: a Short's layout (None = keep it; a new Short gets a first one)."""
    table = TABLE[check_kind(kind)]
    clip = clip_ref(conn, moment_pk)
    segs = clean_segments(raw, clip.duration_s)
    stream = [[round(s + clip.zero_s, 3), round(e + clip.zero_s, 3), g] for s, e, g in segs]
    existing = cr.stored(conn, moment_pk, kind)
    if effects is not None:
        known = {r["id"] for r in conn.execute("SELECT id FROM sounds")}
        ovs = {r["id"] for r in conn.execute("SELECT id FROM overlays")}
        fx = [f | {"t": round(f["t"] + clip.zero_s, 3)} for f in clean_effects(effects, clip.duration_s, known, ovs)]
    else:
        fx = cr.effects(existing if existing is not None or kind == "long" else cr.stored(conn, moment_pk, "long"))
    conn.execute(
        f"""INSERT INTO {table} (moment_id, segments_json, source, effects_json) VALUES (?, ?, ?, ?)
           ON CONFLICT (moment_id) DO UPDATE SET segments_json = excluded.segments_json,
               effects_json = excluded.effects_json,
               source = COALESCE(excluded.source, {table}.source), updated_at = datetime('now')""",
        (moment_pk, json.dumps(stream), source if source in ("long", "short", "whole") else None,
         json.dumps(fx, ensure_ascii=False)))
    if kind == "short" and (layout is not None or existing is None or not existing["layout_json"]):
        first = current_layout(conn, moment_pk, clip, existing)
        conn.execute("UPDATE shorts SET layout_json = ? WHERE moment_id = ?",
                     (json.dumps(lay.clean(layout, first) if layout is not None else first, ensure_ascii=False),
                      moment_pk))
    if existing is None and kind == "short" and subs is None and subs_style is None:
        long_row = cr.stored(conn, moment_pk, "long")           # a new Short keeps your subtitle fixes
        if long_row is not None:
            conn.execute("UPDATE shorts SET subs_json = ?, subs_style_json = ? WHERE moment_id = ?",
                         (long_row["subs_json"], long_row["subs_style_json"], moment_pk))
    if subs is not None or subs_style is not None:
        from chatwatcher.subs.lines import SubsError, clean, clean_style
        try:
            if subs == "auto":                       # follow Whisper's words again
                conn.execute(f"UPDATE {table} SET subs_json = NULL WHERE moment_id = ?", (moment_pk,))
            elif subs is not None:
                cues = [c | {"t0": round(c["t0"] + clip.zero_s, 3), "t1": round(c["t1"] + clip.zero_s, 3)}
                        for c in clean(subs, clip.duration_s)]
                conn.execute(f"UPDATE {table} SET subs_json = ? WHERE moment_id = ?",
                             (json.dumps(cues, ensure_ascii=False), moment_pk))
            if subs_style is not None:
                conn.execute(f"UPDATE {table} SET subs_style_json = ? WHERE moment_id = ?",
                             (json.dumps(clean_style(subs_style, sub_default(cfg, clip))), moment_pk))
        except SubsError as e:
            raise EditError(str(e), "invalid")
    return segs
