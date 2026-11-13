"""The camera of an AI edit: Claude's shots ("p2-p4 | face | close | cut") as held
punch-ins and push-ins on the kept segments, re-framed so the picture never sits still;
and, in a Short, its layouts ("p5-p12 | chat / face | 55"): the picture for a stretch,
when the scene changes (the game feed goes away, she's chatting now)."""

from __future__ import annotations

import math
import secrets
from typing import Any

from chatwatcher.edit import layout as lay
from chatwatcher.edit import motion, pieces as pc
from chatwatcher.edit.autoedit.plan import CENTRE, parse_range, parts

SHOT_SNAP = 0.35         # a shot this close to a part's edge starts / ends with the part
IDLE_MAX = 2.5           # the picture changes at least this often
REFRAME = 1.22           # a long shot's re-frame: this much tighter (enough to read as a new shot)
WIDE_PUSH = 1.08         # a long wide stretch creeps in this far…
WIDE_PUSH_S = 12.0       # …over at most this long
LEVELS = ("wide", "medium", "close", "tight")
MOVES = ("cut", "push")


# ── the camera ───────────────────────────────────────────────────────────────

def _zoom(level: str, box: dict[str, float] | None, short: bool) -> float:
    if level == "wide":
        return 1.0
    if short or not box:                           # a Short's layout already frames the face
        z = {"medium": 1.2, "close": 1.45, "tight": 1.8}[level]
    else:
        fit = motion.fit_zoom(box)
        z = {"medium": 1 + (fit - 1) * 0.5, "close": fit, "tight": fit * 1.3}[level]
        z = max(z, {"medium": 1.15, "close": 1.3, "tight": 1.6}[level])
    return round(min(max(z, motion.ZOOM_RANGE[0]), 3.0), 2)


def _clip_to(t0: float, t1: float, segs: list[list[float]]) -> list[tuple[float, float]]:
    """[t0, t1) inside each kept segment, snapped to the segment's edges when close."""
    out = []
    for s, e, *_ in segs:
        a, b = max(t0, s), min(t1, e)
        if b - a < 0.2:
            continue
        out.append((s if a - s < SHOT_SNAP else a, e if e - b < SHOT_SNAP else b))
    return out


def layout_effects(items: list[str], ps: list[pc.Piece], segs: list[list[float]],
                   boxes: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
    """A Short's layouts for stretches of pieces, as frame shots: "p5-p12 | top / bottom |
    split %" stacks two regions (top may be "whole" for the whole frame), "p5-p12 | region"
    shows one full-screen, "p5-p12 | full" the whole picture (fit + blur)."""
    out: list[dict[str, Any]] = []
    longest = lay.FRAME_LIMITS[2]
    for item in items:
        bits = parts(item)
        rng = parse_range(bits[0], len(ps)) if bits else []
        if not rng or len(bits) < 2:
            continue
        what = bits[1].lower()
        if "/" in what:
            top, bottom = (x.strip() for x in what.split("/", 1))
            top = "" if top in ("whole", "whole frame", "", lay.FULL) else top
            bottom = "" if bottom in ("whole", "whole frame", "", lay.FULL) else bottom
            if (top and top not in boxes) or (bottom and bottom not in boxes) or not (top or bottom):
                continue
            try:
                split = float(bits[2].rstrip("%")) / 100 if len(bits) > 2 and bits[2] else 0.6
            except ValueError:
                split = 0.6
            split = min(max(split if math.isfinite(split) else 0.6, lay.SPLIT_RANGE[0]), lay.SPLIT_RANGE[1])
            shot = {"region": "", "top": top, "bottom": bottom, "split": round(split, 3)}
            why = f"layout: {top or 'whole frame'} over {bottom or 'whole frame'}"
        elif what == lay.FULL or what in boxes:
            shot, why = {"region": what}, f"layout: {what}"
        else:
            continue
        for a, b in _clip_to(ps[rng[0]].t0, ps[rng[-1]].t1, segs):
            t = a
            while b - t > 0.2:                       # longer than a frame shot may last: a chain of them
                d = min(longest, b - t)
                out.append({"id": "c" + secrets.token_hex(3), "type": lay.FRAME, "t": round(t, 3), "d": round(d, 3),
                            **shot, "by": "claude", "why": why})
                t += d
    return out


def _visible_at(t: float, scenes: list[dict[str, Any]], boxes: dict[str, dict[str, float]],
                visible: set[str], size: tuple[int, int]) -> set[str]:
    """The regions on screen at t: the layout's, or the frame shot's showing then."""
    fr = lay.frame_at(t, scenes)
    ps = lay.shot_panels(fr, boxes, *size) if fr else None
    if not ps:
        return visible
    return {n for n, b in boxes.items() if lay.map_point(b["x"] + b["w"] / 2, b["y"] + b["h"] / 2, ps)}


def camera_track(items: list[str], ps: list[pc.Piece], segs: list[list[float]],
                 boxes: dict[str, dict[str, float]], short: bool,
                 visible: set[str] | None = None, scenes: list[dict[str, Any]] | None = None,
                 size: tuple[int, int] = (1920, 1080)) -> list[dict[str, Any]]:
    """Claude's shots as held punch-ins / push-ins, clipped to the kept segments, then
    re-framed wherever the picture would hold still longer than IDLE_MAX. In a Short, a
    shot on a region the picture doesn't show then (visible: the ones the layout shows;
    scenes: frame shots from layout_effects, which change that; size: the clip's) also
    gets a frame shot, so the picture moves there."""
    visible = set(boxes) if visible is None else visible
    scenes = scenes or []
    shots: list[tuple[float, float, str, str, str]] = []           # t0, t1, region, level, move
    for item in items:
        bits = parts(item)
        rng = parse_range(bits[0], len(ps)) if bits else []
        if not rng or len(bits) < 3:
            continue
        region = bits[1].lower() if bits[1].lower() in boxes else CENTRE
        level = bits[2].lower() if bits[2].lower() in LEVELS else "medium"
        if bits[1].lower() == lay.FULL:          # the whole picture: fit + blur in a Short, wide in 16:9
            region, level = (lay.FULL if short else CENTRE), "wide"
        move = bits[3].lower() if len(bits) > 3 and bits[3].lower() in MOVES else "cut"
        shots.append((ps[rng[0]].t0, ps[rng[-1]].t1, region, level, move))
    out: list[dict[str, Any]] = []

    def add(kind: str, t: float, d: float, region: str, zoom: float, why: str) -> None:
        if d < 0.2 or zoom <= 1.0:
            return
        default, shortest, longest = motion.TYPES[kind]
        out.append({"id": "c" + secrets.token_hex(3), "type": kind, "t": round(t, 3),
                    "d": round(min(max(d, shortest), longest), 3), "region": "" if region == CENTRE else region,
                    "zoom": zoom, "by": "claude", "why": why})

    for s, e, _ in segs:
        cursor = s
        for t0, t1, region, level, move in sorted(shots):
            a, b = max(t0, s), min(t1, e)
            if b - a < 0.2:
                continue
            a = s if a - s < SHOT_SNAP else a          # no sliver of the old picture at a part's edge
            b = e if e - b < SHOT_SNAP else b
            if short and region != CENTRE and region not in _visible_at(a, scenes, boxes, visible, size):
                out.append({"id": "c" + secrets.token_hex(3), "type": lay.FRAME, "t": round(a, 3),
                            "d": round(min(max(b - a, lay.FRAME_LIMITS[1]), lay.FRAME_LIMITS[2]), 3),
                            "region": region, "by": "claude", "why": f"show {region}"})
            zoom = _zoom(level, boxes.get(region), short)
            if zoom <= 1.0:
                cursor = max(cursor, b)
                continue
            kind = "push" if move == "push" else "punch"
            if kind == "push":
                add("push", a, b - a, region, zoom, f"push in on {region}")
            else:                                   # long holds get a small re-frame
                t, flip = a, False
                while t < b - 0.05:
                    d = min(IDLE_MAX - 0.4, b - t)
                    if b - (t + d) < 0.8:
                        d = b - t
                    add("punch", t, d, region, round(min(zoom * REFRAME, 3.0), 2) if flip else zoom,
                        f"{level} on {region}")
                    t, flip = t + d, not flip
            cursor = max(cursor, b)
    # Wide stretches that run long get a slow push-in on the centre, so nothing sits still.
    for s, e, _ in segs:
        busy = sorted((fx["t"], fx["t"] + fx["d"]) for fx in out
                      if fx["type"] in motion.ZOOMS and fx["t"] < e and fx["t"] + fx["d"] > s)
        t = s
        for a, b in busy + [(e, e)]:
            gap0, gap1 = t, min(a, e)
            while gap1 - gap0 > IDLE_MAX:         # one slow creep, not a snap every few seconds
                d = min(WIDE_PUSH_S, gap1 - gap0)
                add("push", gap0, d, CENTRE, WIDE_PUSH, "slow push so the frame doesn't sit still")
                gap0 += d
            t = max(t, b)
    # A teaser repeats clip time that the story also plays, so a shot can come up twice.
    seen, unique = set(), []
    for fx in sorted(out, key=lambda fx: fx["t"]):
        key = (fx["type"], fx["t"], fx["region"])
        if key not in seen:
            seen.add(key)
            unique.append(fx)
    return unique
