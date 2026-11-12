"""How a Short (9:16) is built from the 16:9 clip.

Three layouts:
    crop   a tall window cut from the clip. x, y: its centre (fractions of the frame);
           scale 1 = the full height, more = tighter
    stack  two boxes, one above the other: top (e.g. the game) and bottom (her face), each
           a region name or "" for the whole frame. split = the top's share of the height
    fit    the whole frame across the middle, over a blurred, darker copy that fills the screen
Any layout can have a title in the top band (burned in with the subtitles).

A "frame" effect (FRAME) overrides the layout for a while: the Short then shows one region
full-screen (frame_panels): a narrow one (her face, a plugin window) as a tight crop, a wide
one (the game) across the middle over a blurred copy. That's how the camera moves to
something the layout doesn't show. Its region can be FULL, the whole picture: the fit + blur
view for a while (everyone's facecams and the game at once, a desktop).

panels() turns a layout into rectangles of the clip and where each goes on the Short's
1080×1920 frame. The renderer makes an ffmpeg graph from them (graph()), and editor.js
draws the same panels for its preview: change both together.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

OUT_W, OUT_H = 1080, 1920
TYPES = ("crop", "stack", "fit")
SCALE_RANGE = (1.0, 2.5)
SPLIT_RANGE = (0.3, 0.7)
MAX_TITLE = 100
BLUR_DOWN = 8                  # the fit background is blurred at 1/8 size (cheap, and softer)
BG_DARK = 0.7                  # …and darkened to 70 %

Box = tuple[float, float, float, float]       # x, y, w, h as fractions


@dataclass
class Panel:
    src: Box                   # part of the clip's frame
    dst: Box                   # where it goes on the Short's frame
    blur: bool = False         # the fit layout's background


def default(boxes: dict[str, dict[str, float]], stream_type: str | None = None) -> dict[str, Any]:
    """A first layout: gaming with a face box → game over face; otherwise a crop on her face
    (or the centre)."""
    face = boxes.get("face")
    if stream_type == "gaming" and face:
        return {"type": "stack", "top": "game" if "game" in boxes else "", "bottom": "face", "split": 0.6,
                "x": 0.5, "y": 0.5, "scale": 1.0, "title": ""}
    x = round(face["x"] + face["w"] / 2, 4) if face else 0.5
    return {"type": "crop", "x": x, "y": 0.5, "scale": 1.0, "top": "game" if "game" in boxes else "",
            "bottom": "face" if face else "", "split": 0.6, "title": ""}


def _num(v: Any, default: float, lo: float, hi: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return round(min(max(f, lo), hi), 4) if math.isfinite(f) else default


def _region(v: Any) -> str:
    n = re.sub(r"\s+", " ", str(v or "")).strip().lower()
    return n if re.match(r"^[a-z0-9][a-z0-9 _-]{0,19}$", n) else ""


def clean(raw: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    """A layout from the browser, every field checked; anything missing or bad comes from fallback."""
    raw = raw if isinstance(raw, dict) else {}
    f = fallback
    title = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(raw.get("title", f.get("title", "")) or "")).split())
    return {"type": raw.get("type") if raw.get("type") in TYPES else f.get("type", "crop"),
            "x": _num(raw.get("x", f.get("x")), 0.5, 0.0, 1.0),
            "y": _num(raw.get("y", f.get("y")), 0.5, 0.0, 1.0),
            "scale": _num(raw.get("scale", f.get("scale")), 1.0, *SCALE_RANGE),
            "top": _region(raw.get("top", f.get("top"))),
            "bottom": _region(raw.get("bottom", f.get("bottom"))),
            "split": _num(raw.get("split", f.get("split")), 0.6, *SPLIT_RANGE),
            "title": title[:MAX_TITLE]}


def used_regions(layout: dict[str, Any]) -> set[str]:
    return {n for n in (layout.get("top"), layout.get("bottom")) if n} if layout.get("type") == "stack" else set()


def _fit(box: Box, aspect: float, sw: int, sh: int) -> Box:
    """The box grown around its centre to this shape (width / height in pixels), so all of it
    shows; shrunk only if it would leave the frame."""
    bx, by, bw, bh = box
    pw, ph = bw * sw, bh * sh
    if pw / ph < aspect:
        pw = ph * aspect
    else:
        ph = pw / aspect
    if pw > sw:
        pw, ph = sw, sw / aspect
    if ph > sh:
        pw, ph = sh * aspect, sh
    cx, cy = (bx + bw / 2) * sw, (by + bh / 2) * sh
    x0 = min(max(cx - pw / 2, 0.0), sw - pw)
    y0 = min(max(cy - ph / 2, 0.0), sh - ph)
    return x0 / sw, y0 / sh, pw / sw, ph / sh


def _box(name: str, boxes: dict[str, dict[str, float]]) -> Box:
    b = boxes.get(name) if name else None
    return (b["x"], b["y"], b["w"], b["h"]) if b else (0.0, 0.0, 1.0, 1.0)


def panels(layout: dict[str, Any], boxes: dict[str, dict[str, float]], sw: int, sh: int) -> list[Panel]:
    """The layout as panels, drawn in order (later ones on top)."""
    sw, sh = sw or 1920, sh or 1080
    kind = layout.get("type", "crop")
    if kind == "stack":
        split = layout.get("split", 0.6)
        top_aspect, bottom_aspect = OUT_W / (OUT_H * split), OUT_W / (OUT_H * (1 - split))
        return [Panel(_fit(_box(layout.get("top", ""), boxes), top_aspect, sw, sh), (0.0, 0.0, 1.0, split)),
                Panel(_fit(_box(layout.get("bottom", ""), boxes), bottom_aspect, sw, sh), (0.0, split, 1.0, 1 - split))]
    if kind == "fit":
        h = OUT_W * sh / sw / OUT_H
        return [Panel(_fit((0.0, 0.0, 1.0, 1.0), OUT_W / OUT_H, sw, sh), (0.0, 0.0, 1.0, 1.0), blur=True),
                Panel((0.0, 0.0, 1.0, 1.0), (0.0, (1 - h) / 2, 1.0, h))]
    # crop: a window of the Short's shape, scale × tighter than the full height
    ph = sh / layout.get("scale", 1.0)
    pw = ph * OUT_W / OUT_H
    if pw > sw:
        pw, ph = sw, sw * OUT_H / OUT_W
    cx, cy = layout.get("x", 0.5) * sw, layout.get("y", 0.5) * sh
    x0, y0 = min(max(cx - pw / 2, 0.0), sw - pw), min(max(cy - ph / 2, 0.0), sh - ph)
    return [Panel((x0 / sw, y0 / sh, pw / sw, ph / sh), (0.0, 0.0, 1.0, 1.0))]


FRAME = "frame"
FULL = "full"                       # a frame shot on the whole picture: fit + blur for a while
FULL_BOX = {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}
FRAME_LIMITS = (2.0, 0.3, 15.0)     # default, shortest and longest frame shot, seconds
FRAME_MARGIN = 1.25                 # room around the region
FRAME_WIDE = 1.6                    # wider than this × the Short's shape: across the middle instead


def frame_panels(box: dict[str, float], sw: int, sh: int) -> list[Panel]:
    """The Short showing just this region (fractions of the clip's frame)."""
    sw, sh = sw or 1920, sh or 1080
    w, h = min(box["w"] * FRAME_MARGIN, 1.0), min(box["h"] * FRAME_MARGIN, 1.0)
    cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
    grown = (min(max(cx - w / 2, 0.0), 1 - w), min(max(cy - h / 2, 0.0), 1 - h), w, h)
    aspect = OUT_W / OUT_H
    if (w * sw) / (h * sh) <= aspect * FRAME_WIDE:
        return [Panel(_fit(grown, aspect, sw, sh), (0.0, 0.0, 1.0, 1.0))]
    src = _fit(grown, OUT_W / (OUT_H * 0.6), sw, sh)           # wide: as big as fits across
    dh = min(0.6, OUT_W / (src[2] * sw) * (src[3] * sh) / OUT_H)
    return [Panel(_fit(grown, aspect, sw, sh), (0.0, 0.0, 1.0, 1.0), blur=True),
            Panel(src, (0.0, (1 - dh) / 2, 1.0, dh))]


def region_box(name: str, boxes: dict[str, dict[str, float]]) -> dict[str, float] | None:
    """A frame shot's region: a drawn box, or FULL (the whole picture), or None."""
    return FULL_BOX if name == FULL else boxes.get(name)


def frame_at(t: float, frames: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The frame shot showing at t (the latest one to start wins)."""
    on = [f for f in frames if f["t"] <= t < f["t"] + f["d"]]
    return max(on, key=lambda f: f["t"]) if on else None


def map_point(x: float, y: float, ps: list[Panel]) -> tuple[float, float] | None:
    """Where a point of the clip's frame lands on the Short (the top-most sharp panel that
    shows it), or None when it isn't on screen."""
    for p in reversed(ps):
        if p.blur:
            continue
        sx, sy, sw, sh = p.src
        if sx <= x <= sx + sw and sy <= y <= sy + sh:
            dx, dy, dw, dh = p.dst
            return dx + (x - sx) / sw * dw, dy + (y - sy) / sh * dh
    return None


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def graph(ps: list[Panel], sw: int, sh: int) -> str:
    """An ffmpeg filter graph from [0:v] to [base], the 1080×1920 picture."""
    chains, labels = [], []
    n = len(ps)
    head = f"[0:v]split={n}" + "".join(f"[i{k}]" for k in range(n)) + ";" if n > 1 else ""
    for k, p in enumerate(ps):
        src = "[0:v]" if n == 1 else f"[i{k}]"
        x, y, w, h = p.src
        cw, ch = min(_even(w * sw), sw - sw % 2), min(_even(h * sh), sh - sh % 2)
        cx, cy = min(max(0, round(x * sw)), sw - cw), min(max(0, round(y * sh)), sh - ch)
        y0, y1 = _even(p.dst[1] * OUT_H) if p.dst[1] > 0 else 0, _even((p.dst[1] + p.dst[3]) * OUT_H)
        x0, x1 = _even(p.dst[0] * OUT_W) if p.dst[0] > 0 else 0, _even((p.dst[0] + p.dst[2]) * OUT_W)
        dw, dh = min(x1, OUT_W) - x0, min(y1, OUT_H) - y0
        chain = f"{src}crop={cw}:{ch}:{cx}:{cy},"
        if p.blur:
            chain += (f"scale={_even(dw / BLUR_DOWN)}:{_even(dh / BLUR_DOWN)},boxblur=4:2,"
                      f"scale={dw}:{dh}:flags=bicubic,lutyuv=y=val*{BG_DARK}")
        else:
            chain += f"scale={dw}:{dh}:flags=lanczos"
        chain += f",setsar=1[p{k}]"
        chains.append(chain)
        labels.append((f"[p{k}]", x0, y0, dw, dh))
    out = []
    first, x0, y0, dw, dh = labels[0]
    if (dw, dh) == (OUT_W, OUT_H):
        cur = first
    else:
        out.append(f"{first}pad={OUT_W}:{OUT_H}:{x0}:{y0}:black[c0]")
        cur = "[c0]"
    for k, (lab, x0, y0, _dw, _dh) in enumerate(labels[1:], 1):
        out.append(f"{cur}{lab}overlay={x0}:{y0}:eof_action=pass[c{k}]")
        cur = f"[c{k}]"
    out.append(f"{cur}format=yuv420p[base]")
    return head + ";".join(chains + out)
