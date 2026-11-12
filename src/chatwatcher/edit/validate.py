"""Checking an edit: segments and effects from the browser or from Claude
(clean_segments, clean_effects), the regions its effects depend on, and the picture
effects made ready for the renderer (visuals, short_visuals, split_at)."""

from __future__ import annotations

import math
import re
import sqlite3
from typing import Any

from chatwatcher.edit import captions as cap
from chatwatcher.edit import layout as lay
from chatwatcher.edit import overlays as ovl
from chatwatcher.edit import spots as spt
from chatwatcher.edit import texts as txt
from chatwatcher.edit import motion, regions
from chatwatcher.edit.clipref import MIN_SEGMENT_S, EditError, Segment

MAX_SEGMENTS = 80           # the AI edit cuts every pause, so a clip can have many
MAX_EFFECTS = 120
GAIN_RANGE = (-30.0, 10.0)
DUCK_RANGE = (-30.0, 0.0)


def clean_segments(raw: Any, duration: float) -> list[Segment]:
    """Check segments from the browser (clip time). Raises EditError(kind='invalid')."""
    if not isinstance(raw, list) or not raw:
        raise EditError("The edit needs at least one segment.", "invalid")
    if len(raw) > MAX_SEGMENTS:
        raise EditError(f"At most {MAX_SEGMENTS} segments.", "invalid")
    out: list[Segment] = []
    for i, seg in enumerate(raw, 1):
        try:
            s, e = float(seg[0]), float(seg[1])
            g = float(seg[2]) if len(seg) > 2 else 0.0
        except (TypeError, ValueError, IndexError):
            raise EditError(f"Segment {i} isn't [start, end, gain].", "invalid")
        if not all(math.isfinite(v) for v in (s, e, g)):
            raise EditError(f"Segment {i} has a bad number.", "invalid")
        s, e = max(0.0, s), min(e, duration)
        if e - s < MIN_SEGMENT_S:
            raise EditError(f"Segment {i} is shorter than {MIN_SEGMENT_S:g} s.", "invalid")
        g = round(min(max(g, GAIN_RANGE[0]), GAIN_RANGE[1]), 1)
        out.append([round(s, 3), round(e, 3), g])
    if sum(e - s for s, e, _ in out) < 1:
        raise EditError("The edit must be at least 1 second long.", "invalid")
    return out


def clean_effects(raw: Any, duration: float, sounds: set[str],
                  overlays: set[str] | None = None) -> list[dict[str, Any]]:
    """Check effects from the browser or from Claude (clip time). Raises EditError(kind='invalid').
    overlays: the overlay library's ids (None: any id)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise EditError("Effects must be a list.", "invalid")
    if len(raw) > MAX_EFFECTS:
        raise EditError(f"At most {MAX_EFFECTS} effects.", "invalid")
    out, ids = [], set()
    for i, fx in enumerate(raw, 1):
        kind = fx.get("type", "sfx") if isinstance(fx, dict) else None
        if kind != "sfx" and kind not in motion.TYPES and kind not in (lay.FRAME, "transition", ovl.OVERLAY, txt.TEXT,
                                                                         spt.SPOT, cap.CAPTION):
            raise EditError(f"Effect {i} isn't a sound, zoom, shake, flash, overlay, text, spotlight or caption style.",
                            "invalid")
        try:
            t = float(fx["t"])
        except (KeyError, TypeError, ValueError):
            raise EditError(f"Effect {i} needs a time.", "invalid")
        if not math.isfinite(t) or not 0 <= t <= duration:
            raise EditError(f"Effect {i} is outside the clip.", "invalid")
        fid = re.sub(r"[^A-Za-z0-9_-]", "", str(fx.get("id") or ""))[:24] or f"fx{i}"
        while fid in ids:
            fid += "x"
        ids.add(fid)
        e: dict[str, Any] = {"id": fid, "type": kind, "t": round(t, 3)}
        if kind == "sfx":
            try:
                gain, duck = float(fx.get("gain", 0)), float(fx.get("duck", 0))
            except (TypeError, ValueError):
                raise EditError(f"Effect {i} has a bad volume.", "invalid")
            if not (math.isfinite(gain) and math.isfinite(duck)):
                raise EditError(f"Effect {i} has a bad volume.", "invalid")
            sound = str(fx.get("sound") or "")
            if sound not in sounds:
                raise EditError(f"Effect {i} uses a sound that isn't in your library ({sound or 'none'}).", "invalid")
            e |= {"sound": sound, "gain": round(min(max(gain, GAIN_RANGE[0]), GAIN_RANGE[1]), 1),
                  "duck": round(min(max(duck, DUCK_RANGE[0]), DUCK_RANGE[1]), 1)}
        elif kind == "transition":               # how the cut into the segment starting at t looks
            style = str(fx.get("style") or "")
            if style not in motion.TRANSITIONS:
                raise EditError(f"Effect {i} is an unknown transition ({style or 'none'}).", "invalid")
            try:
                d = float(fx.get("d") or motion.TRANSITIONS[style][0])
            except (TypeError, ValueError):
                raise EditError(f"Effect {i} has a bad length.", "invalid")
            if not math.isfinite(d):
                raise EditError(f"Effect {i} has a bad length.", "invalid")
            e |= {"style": style, "d": round(min(max(d, motion.TRANSITION_D[0]), motion.TRANSITION_D[1]), 3)}
        elif kind == ovl.OVERLAY:                # a green-screen clip over the picture, with its sound
            oid = str(fx.get("overlay") or "")
            if not ovl.valid_id(oid) or (overlays is not None and oid not in overlays):
                raise EditError(f"Effect {i} uses an overlay that isn't in your library ({oid or 'none'}).", "invalid")
            try:
                d, size, gain = float(fx.get("d", 1.0)), float(fx.get("size", 1.0)), float(fx.get("gain", 0))
                dx, dy = float(fx.get("dx") or 0), float(fx.get("dy") or 0)
            except (TypeError, ValueError):
                raise EditError(f"Effect {i} has a bad length, size, position or volume.", "invalid")
            if not all(math.isfinite(v) for v in (d, size, gain, dx, dy)):
                raise EditError(f"Effect {i} has a bad length, size, position or volume.", "invalid")
            at = str(fx.get("at") or "full")
            at = at if at in ovl.PLACES else (regions.normal_name(at) or "full")
            lo, hi = ovl.OFFSET_RANGE
            e |= {"overlay": oid, "d": round(min(max(d, 0.1), ovl.MAX_SECONDS), 3), "at": at,
                  "dx": round(min(max(dx, lo), hi), 4), "dy": round(min(max(dy, lo), hi), 4),
                  "size": round(min(max(size, ovl.SIZE_RANGE[0]), ovl.SIZE_RANGE[1]), 2),
                  "gain": round(min(max(gain, ovl.GAIN_RANGE[0]), ovl.GAIN_RANGE[1]), 1),
                  "mode": fx.get("mode") if fx.get("mode") in ovl.MODES else "both"}
        elif kind == spt.SPOT:                   # a spotlight on something in the clip's frame
            default, shortest, longest = spt.D_LIMITS
            try:
                d = float(fx.get("d", default))
                x, y, w, h = (float(fx[k]) for k in ("x", "y", "w", "h"))
            except (KeyError, TypeError, ValueError):
                raise EditError(f"Effect {i} is a spotlight without a place (x, y, w, h).", "invalid")
            if not all(math.isfinite(v) for v in (d, x, y, w, h)):
                raise EditError(f"Effect {i} has a bad length or place.", "invalid")
            w, h = min(max(w, spt.MIN_SIZE), 1.5), min(max(h, spt.MIN_SIZE), 1.5)
            style = fx.get("style") if fx.get("style") in spt.STYLES else "spotlight"
            e |= {"d": round(min(max(d, shortest), longest), 3), "style": style,
                  "color": fx.get("color") if fx.get("color") in spt.COLORS else spt.DEFAULT_COLOR[style],
                  "x": round(min(max(x, -w / 2), 1 - w / 2), 4), "y": round(min(max(y, -h / 2), 1 - h / 2), 4),
                  "w": round(w, 4), "h": round(h, 4)}
        elif kind == txt.TEXT:                   # a text callout over the picture
            text = txt.clean_text(fx.get("text") or "")
            if not text:
                raise EditError(f"Effect {i} is a text with nothing to say.", "invalid")
            default, shortest, longest = txt.D_LIMITS
            try:
                d, dx, dy = float(fx.get("d", default)), float(fx.get("dx") or 0), float(fx.get("dy") or 0)
                size = None if fx.get("size") is None else float(fx["size"])
            except (TypeError, ValueError):
                raise EditError(f"Effect {i} has a bad length, size or position.", "invalid")
            if not all(math.isfinite(v) for v in (d, dx, dy, size if size is not None else 0)):
                raise EditError(f"Effect {i} has a bad length, size or position.", "invalid")
            style = fx.get("style") if fx.get("style") in txt.STYLES else "pop"
            at = str(fx.get("at") or "middle")
            at = at if at in ovl.PLACES else (regions.normal_name(at) or "middle")
            lo, hi = ovl.OFFSET_RANGE
            e |= {"text": text, "style": style, "d": round(min(max(d, shortest), longest), 3),
                  "color": fx.get("color") if fx.get("color") in txt.COLORS else txt.DEFAULT_COLOR[style],
                  "at": at, "dx": round(min(max(dx, lo), hi), 4), "dy": round(min(max(dy, lo), hi), 4)}
            if size is not None:
                e["size"] = round(min(max(size, txt.SIZE_RANGE[0]), txt.SIZE_RANGE[1]), 3)
        elif kind == cap.CAPTION:                # how the captions starting in [t, t + d) look
            default, shortest, longest = cap.D_LIMITS
            try:
                d = float(fx.get("d", default))
            except (TypeError, ValueError):
                raise EditError(f"Effect {i} has a bad length.", "invalid")
            if not math.isfinite(d):
                raise EditError(f"Effect {i} has a bad length.", "invalid")
            at = str(fx.get("at") or "")
            at = at if at in ("", *cap.PLACES) else (regions.normal_name(at) or "")
            e |= {"d": round(min(max(d, shortest), longest), 3), "fx": fx.get("fx") if fx.get("fx") in cap.FX else "pop",
                  "font": fx.get("font") if fx.get("font") in cap.FONTS else "", "at": at,
                  "size": fx.get("size") if fx.get("size") in cap.SIZES else ""}
        elif kind == lay.FRAME:                  # a Short shows this region full-screen for d seconds
            default, shortest, longest = lay.FRAME_LIMITS
            try:
                d = float(fx.get("d", default))
            except (TypeError, ValueError):
                raise EditError(f"Effect {i} has a bad length.", "invalid")
            if not math.isfinite(d):
                raise EditError(f"Effect {i} has a bad length.", "invalid")
            e |= {"d": round(min(max(d, shortest), longest), 3), "region": regions.normal_name(fx.get("region"))}
        else:
            default, shortest, longest = motion.TYPES[kind]
            try:
                d = float(fx.get("d", default))
                zoom = float(fx.get("zoom", 1.5 if kind == "punch" else 1.25))
                strength = int(fx.get("strength", 2))
            except (TypeError, ValueError):
                raise EditError(f"Effect {i} has a bad setting.", "invalid")
            if not (math.isfinite(d) and math.isfinite(zoom)):
                raise EditError(f"Effect {i} has a bad setting.", "invalid")
            e["d"] = round(min(max(d, shortest), longest), 3)
            if kind in motion.ZOOMS:
                e |= {"region": regions.normal_name(fx.get("region")),
                      "zoom": round(min(max(zoom, motion.ZOOM_RANGE[0]), motion.ZOOM_RANGE[1]), 2)}
            elif kind == "shake":
                e["strength"] = min(max(strength, 1), 3)
            else:
                color = str(fx.get("color") or "white")
                e["color"] = color if color in motion.FLASH else "white"
        e |= {"by": "claude" if fx.get("by") == "claude" else "you", "why": str(fx.get("why") or "")[:300]}
        out.append(e)
    return sorted(out, key=lambda fx: fx["t"])


def used_regions(conn: sqlite3.Connection, moment_pk: int,
                  effects: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """The boxes of the regions these effects aim at (as they are now)."""
    names = {fx.get("region") for fx in effects if fx.get("type") in motion.ZOOMS and fx.get("region")}
    names |= {fx.get("at") for fx in effects if fx.get("type") in (ovl.OVERLAY, txt.TEXT, cap.CAPTION)
              and fx.get("at") and fx.get("at") not in (*ovl.PLACES, *cap.PLACES)}
    if not names:
        return {}
    have = regions.get(conn, regions.scope_for(conn, moment_pk)[0])
    return {n: have[n] for n in sorted(names) if n in have}


def short_regions(conn: sqlite3.Connection, moment_pk: int, effects: list[dict[str, Any]],
                   layout: dict[str, Any]) -> dict[str, dict[str, float]]:
    """The boxes a Short depends on: those its zooms aim at, and its stack panels."""
    have = regions.get(conn, regions.scope_for(conn, moment_pk)[0])
    names = {fx.get("region") for fx in effects if fx.get("type") in (*motion.ZOOMS, lay.FRAME) and fx.get("region")}
    names |= {fx.get("at") for fx in effects if fx.get("type") in (ovl.OVERLAY, txt.TEXT, cap.CAPTION)
              and fx.get("at") and fx.get("at") not in (*ovl.PLACES, *cap.PLACES)}
    names |= lay.used_regions(layout)
    return {n: have[n] for n in sorted(names) if n in have}


def visuals(effects: list[dict[str, Any]], boxes: dict[str, dict[str, float]]) -> tuple[list[motion.Visual], set[str]]:
    """Picture effects ready for the renderer, and the region names that don't exist
    (those zooms aim at the centre)."""
    out, missing = [], set()
    for fx in effects:
        kind = fx.get("type")
        if kind not in motion.TYPES:
            continue
        v = motion.Visual(kind, fx["t"], fx["d"], strength=fx.get("strength", 2), color=fx.get("color", "white"))
        if kind in motion.ZOOMS:
            v.zoom = fx.get("zoom", 1.5)
            box = boxes.get(fx.get("region") or "")
            if box:
                v.cx, v.cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
            elif fx.get("region"):
                missing.add(fx["region"])
        out.append(v)
    return out, missing


def short_visuals(effects: list[dict[str, Any]], boxes: dict[str, dict[str, float]],
                  ps: list[lay.Panel]) -> tuple[list[motion.Visual], set[str]]:
    """visuals() for a Short: zooms aim at where their region lands on the 9:16 frame (its
    centre when the region isn't drawn or isn't on screen)."""
    out, missing = visuals(effects, boxes)
    for fx, v in zip([f for f in effects if f.get("type") in motion.TYPES], out):
        if v.type in motion.ZOOMS:
            at = lay.map_point(v.cx, v.cy, ps) if boxes.get(fx.get("region") or "") else None
            v.cx, v.cy = at or (0.5, 0.5)
    return out, missing


def split_at(segs: list[Segment], times: list[float], fps: float) -> list[Segment]:
    """Segments cut (on whole frames, and not into slivers) at these clip times; the parts
    still play back to back."""
    out: list[Segment] = []
    for s, e, g in segs:
        cuts = sorted({round(s + round((t - s) * fps) / fps, 3) for t in times
                       if s + MIN_SEGMENT_S < t < e - MIN_SEGMENT_S})
        edges = [s, *cuts, e]
        out += [[a, b, g] for a, b in zip(edges, edges[1:])]
    return out
