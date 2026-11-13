"""Picture effects of an edit: punch-in, push-in, shake and flash.

camera() says which part of the frame is on screen at a moment. The renderer turns the
same maths into ffmpeg expressions (view_exprs), and the editor's preview in editor.js
mirrors it, so the browser shows what edit.mp4 will show. Change all three together.

A view is a centre (xc, yc) and a half-size h, as fractions of the frame: the whole
frame is (0.5, 0.5, 0.5); a 2× zoom on a face is (the face's centre, 0.25). The view
always stays inside the frame, so no black edges show.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

# type: (default length, shortest, longest) in seconds
TYPES: dict[str, tuple[float, float, float]] = {
    "punch": (1.5, 0.2, 10.0),     # instant zoom on a region, then straight back
    "push": (3.0, 0.5, 15.0),      # slow zoom towards a region, eased; back at the end
    "shake": (0.6, 0.1, 3.0),      # camera shake, strongest at the start
    "flash": (0.3, 0.05, 1.5),     # a colour over the picture, fading out
}
ZOOMS = ("punch", "push", "pull")     # pull: starts at zoom / (cx, cy) and eases back (transitions only)
ZOOM_RANGE = (1.05, 4.0)
SHAKE_AMP = {1: 0.007, 2: 0.014, 3: 0.028}       # light, normal, heavy: fraction of the frame
FLASH = {"white": ((255, 255, 255), 0.85), "red": ((255, 30, 30), 0.5), "black": ((0, 0, 0), 0.85)}
MARGIN = 2.2                                     # extra zoom per unit of shake, so edges never show
WAVES_X = ((0.6, 9.3, 0.0), (0.4, 15.7, 1.1))    # shake wobble: (weight, Hz, phase)
WAVES_Y = ((0.6, 11.1, 0.7), (0.4, 17.3, 2.3))


@dataclass
class Visual:
    """One picture effect. Times are in whatever timeline the caller works in."""
    type: str
    t: float
    d: float
    zoom: float = 1.0              # punch: the zoom; push: the zoom it ends at
    cx: float = 0.5                # punch/push: the region's centre
    cy: float = 0.5
    strength: int = 2              # shake: 1 light, 2 normal, 3 heavy
    color: str = "white"           # flash
    box: tuple[float, float, float, float] | None = None   # punch/push in a Short with two pictures
    # (a stack): the panel it zooms inside, x, y, w, h as fractions of the Short's frame; cx, cy
    # are then fractions of that panel. None: the whole picture.

    def active(self, t: float) -> bool:
        return self.t <= t < self.t + self.d


def fit_zoom(box: dict[str, float]) -> float:
    """The zoom that makes a region fill the frame."""
    z = min(1 / max(box["w"], 1e-3), 1 / max(box["h"], 1e-3))
    return round(min(max(z, ZOOM_RANGE[0]), ZOOM_RANGE[1]), 2)


def _smooth(q: float) -> float:
    return q * q * (3 - 2 * q)


def _wave(tau: float, waves) -> float:
    return sum(w * math.sin(2 * math.pi * hz * tau + ph) for w, hz, ph in waves)


def camera(t: float, visuals: list[Visual]) -> tuple[float, float, float]:
    """(xc, yc, h) on screen at time t."""
    z, cx, cy = 1.0, 0.5, 0.5
    zoom = None
    for v in sorted((v for v in visuals if v.type in ZOOMS), key=lambda v: v.t):
        if v.active(t):
            zoom = v                                   # the latest one to start wins
    if zoom:
        q = _smooth(min(max((t - zoom.t) / zoom.d, 0.0), 1.0))
        p = 1.0 if zoom.type == "punch" else 1 - q if zoom.type == "pull" else q
        z, cx, cy = 1 + (zoom.zoom - 1) * p, 0.5 + (zoom.cx - 0.5) * p, 0.5 + (zoom.cy - 0.5) * p
    a = dx = dy = 0.0
    for v in visuals:
        if v.type == "shake" and v.active(t):
            e = SHAKE_AMP.get(v.strength, SHAKE_AMP[2]) * (1 - (t - v.t) / v.d)
            a += e
            dx += e * _wave(t - v.t, WAVES_X)
            dy += e * _wave(t - v.t, WAVES_Y)
    h = 0.5 / (z * (1 + MARGIN * a))
    lo, hi = h + a, 1 - h - a
    if lo > hi:
        cx = cy = 0.5
    else:
        cx, cy = min(max(cx, lo), hi), min(max(cy, lo), hi)
    return cx + dx, cy + dy, h


def flashes(t: float, visuals: list[Visual]) -> list[tuple[tuple[int, int, int], float]]:
    """(colour, opacity) of each flash showing at time t."""
    out = []
    for v in visuals:
        if v.type in ("flash", "fadeout") and v.active(t):
            rgb, peak = FLASH.get(v.color, FLASH["white"])
            q = (t - v.t) / v.d
            out.append((rgb, peak * (q if v.type == "fadeout" else 1 - q)))
    return out


# ── transitions: how one cut goes into the next segment ─────────────────────
# A transition is an effect {"type": "transition", "t": the incoming segment's start (clip
# time), "style", "d"}. The renderer encodes every segment on its own, so a transition is
# an "out" half at the end of the segment before and an "in" half at the start of the next,
# both made of the pieces above. editor.js (transVis) builds the same list for its preview.
TRANSITIONS = {            # style: (default length, what it is)
    "flash": (0.3, "a white flash into the next shot (a reveal, the cold open into the story)"),
    "fade": (0.5, "a dip through black (time passes, \"later…\", the ending)"),
    "zoom": (0.35, "zoom hard into the cut and out of the next shot (into the punchline)"),
    "whip": (0.3, "a fast blurred pan (a jump in time or topic)"),
}
TRANSITION_D = (0.15, 1.2)


def transitions(segments: list[list[float]], effects: list[dict], ends: dict[int, float] | None = None) -> list[tuple[int, Visual]]:
    """(segment index, Visual in clip time) for every transition on a real cut (not a
    split that plays on). ends: each segment's true end, when it isn't segments[i][1]."""
    out: list[tuple[int, Visual]] = []
    marks = [f for f in effects if f.get("type") == "transition" and f.get("style") in TRANSITIONS]
    for i in range(1, len(segments)):
        s, prev = segments[i][0], segments[i - 1]
        e = (ends or {}).get(i - 1, prev[1])
        if abs(s - e) < 0.002:
            continue
        tr = next((f for f in marks if abs(f["t"] - s) < 0.05), None)
        if tr is None:
            continue
        d = min(max(float(tr.get("d") or TRANSITIONS[tr["style"]][0]), TRANSITION_D[0]), TRANSITION_D[1])
        h = min(d / 2, (e - prev[0]) / 3, (segments[i][1] - s) / 3)
        st = tr["style"]
        if st == "flash":
            out.append((i, Visual("flash", s, max(h * 2, 0.1), color="white")))
        elif st == "fade":
            out += [(i - 1, Visual("fadeout", e - h, h, color="black")), (i, Visual("flash", s, h, color="black"))]
        elif st == "zoom":
            out += [(i - 1, Visual("push", e - h, h, zoom=1.8)), (i, Visual("pull", s, h, zoom=1.8))]
        elif st == "whip":
            out += [(i - 1, Visual("push", e - h, h, zoom=1.35, cx=1.0)), (i - 1, Visual("blur", e - h, h)),
                    (i, Visual("pull", s, h, zoom=1.35, cx=0.0)), (i, Visual("blur", s, h))]
    return out


# ── ffmpeg ───────────────────────────────────────────────────────────────────

def _n(v: float) -> str:
    return f"({v:.6g})"


def view_exprs(visuals: list[Visual], tv: str) -> tuple[str, str, str]:
    """camera() as ffmpeg expressions (xc, yc, h) of the time expression tv."""
    T = tv
    Z, CX, CY = "1", "0.5", "0.5"
    for v in sorted((v for v in visuals if v.type in ZOOMS), key=lambda v: v.t):
        gate = f"gte({T},{_n(v.t)})*lt({T},{_n(v.t + v.d)})"
        if v.type == "punch":
            p = "1"
        else:
            q = f"clip(({T}-{_n(v.t)})/{_n(v.d)},0,1)"
            p = f"({q}*{q}*(3-2*{q}))"
            if v.type == "pull":
                p = f"(1-{p})"
        Z = f"if({gate},(1+{_n(v.zoom - 1)}*{p}),{Z})"
        CX = f"if({gate},(0.5+{_n(v.cx - 0.5)}*{p}),{CX})"
        CY = f"if({gate},(0.5+{_n(v.cy - 0.5)}*{p}),{CY})"
    A, DX, DY = ["0"], ["0"], ["0"]
    for v in visuals:
        if v.type != "shake":
            continue
        tau = f"({T}-{_n(v.t)})"
        e = (f"({_n(SHAKE_AMP.get(v.strength, SHAKE_AMP[2]))}*(1-{tau}/{_n(v.d)})"
             f"*gte({T},{_n(v.t)})*lt({T},{_n(v.t + v.d)}))")
        A.append(e)
        for out, waves in ((DX, WAVES_X), (DY, WAVES_Y)):
            out.append(e + "*(" + "+".join(f"{_n(w)}*sin({_n(2 * math.pi * hz)}*{tau}+{_n(ph)})"
                                           for w, hz, ph in waves) + ")")
    a = "(" + "+".join(A) + ")"
    h = f"(0.5/(({Z})*(1+{_n(MARGIN)}*{a})))"
    lo, hi = f"({h}+{a})", f"(1-{h}-{a})"
    xc = f"(if(gt({lo},{hi}),0.5,clip({CX},{lo},{hi}))+(" + "+".join(DX) + "))"
    yc = f"(if(gt({lo},{hi}),0.5,clip({CY},{lo},{hi}))+(" + "+".join(DY) + "))"
    return xc, yc, h


def local(visuals: list[Visual], start: float, length: float) -> list[Visual]:
    """The effects that touch [start, start + length), moved to that span's own time."""
    return [replace(v, t=v.t - start) for v in visuals if v.t < start + length and v.t + v.d > start]


def _warp(visuals: list[Visual], fps: float) -> str:
    """The view of these zooms and shakes as a perspective warp (smooth, sub-pixel)."""
    xc, yc, h = view_exprs(visuals, f"(in/{fps:.6g})")
    corners = {"x0": f"W*({xc}-{h})", "y0": f"H*({yc}-{h})", "x1": f"W*({xc}+{h})", "y1": f"H*({yc}-{h})",
               "x2": f"W*({xc}-{h})", "y2": f"H*({yc}+{h})", "x3": f"W*({xc}+{h})", "y3": f"H*({yc}+{h})"}
    return "perspective=interpolation=linear:eval=frame:" + ":".join(f"{k}='{v}'" for k, v in corners.items())


def _even(v: float) -> int:
    return int(round(v / 2)) * 2


def panel_boxes(visuals: list[Visual]) -> list[tuple[float, float, float, float]]:
    """The panels that zoom on their own, in a fixed order."""
    return sorted({v.box for v in visuals if v.box is not None and v.type in ZOOMS})


def video_filter(visuals: list[Visual], fps: float, length: float,
                 size: tuple[int, int] = (1080, 1920)) -> str | None:
    """The -vf chain for one segment, effects in its own time (0 = its first frame), or
    None when nothing shows in it. Each flash is one filled box per frame it shows on.
    Zooms with a box (a stack's panel) zoom just that part of the picture: it's cropped
    off a copy, warped and laid back where it was (size: the picture's width and height).
    The result can hold labelled branches, so it goes after an input label in a graph
    (or as a -vf script), never inside a longer chain."""
    fps = fps or 30.0
    parts = []
    W, H = size
    for k, box in enumerate(panel_boxes(visuals)):
        x0, y0 = min(max(_even(box[0] * W), 0), W - 2), min(max(_even(box[1] * H), 0), H - 2)
        w = max(2, min(_even((box[0] + box[2]) * W), W) - x0)
        h = max(2, min(_even((box[1] + box[3]) * H), H) - y0)
        warp = _warp([v for v in visuals if v.box == box and v.type in ZOOMS], fps)
        parts.append(f"split[pz{k}m][pz{k}s];[pz{k}s]crop={w}:{h}:{x0}:{y0},{warp}[pz{k}z];"
                     f"[pz{k}m][pz{k}z]overlay={x0}:{y0}")
    whole = [v for v in visuals if v.box is None or v.type not in ZOOMS]
    if any(v.type in ZOOMS or v.type == "shake" for v in whole):
        parts.append(_warp(whole, fps))
    frames = max(1, round(length * fps))
    for v in visuals:                          # a whip's motion blur
        if v.type == "blur":
            parts.append(f"boxblur=luma_radius=24:luma_power=1:chroma_radius=12:chroma_power=1"
                         f":enable='between(t,{v.t:.4f},{v.t + v.d:.4f})'")
    for v in visuals:
        if v.type not in ("flash", "fadeout"):
            continue
        rgb, _peak = FLASH.get(v.color, FLASH["white"])
        first, last = max(0, math.ceil(v.t * fps - 1e-6)), min(frames - 1, math.ceil((v.t + v.d) * fps - 1e-6) - 1)
        for n in range(first, last + 1):
            alpha = next((a for c, a in flashes(n / fps, [v])), 0.0)
            if alpha > 0.004:
                parts.append(f"drawbox=x=0:y=0:w=iw:h=ih:t=fill:color=0x{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"
                             f"@{alpha:.3f}:enable='eq(n,{n})'")
    return ",".join(parts) or None
