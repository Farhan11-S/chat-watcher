"""Spotlights: point the viewer at the thing that matters (the "Yes" she clicked, the date
that changed, the 1 HP) by dimming everything else round an ellipse, with a ring round it,
or just the ring (the red circle), or just the dimming.

A spotlight on an edit is an effect {"type": "spot", "t", "d", "x", "y", "w", "h", "style",
"color"}: x, y, w, h are the ellipse's bounds as shares of the CLIP's frame (like regions),
so on a Short it stays on the thing whatever the layout or a frame shot does with the
picture (map_box); style is one of STYLES, color the ring's (COLORS).

The render lays a picture of it (mask(): black, see-through inside the ellipse, the ring
drawn on) over the Short's or the clip's picture BEFORE the zooms, so a punch-in zooms
into it too, and fades it in and out. editor.js draws the same in its preview (drawSpots):
change both together."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from chatwatcher.edit import layout as lay

SPOT = "spot"
STYLES = ("spotlight", "circle", "dim")      # dim + ring, the ring only, dim only
COLORS = {"red": (255, 59, 48), "yellow": (255, 214, 0), "white": (255, 255, 255)}
DEFAULT_COLOR = {"spotlight": "white", "circle": "red", "dim": "white"}
D_LIMITS = (1.5, 0.3, 8.0)       # default, shortest and longest, seconds
DIM = 0.62                       # how dark outside the ellipse (0-1)
FEATHER = 0.07                   # the soft edge of the dimming, as a share of the picture's shorter side
RING = 0.011                     # the ring's thickness, same
FADE_IN, FADE_OUT = 0.2, 0.18
MIN_SIZE = 0.02


def map_box(box: tuple[float, float, float, float], panels: list[lay.Panel] | None
            ) -> tuple[float, float, float, float] | None:
    """The ellipse's bounds on the output picture: as they are on a 16:9 picture (panels
    None), or through the Short's sharp panel that shows its centre (None: not on screen)."""
    x, y, w, h = box
    if panels is None:
        return box
    cx, cy = x + w / 2, y + h / 2
    for p in reversed(panels):
        if p.blur:
            continue
        sx, sy, sw, sh = p.src
        if sx <= cx <= sx + sw and sy <= cy <= sy + sh:
            dx, dy, dw, dh = p.dst
            kx, ky = dw / sw, dh / sh
            ox, oy = dx + (cx - sx) * kx, dy + (cy - sy) * ky
            return ox - w * kx / 2, oy - h * ky / 2, w * kx, h * ky
    return None


@dataclass
class Placed:
    t: float                     # clip time
    d: float
    box: tuple[float, float, float, float]       # on the output picture, shares
    style: str
    color: tuple[int, int, int]


def placed(effects: list[dict[str, Any]], panels: list[lay.Panel] | None) -> list[Placed]:
    out = []
    for fx in effects:
        if fx.get("type") != SPOT:
            continue
        b = map_box((fx["x"], fx["y"], fx["w"], fx["h"]), panels)
        if b is None:
            continue
        style = fx.get("style") if fx.get("style") in STYLES else "spotlight"
        out.append(Placed(fx["t"], fx["d"], b, style,
                          COLORS.get(fx.get("color") or "", COLORS[DEFAULT_COLOR[style]])))
    return out


def local(items: list[Placed], start: float, length: float) -> list[tuple[Placed, float, float, bool, bool]]:
    """(spotlight, when it starts in this part, how long it shows, begins here, ends here)."""
    out = []
    for p in items:
        a, b = max(p.t, start), min(p.t + p.d, start + length)
        if b - a > 0.04:
            out.append((p, a - start, b - a, a - p.t < 0.02, p.t + p.d - b < 0.02))
    return out


def mask(p: Placed, w: int, h: int) -> np.ndarray:
    """The picture laid over the frame (RGBA, h × w): black at DIM outside the ellipse with a
    soft edge, see-through inside, the ring on its edge."""
    bx, by, bw, bh = p.box
    cx, cy = (bx + bw / 2) * w, (by + bh / 2) * h
    rx, ry = max(bw * w / 2, 2.0), max(bh * h / 2, 2.0)
    short = min(w, h)
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    nx, ny = (xs + 0.5 - cx) / rx, (ys + 0.5 - cy) / ry
    r = np.sqrt(nx * nx + ny * ny)
    alpha = np.zeros((h, w), np.float32)
    if p.style in ("spotlight", "dim"):           # the dimming: like the preview's radial gradient
        alpha = DIM * np.clip((r - 1) / (FEATHER * short / min(rx, ry)), 0, 1)
    rgb = np.zeros((h, w, 3), np.float32)
    if p.style in ("spotlight", "circle"):        # the ring: distance to the ellipse, in pixels
        f = nx * nx + ny * ny - 1
        grad = 2 * np.sqrt((nx / rx) ** 2 + (ny / ry) ** 2) + 1e-6
        dist = np.abs(f / grad)
        half = max(1.5, RING * short / 2)
        ring = np.clip(half + 0.75 - dist, 0, 1)
        alpha = ring + alpha * (1 - ring)
        rgb = np.asarray(p.color, np.float32) * (ring / np.maximum(alpha, 1e-6))[..., None]
    out = np.dstack([rgb, alpha[..., None] * 255])
    return np.clip(out, 0, 255).astype(np.uint8)


def write_mask(p: Placed, w: int, h: int, dest: Path) -> Path:
    from PIL import Image
    Image.fromarray(mask(p, w, h), "RGBA").save(dest)
    return dest


def filter_graph(base: str, items: list[tuple[Placed, float, float, bool, bool]], first_input: int) -> tuple[str, str]:
    """ffmpeg filters laying the spotlights (inputs first_input, …: their masks, looped) over
    the stream labelled base, fading in and out. Returns (filters, the label of the result)."""
    chains, cur = [], base
    for k, (_p, a, d, begins, ends) in enumerate(items):
        fades = []
        if begins:
            fades.append(f"fade=t=in:st={a:.3f}:d={min(FADE_IN, d / 2):.3f}:alpha=1")
        if ends:
            fo = min(FADE_OUT, d / 2)
            fades.append(f"fade=t=out:st={a + d - fo:.3f}:d={fo:.3f}:alpha=1")
        chains.append(f"[{first_input + k}:v]format=rgba{''.join(',' + x for x in fades)}[sp{k}]")
        chains.append(f"{cur}[sp{k}]overlay=0:0:shortest=1:format=auto:"
                      f"enable='between(t,{a:.3f},{a + d:.3f})'[spo{k}]")
        cur = f"[spo{k}]"
    return ";".join(chains), cur
