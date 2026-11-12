"""Claude's picks → editor effects: sounds, overlays, text callouts, spotlights, shakes
and flashes, transitions and caption styles. Each builder keeps what lands inside the cut,
snaps it to a loud hit where that fits, and clamps it to what the editor allows."""

from __future__ import annotations

import math
import re
import secrets
from typing import Any

from chatwatcher.edit import captions as cap
from chatwatcher.edit import overlays as ovl
from chatwatcher.edit import spots as spt
from chatwatcher.edit import texts as txt
from chatwatcher.edit import motion, pieces as pc, service
from chatwatcher.edit.audio import snap
from chatwatcher.edit.autoedit.plan import (CENTRE, MAX_CAPTIONS, MAX_OVERLAYS, MAX_SPOTS, MAX_TEXTS, STRENGTHS, parse_range,
                                           parts)

HIT_SNAP_S = 1.0
KINDS = {"punch_in": "punch", "push_in": "push", "shake": "shake", "flash": "flash"}
CAPTION_LEAD = 0.05      # a caption style starts this long before its first word
SPOT_PAD = 0.15         # room round a region a spotlight is put round


def _inside(t: float, segs: list[list[float]], margin: float = 0.0) -> int | None:
    return next((i for i, (s, e, *_) in enumerate(segs) if s - margin <= t < e + margin), None)


def sound_effects(items: list[str], segs: list[list[float]], hits: list[tuple[float, float]],
                  known: set[str], limit: int) -> list[dict[str, Any]]:
    out = []
    for item in items:
        bits = parts(item)
        try:
            t = float(bits[0])
            gain = float(bits[2]) if len(bits) > 2 and bits[2] else 0.0
            duck = float(bits[3]) if len(bits) > 3 and bits[3] else 0.0
        except (ValueError, IndexError):
            continue
        sound = bits[1] if len(bits) > 1 else ""
        k = _inside(t, segs)
        if k is None or sound not in known:
            continue
        snapped = snap(t, hits, HIT_SNAP_S)
        if _inside(snapped, segs) == k:
            t = snapped
        out.append({"id": "c" + secrets.token_hex(3), "type": "sfx", "t": round(t, 3), "sound": sound,
                    "gain": min(max(gain, -20.0), 6.0), "duck": min(max(duck, -20.0), 0.0), "by": "claude",
                    "why": " | ".join(bits[4:])[:300]})
    return out[:limit]


def overlay_effects(items: list[str], segs: list[list[float]], hits: list[tuple[float, float]],
                    library: list[dict[str, Any]], boxes: dict[str, dict[str, float]], out: tuple[int, int],
                    limit: int = MAX_OVERLAYS) -> list[dict[str, Any]]:
    """Claude's overlays as editor effects: inside the cut, snapped to a loud hit close by,
    as long as the overlay, centred where Claude said (full / middle / a drawn region)."""
    known = {o["id"]: o for o in library}
    res = []
    for item in items:
        bits = parts(item)
        try:
            t = float(bits[0])
        except (ValueError, IndexError):
            continue
        ov = known.get(bits[1] if len(bits) > 1 else "")
        k = _inside(t, segs)
        if ov is None or k is None:
            continue
        snapped = snap(t, hits, HIT_SNAP_S)
        if _inside(snapped, segs) == k:
            t = snapped
        at = bits[2].lower() if len(bits) > 2 else "full"
        at = at if at in ovl.PLACES or at in boxes else "full"
        res.append({"id": "c" + secrets.token_hex(3), "type": ovl.OVERLAY, "overlay": ov["id"], "t": round(t, 3),
                    "d": ov["duration_s"], "at": at, "size": ovl.default_size(at, ov, *out), "gain": 0.0,
                    "by": "claude", "why": " | ".join(bits[3:])[:300]})
    return res[:limit]


def text_effects(items: list[str], segs: list[list[float]], boxes: dict[str, dict[str, float]], short: bool,
                 limit: int = MAX_TEXTS) -> list[dict[str, Any]]:
    """Claude's text callouts as editor effects: inside the cut (cut short at its end),
    styled and placed where Claude said (middle or a drawn region)."""
    res = []
    for item in items:
        bits = parts(item)
        try:
            t, d = float(bits[0]), float(bits[1])
        except (ValueError, IndexError):
            continue
        text = txt.clean_text(bits[2] if len(bits) > 2 else "")
        k = _inside(t, segs)
        if not text or k is None or not math.isfinite(d):
            continue
        default, shortest, longest = txt.D_LIMITS
        d = min(max(d, shortest), longest, max(segs[k][1] - t, shortest))
        style = bits[3].lower() if len(bits) > 3 and bits[3].lower() in txt.STYLES else "pop"
        at = bits[4].lower() if len(bits) > 4 else "middle"
        at = at if at in boxes else "middle"
        res.append({"id": "c" + secrets.token_hex(3), "type": txt.TEXT, "t": round(t, 3), "d": round(d, 3),
                    "text": text, "style": style, "color": txt.DEFAULT_COLOR[style], "at": at, "dx": 0.0, "dy": 0.0,
                    "size": txt.default_size(style, short), "by": "claude", "why": " | ".join(bits[5:])[:300]})
    return res[:limit]


_SPOT_BOX = re.compile(r"x\s*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\D+?y\s*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)", re.I)


def spot_target(target: str, boxes: dict[str, dict[str, float]]) -> dict[str, float] | None:
    """A spotlight's box in the clip's frame: round a region (with SPOT_PAD of room), or
    "x 40-60 y 20-35" (percent of the frame). None when it's neither."""
    b = boxes.get(target.strip().lower())
    if b:
        return {"x": b["x"] - b["w"] * SPOT_PAD, "y": b["y"] - b["h"] * SPOT_PAD,
                "w": b["w"] * (1 + 2 * SPOT_PAD), "h": b["h"] * (1 + 2 * SPOT_PAD)}
    m = _SPOT_BOX.search(target)
    if not m:
        return None
    x0, x1, y0, y1 = sorted(float(v) / 100 for v in m.groups()[:2]) + sorted(float(v) / 100 for v in m.groups()[2:])
    if x1 - x0 < 0.01 or y1 - y0 < 0.01 or x1 > 1.05 or y1 > 1.05:
        return None
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def spot_effects(items: list[str], segs: list[list[float]], boxes: dict[str, dict[str, float]],
                 limit: int = MAX_SPOTS) -> list[dict[str, Any]]:
    """Claude's spotlights as editor effects: inside the cut (cut short at its end), round a
    region or the box Claude read off the frames."""
    res = []
    for item in items:
        bits = parts(item)
        try:
            t, d = float(bits[0]), float(bits[1])
        except (ValueError, IndexError):
            continue
        box = spot_target(bits[2], boxes) if len(bits) > 2 else None
        k = _inside(t, segs)
        if box is None or k is None or not math.isfinite(d):
            continue
        default, shortest, longest = spt.D_LIMITS
        d = min(max(d, shortest), longest, max(segs[k][1] - t, shortest))
        style = bits[3].lower() if len(bits) > 3 and bits[3].lower() in spt.STYLES else "spotlight"
        res.append({"id": "c" + secrets.token_hex(3), "type": spt.SPOT, "t": round(t, 3), "d": round(d, 3),
                    "style": style, "color": spt.DEFAULT_COLOR[style], **{k: round(v, 4) for k, v in box.items()},
                    "by": "claude", "why": " | ".join(bits[4:])[:300]})
    return res[:limit]


def hit_effects(items: list[str], segs: list[list[float]], hits: list[tuple[float, float]],
                boxes: dict[str, dict[str, float]], duration: float, limit: int) -> list[dict[str, Any]]:
    raw = []
    for item in items:
        bits = parts(item)
        if len(bits) < 2 or bits[1].lower() not in ("shake", "flash"):
            continue
        try:
            t = float(bits[0])
        except ValueError:
            continue
        kind = bits[1].lower()
        opt = bits[2].lower() if len(bits) > 2 else ""
        raw.append({"kind": kind, "at": t, "length_s": 0.6 if kind == "shake" else 0.3, "region": CENTRE,
                    "zoom": 1.05, "strength": opt if opt in ("light", "normal", "heavy") else "normal",
                    "color": opt if opt in motion.FLASH else "white", "why": " | ".join(bits[3:])})
    fx, _dropped = _visuals(raw, segs, hits, boxes, duration)
    return fx[:limit]


def _visuals(items: list[dict[str, Any]], segs: list[list[float]], hits: list[tuple[float, float]],
             boxes: dict[str, dict[str, float]], duration: float) -> tuple[list[dict[str, Any]], int]:
    """Claude's camera effects as editor effects: inside the cut, snapped to hits, aimed
    only at regions that exist. Returns (effects, how many were dropped)."""
    out, dropped = [], 0
    for item in items:
        kind = KINDS.get(str(item.get("kind")))
        try:
            t, d = float(item["at"]), float(item.get("length_s") or 0)
        except (KeyError, TypeError, ValueError):
            dropped += 1
            continue
        k = _inside(t, segs)
        if kind is None or k is None:
            dropped += 1
            continue
        if kind != "push":                       # these land on a moment; a push-in ends on one
            snapped = snap(t, hits, HIT_SNAP_S)
            if _inside(snapped, segs) == k:
                t = snapped
        default, shortest, longest = motion.TYPES[kind]
        fx: dict[str, Any] = {"id": "c" + secrets.token_hex(3), "type": kind, "t": round(t, 3),
                              "d": round(min(max(d or default, shortest), longest, max(duration - t, shortest)), 3),
                              "by": "claude", "why": str(item.get("why") or "")}
        if kind in motion.ZOOMS:
            region = str(item.get("region") or "")
            fx["region"] = region if region in boxes else ""
            try:
                zoom = float(item.get("zoom") or 0)
            except (TypeError, ValueError):
                zoom = 0
            if kind == "punch" and zoom < 1.1:           # "fill the frame with the region"
                zoom = motion.fit_zoom(boxes[fx["region"]]) if fx["region"] else 1.5
            fx["zoom"] = round(min(max(zoom or 1.25, motion.ZOOM_RANGE[0]), motion.ZOOM_RANGE[1]), 2)
        elif kind == "shake":
            fx["strength"] = STRENGTHS.get(str(item.get("strength")), 2)
        else:
            fx["color"] = str(item.get("color")) if item.get("color") in motion.FLASH else "white"
        out.append(fx)
    return out, dropped


def transition_effects(items: list[str], ps: list[pc.Piece], segs: list[list[float]], cold_open: bool,
                       limit: int) -> list[dict[str, Any]]:
    """Claude's transitions on the real cuts: "p14 | whip | why" goes on the cut into the
    segment that starts inside p14 (or nearest to it). A cold open's cut into the story
    gets a flash if Claude gave it nothing."""
    cuts = [i for i in range(1, len(segs)) if abs(segs[i][0] - segs[i - 1][1]) >= 0.002]
    out: dict[int, dict[str, Any]] = {}
    for item in items:
        bits = parts(item)
        rng = parse_range(bits[0], len(ps)) if bits else []
        style = bits[1].lower() if len(bits) > 1 else ""
        if not rng or style not in motion.TRANSITIONS or not cuts:
            continue
        a, b = ps[rng[0]].t0, ps[rng[-1]].t1
        i = min(cuts, key=lambda k: 0 if a - 0.3 <= segs[k][0] < b else min(abs(segs[k][0] - a), abs(segs[k][0] - b)))
        if not (a - 0.3 <= segs[i][0] < b) or i in out:      # no real cut into that piece: skip it
            continue
        out[i] = {"id": "c" + secrets.token_hex(3), "type": "transition", "t": round(segs[i][0], 3), "style": style,
                  "d": motion.TRANSITIONS[style][0], "by": "claude", "why": " | ".join(bits[2:])[:300]}
    if cold_open and 1 in cuts and 1 not in out:
        out[1] = {"id": "c" + secrets.token_hex(3), "type": "transition", "t": round(segs[1][0], 3), "style": "flash",
                  "d": motion.TRANSITIONS["flash"][0], "by": "claude", "why": "from the teaser into the story"}
    return [out[k] for k in sorted(out)][:limit]


_SPAN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*$")


def caption_effects(items: list[str], ps: list[pc.Piece], segs: list[list[float]],
                    boxes: dict[str, dict[str, float]], duration: float,
                    limit: int = MAX_CAPTIONS) -> list[dict[str, Any]]:
    """Claude's caption styles as editor effects: a stretch of pieces (from just before the
    first word to the last) or of clip seconds, inside the cut, with a known effect, font and place."""
    res = []
    for item in items:
        bits = parts(item)
        span = _SPAN.match(bits[0]) if bits else None
        if span:
            t0, t1 = sorted((float(span[1]), float(span[2])))
        else:
            ids = parse_range(bits[0] if bits else "", len(ps))
            if not ids:
                continue
            first, last = ps[ids[0]], ps[ids[-1]]
            t0 = (first.w0 if first.kind == "talk" else first.t0) - CAPTION_LEAD
            t1 = last.w1 if last.kind == "talk" else last.t1
        t0 = max(0.0, t0)
        t1 = min(t1, duration)
        if not any(s < t1 and t0 < e for s, e, *_ in segs) or t1 - t0 < cap.D_LIMITS[1]:
            continue
        fx = bits[1].lower() if len(bits) > 1 and bits[1].lower() in cap.FX else "pop"
        font = bits[2].lower() if len(bits) > 2 and bits[2].lower() in cap.FONTS else ""
        at = bits[3].strip().strip('"').lower() if len(bits) > 3 else ""
        at = at if at in cap.PLACES or at in boxes else ""
        size = bits[4].lower() if len(bits) > 4 else ""        # an answer without a size: the why comes next
        why = bits[5:] if size in (*cap.SIZES, "", "normal", "usual") else bits[4:]
        res.append({"id": "c" + secrets.token_hex(3), "type": cap.CAPTION, "t": round(t0, 3),
                    "d": round(min(t1 - t0, cap.D_LIMITS[2]), 3), "fx": fx, "font": "" if font == "round" else font,
                    "at": at, "size": size if size in cap.SIZES else "", "by": "claude",
                    "why": " | ".join(why)[:300]})
    return res[:limit]


def captions(words: dict[str, Any] | None, emphasis: list[str]) -> list[dict[str, Any]]:
    """Word-by-word cues with Claude's emphasis words *highlighted*."""
    return service.auto_cues(words, "short", {"mode": "words", "emphasis": emphasis})
