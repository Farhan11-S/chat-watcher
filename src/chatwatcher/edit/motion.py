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
ZOOMS = ("punch", "push")
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
        p = 1.0 if zoom.type == "punch" else _smooth(min(max((t - zoom.t) / zoom.d, 0.0), 1.0))
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
        if v.type == "flash" and v.active(t):
            rgb, peak = FLASH.get(v.color, FLASH["white"])
            out.append((rgb, peak * (1 - (t - v.t) / v.d)))
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


def video_filter(visuals: list[Visual], fps: float, length: float) -> str | None:
    """The -vf chain for one segment, effects in its own time (0 = its first frame), or
    None when nothing shows in it. The view is a perspective warp (smooth, sub-pixel);
    each flash is one filled box per frame it shows on."""
    fps = fps or 30.0
    parts = []
    if any(v.type in ZOOMS or v.type == "shake" for v in visuals):
        xc, yc, h = view_exprs(visuals, f"(in/{fps:.6g})")
        corners = {"x0": f"W*({xc}-{h})", "y0": f"H*({yc}-{h})", "x1": f"W*({xc}+{h})", "y1": f"H*({yc}-{h})",
                   "x2": f"W*({xc}-{h})", "y2": f"H*({yc}+{h})", "x3": f"W*({xc}+{h})", "y3": f"H*({yc}+{h})"}
        parts.append("perspective=interpolation=linear:eval=frame:"
                     + ":".join(f"{k}='{v}'" for k, v in corners.items()))
    frames = max(1, round(length * fps))
    for v in visuals:
        if v.type != "flash":
            continue
        rgb, _peak = FLASH.get(v.color, FLASH["white"])
        first, last = max(0, math.ceil(v.t * fps - 1e-6)), min(frames - 1, math.ceil((v.t + v.d) * fps - 1e-6) - 1)
        for n in range(first, last + 1):
            alpha = next((a for c, a in flashes(n / fps, [v])), 0.0)
            if alpha > 0.004:
                parts.append(f"drawbox=x=0:y=0:w=iw:h=ih:t=fill:color=0x{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"
                             f"@{alpha:.3f}:enable='eq(n,{n})'")
    return ",".join(parts) or None
