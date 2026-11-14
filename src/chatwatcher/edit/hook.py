"""The hook: a Short's opening text ("1 HP. One hit left."), the layout's title, shown over
the picture for the first few seconds of the Short and then gone, so the picture is
clear for the rest of it.

How it looks is the layout's "hook" (clean() checks it; the AI edit picks it):
    s       seconds it shows from the Short's start (HOOK_S)
    font    "" (the subtitles' font) or a caption font: round, angry (dripping paint),
            scary (horror), loud (comic capitals)
    fx      how it comes in and moves: pop, slam (drops from big, like a stamp), bounce,
            shake, wave, wobble, grow, none (the caption effects, edit.captions, plus slam)
    look    outline (black outline and shadow) · bubble (thick white outline, dark ring,
            like the bubble captions) · box (on a solid box, in `box` colour)
    color   the text's colour; accent: *starred* words' colour; box: the box's colour
    at      top · upper · middle · lower: how far down the picture it sits
    size    small · normal · big · huge
    tilt    degrees, anticlockwise (- leans the other way)

Drawn by libass with the captions (edit.render puts events() in the part's .ass file).
The editor draws the same for its preview (preview.js drawHook): change both together."""

from __future__ import annotations

import math
import re
from typing import Any

from chatwatcher.edit import captions as cap
from chatwatcher.subs import lines

HOOK_S = (3.0, 1.0, 10.0)            # default, shortest, longest
FONTS = ("", *cap.FONTS)
FX = ("pop", "slam", "bounce", "shake", "wave", "wobble", "grow", "none")
LOOKS = ("outline", "bubble", "box")
PLACES = {"top": 0.06, "upper": 0.2, "middle": 0.42, "lower": 0.62}   # the text's top, as a share of the height
SIZES = {"small": 0.8, "normal": 1.0, "big": 1.3, "huge": 1.65}       # times lines.TITLE_SIZE
COLORS = {"white": "FFFFFF", "yellow": "FFD600", "red": "FF3B30", "green": "3DDC84", "blue": "4DA3FF",
          "pink": "FF5FA2", "purple": "8B5CF6", "orange": "FF8A00", "black": "111111"}
TILT = 12.0
SLAM_MS = 160
FADE_MS = 200
DEFAULT = {"s": HOOK_S[0], "font": "", "fx": "pop", "look": "outline", "color": "white", "accent": "yellow",
           "box": "red", "at": "top", "size": "normal", "tilt": 0.0}


def clean(raw: Any) -> dict[str, Any]:
    """A hook style, every field checked; anything missing or bad is the default."""
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULT)
    try:
        s = float(raw.get("s", DEFAULT["s"]))
        out["s"] = round(min(max(s, HOOK_S[1]), HOOK_S[2]), 2) if math.isfinite(s) else DEFAULT["s"]
    except (TypeError, ValueError):
        pass
    try:
        tilt = float(raw.get("tilt", 0) or 0)
        out["tilt"] = round(min(max(tilt, -TILT), TILT), 1) if math.isfinite(tilt) else 0.0
    except (TypeError, ValueError):
        pass
    for key, allowed in (("font", FONTS), ("fx", FX), ("look", LOOKS), ("color", COLORS), ("accent", COLORS),
                         ("box", COLORS), ("at", PLACES), ("size", SIZES)):
        v = str(raw.get(key, out[key]) or "").strip().lower()
        if v in allowed:
            out[key] = v
    return out


def _hex(name: str) -> str:
    return COLORS.get(name, "FFFFFF")


def _dark(rgb: str) -> str:
    """The ring round a bubble hook's white outline: its colour, much darker (black for white)."""
    h, l, s = cap._hls(rgb)
    return "1A1A1A" if s < 0.12 else cap._rgb(h, min(l, 0.5) * 0.38, s)


def layout(text: str, style: dict[str, Any], w: int, h: int) -> dict[str, Any]:
    """Where the hook goes and how big: rows, font size, centre (px). preview.js does the same."""
    k = SIZES[style["size"]]
    size = lines.TITLE_SIZE * w * k
    rows = lines.wrap(text, max(6, int(lines.TITLE_LINE / max(k, 1.0))), 3)
    height = len(rows) * size * cap.LINE_H
    return {"rows": rows, "size": size, "x": w / 2, "y": PLACES[style["at"]] * h + height / 2}


def events(text: str, style: dict[str, Any], w: int, h: int, into: float, length: float) -> list[str]:
    """ASS Dialogue lines for the hook in one part of the render: into is how far into the
    Short the part starts, length how long the part is. Nothing once the hook is over."""
    style = clean(style)
    left = style["s"] - into
    if not text or left <= 0.02 or length <= 0:
        return []
    d = min(left, length)
    begins, ends = into < 0.02, left <= length + 0.02
    lay_ = layout(text, style, w, h)
    rows, size, x, y = lay_["rows"], lay_["size"], lay_["x"], lay_["y"]
    dur_ms, into_ms = max(1, round(d * 1000)), round(max(into, 0) * 1000)
    fx = style["fx"]
    moved = "none" if fx == "slam" else fx
    family = cap.font_for(style["font"], text, lines.FONT)
    fill, hi = _hex(style["color"]), _hex(style["accent"])
    moves = cap._moves(moved, 5, x, y, size, begins, dur_ms, into_ms, 7)
    extra = f"\\frz{style['tilt']:.1f}" if style["tilt"] and fx not in ("shake", "wobble", "wave") else ""
    if begins and fx == "slam":
        extra += f"\\alpha&HFF&\\t(0,{SLAM_MS},\\alpha&H00&)"
    if style["look"] == "bubble":
        ring = _dark(fill)
        layers = [(3, f"\\bord{size * cap.OUTER:.1f}\\shad0\\3c{cap._ass_color(ring)}", ring, ring, "Callout"),
                  (4, f"\\bord{size * cap.INNER:.1f}\\shad0\\3c&HFFFFFF&", fill, hi, "Callout")]
    elif style["look"] == "box":
        layers = [(3, f"\\bord{size * 0.28:.1f}\\shad0\\3c{cap._ass_color(_hex(style['box']))}"
                      f"\\4c{cap._ass_color(_hex(style['box']))}", fill, hi, "Label")]
    else:
        layers = [(3, f"\\bord{size * 0.12:.1f}\\shad{size * 0.05:.1f}\\3c&H000000&", fill, hi, "Callout")]
    out = []
    for layer, outline, col, hi_col, ass_style in layers:
        for m0, m1, place in moves:
            if m1 <= m0:
                continue
            body = _body(rows, col, hi_col, size, fx, begins and m0 == 0, dur_ms, into_ms)
            out.append(f"Dialogue: {layer},{lines._ass_time(m0 / 1000)},{lines._ass_time(m1 / 1000)},{ass_style},,0,0,0,,"
                       f"{{\\fn{family}\\fs{round(size)}\\b1\\q2{place}{outline}{extra}"
                       f"{_fade(m0, m1, dur_ms) if ends else ''}}}{body}")
    return out


def _fade(m0: int, m1: int, dur_ms: int) -> str:
    """Fading out over the hook's last FADE_MS: \\fad on one long event; a shake's short
    steps each get the see-through-ness of their middle instead (\\fad would fade every step)."""
    fade = min(FADE_MS, dur_ms // 2)
    if m1 - m0 > 2 * fade:
        return f"\\fad(0,{fade})"
    left = dur_ms - (m0 + m1) / 2
    if left >= fade:
        return ""
    return f"\\alpha&H{round(255 * (1 - max(left, 0) / fade)):02X}&"


def _body(rows: list[str], fill: str, hi: str, size: float, fx: str, begins: bool, dur_ms: int, into_ms: int) -> str:
    if fx != "slam":
        return cap._body(rows, fill, hi, size, fx, begins, dur_ms, into_ms)
    body = cap._body(rows, fill, hi, size, "none", False, dur_ms, into_ms)
    if not begins:
        return body
    # A slam: every run starts big and drops to its size (each run sets its own scale).
    return re.sub(r"\\fscx(\d+)\\fscy(\d+)",
                  lambda m: (f"\\fscx{int(m[1]) * 26 // 10}\\fscy{int(m[2]) * 26 // 10}"
                             f"\\t(0,{SLAM_MS},\\fscx{m[1]}\\fscy{m[2]})"), body)


LINE = "seconds | font | motion | look | colour | accent | box colour | where | size | tilt"


def parse(item: Any) -> dict[str, Any]:
    """The AI edit's hook_style ("3 | loud | slam | outline | yellow | red | red | upper |
    big | -6", LINE) as a hook style; "default"/"" for the subtitles' font, and anything
    unknown is the default."""
    bits = [b.strip().lower() for b in str(item or "").split("|")]
    keys = ("s", "font", "fx", "look", "color", "accent", "box", "at", "size", "tilt")
    raw = {k: v for k, v in zip(keys, bits) if v}
    if raw.get("font") in ("default", "classic", "plain"):
        raw["font"] = ""
    for k in ("s", "tilt"):
        raw[k] = raw.get(k, "").rstrip("s°").strip() or DEFAULT[k]
    return clean(raw)


def line(style: dict[str, Any]) -> str:
    """A hook style written the way parse() reads it (for telling Claude the current one)."""
    st = clean(style)
    return " | ".join(str(v) for v in (f"{st['s']:g}", st["font"] or "default", st["fx"], st["look"], st["color"],
                                       st["accent"], st["box"], st["at"], st["size"], f"{st['tilt']:g}"))


def info() -> dict[str, Any]:
    """What the editor needs to draw and edit the same."""
    return {"s": HOOK_S, "fonts": FONTS, "fx": FX, "looks": LOOKS, "places": PLACES, "sizes": SIZES,
            "colors": COLORS, "tilt": TILT, "slam_ms": SLAM_MS, "fade_ms": FADE_MS, "default": DEFAULT}
