"""Layers: a region of the clip's own picture laid over the picture, see-through, for a
while. The stream's chat over the middle of her face at 60 %, the minimap in a corner,
her facecam over the game when the layout doesn't show it.

A layer on an edit is an effect {"type": "layer", "t", "d", "region", "at", "dx", "dy",
"size", "opacity"}: region is the part of the clip it shows (a drawn region); at, dx, dy
and size place it like an overlay (edit.overlays.place: at is "middle", "full" or a region,
centred where that region is on screen, moved by dx, dy; size is its width as a share of
the picture's width); opacity 1 is solid. It fades in and out over FADE seconds.

In the render it's cropped off a copy of the clip and laid over the picture after the zooms
(so it holds still) and under overlays and subtitles (edit.render). The editor draws the
same in its preview (static/editor/overlays.js): change both together.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from chatwatcher.edit import layout as lay
from chatwatcher.edit import overlays as ovl

LAYER = "layer"
D_LIMITS = (3.0, 0.3, 60.0)           # default, shortest and longest, seconds
SIZE_RANGE = (0.1, 1.5)
OPACITY_RANGE = (0.1, 1.0)
DEFAULT_SIZE, DEFAULT_OPACITY = 0.5, 0.7
FADE = 0.15


@dataclass
class Placed:
    src: tuple[float, float, float, float]    # the region, fractions of the clip's frame
    t: float                                   # clip time
    d: float
    x: float                                   # its centre, shares of the output picture
    y: float
    w: float                                   # its width, a share of the output picture's width
    aspect: float                              # width / height on screen
    opacity: float


def placed(effects: list[dict[str, Any]], boxes: dict[str, dict[str, float]], panels: list[lay.Panel] | None,
           src_size: tuple[int, int]) -> list[Placed]:
    """The layers of these effects (clip time) whose region is drawn, ready to render.
    panels: a Short's picture (None for 16:9). src_size: the clip's width and height."""
    sw, sh = src_size[0] or 1920, src_size[1] or 1080
    out = []
    for fx in effects:
        b = boxes.get(fx.get("region") or "") if fx.get("type") == LAYER else None
        if not b:
            continue
        x, y = ovl.place(fx.get("at") or "middle", boxes, panels, float(fx.get("dx") or 0), float(fx.get("dy") or 0))
        out.append(Placed((b["x"], b["y"], b["w"], b["h"]), fx["t"], fx["d"], x, y,
                          float(fx.get("size") or DEFAULT_SIZE), (b["w"] * sw) / max(b["h"] * sh, 1e-6),
                          float(fx.get("opacity") or DEFAULT_OPACITY)))
    return out


def local(items: list[Placed], start: float, length: float) -> list[tuple[Placed, float, float]]:
    """(layer, when it starts in this part, how long it shows) for each layer in [start, start + length)."""
    out = []
    for p in items:
        a, b = max(p.t, start), min(p.t + p.d, start + length)
        if b - a > 0.01:
            out.append((p, a - start, b - a))
    return out


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def filter_graph(base: str, src: str, items: list[tuple[Placed, float, float]], out_w: int, out_h: int,
                 start: float = 0.0) -> tuple[str, str]:
    """ffmpeg filters laying the layers, cut from the stream labelled src (a copy of the
    clip), over the stream labelled base. start: where this part starts in clip time (for
    the fades, which belong to the whole layer). Returns (filters, the label of the result)."""
    chains, cur = [], base
    n = len(items)
    ins = [src] if n == 1 else [f"[ly{k}s]" for k in range(n)]
    if n > 1:
        chains.append(f"{src}split={n}" + "".join(ins))
    for k, (p, a, d) in enumerate(items):
        x, y, w, h = p.src
        ow = _even(p.w * out_w)
        oh = _even(ow / p.aspect)
        fade_in, fade_out = p.t - start, p.t + p.d - start - FADE     # this part's own time
        crop = (f"crop=w=trunc(iw*{w:.5f}/2)*2:h=trunc(ih*{h:.5f}/2)*2"
                f":x=trunc(iw*{x:.5f}/2)*2:y=trunc(ih*{y:.5f}/2)*2")
        fades = (f",fade=t=in:st={fade_in:.3f}:d={FADE}:alpha=1" if fade_in >= 0 else "") + \
                (f",fade=t=out:st={fade_out:.3f}:d={FADE}:alpha=1" if fade_out < a + d else "")
        chains.append(f"{ins[k]}{crop},scale={ow}:{oh}:flags=lanczos,setsar=1,format=yuva420p,"
                      f"colorchannelmixer=aa={p.opacity:.3f}{fades}[ly{k}]")
        ox, oy = round(p.x * out_w - ow / 2), round(p.y * out_h - oh / 2)
        chains.append(f"{cur}[ly{k}]overlay=x={ox}:y={oy}:format=auto:"
                      f"enable='between(t,{a:.3f},{a + d:.3f})'[lyo{k}]")
        cur = f"[lyo{k}]"
    return ";".join(chains), cur
