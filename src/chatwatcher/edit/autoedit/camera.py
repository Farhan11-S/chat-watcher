"""The camera of an AI edit: Claude's shots ("p2-p4 | face | close | cut") as held
punch-ins and push-ins on the kept segments, re-framed so the picture never sits still."""

from __future__ import annotations

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


def camera_track(items: list[str], ps: list[pc.Piece], segs: list[list[float]],
                 boxes: dict[str, dict[str, float]], short: bool,
                 visible: set[str] | None = None) -> list[dict[str, Any]]:
    """Claude's shots as held punch-ins / push-ins, clipped to the kept segments, then
    re-framed wherever the picture would hold still longer than IDLE_MAX. In a Short, a
    shot on a region the layout doesn't show (visible: the ones it does) also gets a frame
    shot, so the picture moves there."""
    visible = set(boxes) if visible is None else visible
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
            if short and region != CENTRE and region not in visible:
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
