"""Caption styles: the "bubble" look, and caption styling the AI edit (or you) puts on
stretches of the clip: an effect, a font and a place.

The bubble look (subs_style "look": "bubble") is the clip channels' Shorts look: a round,
chunky font; the text in a deep shade of the speaker's colour; a thick white outline with
a thin dark ring round it; *highlighted* words bigger. Every caption springs in.

A caption style on an edit is an effect {"type": "caption", "t", "d", "fx", "font", "at"}:
the captions that start in [t, t + d) get it.
    fx    pop: springs in · shake: rattles (anger, screaming, a scare) · wave: the letters
          bob (sing-song, teasing, smug) · wobble: rocks side to side (confused, silly) ·
          bounce: drops in and squashes (a punchline, a deadpan answer) · grow: swells (a
          build-up, a slow realisation) · none
    font  round (the default) · angry (dripping paint) · scary (horror) · loud (comic capitals)
    at    "" (the subtitles' own place) · top · bottom · middle · seam (on the line between a
          Short's two pictures) · a region (on it, low: under the face of whoever talks)

Drawn by libass from the fonts in FONT_DIR (shipped with the app, OFL), so a render needs
no installed fonts. editor.js draws the same for its preview (drawSubPreview, capAnim):
change both together."""

from __future__ import annotations

import colorsys
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chatwatcher.edit import layout as lay
from chatwatcher.subs import lines

CAPTION = "caption"
FONT_DIR = Path(__file__).resolve().parent.parent / "web" / "static" / "fonts"
FONTS = {"round": "Fredoka", "angry": "Rubik Wet Paint", "scary": "Creepster", "loud": "Bangers"}
FONT_FILES = {"round": "Fredoka-Bold.ttf", "angry": "RubikWetPaint-Regular.ttf", "scary": "Creepster-Regular.ttf",
              "loud": "Bangers-Regular.ttf"}
CJK_FONT = "Rounded Mplus 1c ExtraBold"     # the fonts above have no Japanese: lines with it use this
CJK_FILE = "MPLUSRounded1c-ExtraBold.ttf"
FX = ("pop", "shake", "wave", "wobble", "bounce", "grow", "none")
PLACES = ("top", "bottom", "middle", "seam")
D_LIMITS = (2.0, 0.2, 60.0)    # default, shortest and longest, seconds

OUTER, INNER = 0.20, 0.13      # the dark ring and the white outline, as shares of the font size
CLASSIC_BORD = 0.10
HI_SCALE = 1.22                # *highlighted* words, this much bigger
BUBBLE_SCALE = 1.15            # the bubble look's text, this much bigger than the size setting (round fonts read small)
DEFAULT_FILL = "6B2FA3"        # a caption with no voice: deep purple
LOW = 0.78                     # "at" a region: this far down it (under the face, not over the eyes)
POP_MS = 170
SHAKE_STEP_MS = 50
SHAKE_AMP = 0.07               # of the font size
SHAKE_TILT = 4.0
WAVE_AMP = 0.16                # of the font size
WAVE_PERIOD_MS = 700
WAVE_STEP_MS = 100
WAVE_PHASE = 0.9               # radians between two letters
WOBBLE_DEG = 6.0
WOBBLE_PERIOD_MS = 520
BOUNCE_MS = 150
GROW_TO = 1.25
ORG_FAR = 100000.0             # wave: letters turn round a point this far left, so they move up and down
LINE_H = 1.15                  # a line's height, as a multiple of the font size (for stacking)
CHAR_W = 0.56                  # an average character's width (CJK counts 2 halves)


# ── colours ──────────────────────────────────────────────────────────────────

def _hls(rgb: str) -> tuple[float, float, float]:
    return colorsys.rgb_to_hls(*(int(rgb[i:i + 2], 16) / 255 for i in (0, 2, 4)))


def _rgb(h: float, l: float, s: float) -> str:
    return "".join(f"{round(v * 255):02X}" for v in colorsys.hls_to_rgb(h, l, s))


def deep(rgb: str | None) -> str:
    """A voice's colour as a bubble caption's fill: the same hue, dark and strong enough to
    read inside a white outline (voice colours are light pastels, made for a black one)."""
    if not rgb or not re.fullmatch(r"[0-9A-Fa-f]{6}", rgb):
        return DEFAULT_FILL
    h, l, s = _hls(rgb)
    if s < 0.12:                                 # white or grey: no hue to keep
        return DEFAULT_FILL
    return _rgb(h, min(l, 0.38), max(s, 0.6))


def ring(fill: str) -> str:
    """The thin ring round the white outline: the fill, much darker."""
    h, l, s = _hls(fill)
    return _rgb(h, l * 0.38, s)


def accent(fill: str) -> str:
    """*Highlighted* words in a bubble caption: hot pink, or blue when the fill is pink/red already."""
    h, _l, _s = _hls(fill)
    return "2F6BE6" if h < 0.05 or h > 0.85 else "E62E6B"


def _ass_color(rgb: str) -> str:
    return f"&H{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}&"


# ── caption styles on an edit ────────────────────────────────────────────────

@dataclass
class Mark:
    t: float                    # clip time: captions starting in [t, t + d) get this
    d: float
    fx: str
    font: str                   # a FONTS key, or "" for the look's own
    at: str                     # "" | top | bottom (an edge) | a point, in xy
    xy: tuple[float, float] | None = None    # middle / seam / a region: the caption's centre, shares of the picture


def seam_y(panels: list[lay.Panel] | None) -> float:
    """The line between a Short's pictures nearest the middle (0.5 when there's one picture)."""
    edges = {round(y, 4) for p in panels or [] if not p.blur for y in (p.dst[1], p.dst[1] + p.dst[3])}
    inner = [y for y in edges if 0.12 < y < 0.88]
    return min(inner, key=lambda y: abs(y - 0.5)) if inner else 0.5


def anchor(at: str, boxes: dict[str, dict[str, float]], panels: list[lay.Panel] | None) -> tuple[float, float] | None:
    """Where `at` puts a caption's centre on the output picture; None: the subtitles' own place
    (an edge, or a region that isn't drawn or isn't on screen)."""
    if at == "middle":
        return 0.5, 0.5
    if at == "seam":
        return 0.5, seam_y(panels)
    box = boxes.get(at) if at and at not in PLACES else None
    if not box:
        return None
    cx, low = box["x"] + box["w"] / 2, box["y"] + box["h"] * LOW
    if panels is None:
        return cx, low
    return lay.map_point(cx, low, panels) or lay.map_point(cx, box["y"] + box["h"] / 2, panels)


def placed(effects: list[dict[str, Any]], boxes: dict[str, dict[str, float]],
           panels: list[lay.Panel] | None) -> list[Mark]:
    """The caption styles of these effects (clip time), placed on the output picture.
    panels: a Short's picture (None for 16:9)."""
    out = []
    for fx in effects:
        if fx.get("type") != CAPTION:
            continue
        at = str(fx.get("at") or "")
        out.append(Mark(float(fx["t"]), float(fx.get("d") or D_LIMITS[0]),
                        fx.get("fx") if fx.get("fx") in FX else "pop",
                        fx.get("font") if fx.get("font") in FONTS else "", at,
                        None if at in ("", "top", "bottom") else anchor(at, boxes, panels)))
    return sorted(out, key=lambda m: m.t)


def mark_for(t0: float, marks: list[Mark]) -> Mark | None:
    """The style of a caption starting at t0: the latest mark that covers it."""
    on = [m for m in marks if m.t - 0.02 <= t0 < m.t + m.d]
    return on[-1] if on else None


def styled(style: dict[str, Any], marks: list[Mark] | None) -> bool:
    """Whether captions are drawn here (rather than by subs.lines.ass's plain lines)."""
    return style.get("look") == "bubble" or bool(marks)


# ── drawing them (ASS) ───────────────────────────────────────────────────────

@dataclass
class Shown:
    a: float                    # when it shows in this part, part time
    b: float
    text: str
    color: str | None           # the voice's colour
    begins: bool                # it starts in this part (so it springs in here)
    into: float                 # seconds of it already shown before this part
    mark: Mark | None


def local(cues: list[dict[str, Any]], start: float, length: float, marks: list[Mark] | None) -> list[Shown]:
    """The cues showing in [start, start + length) of the clip, in part time, with their style."""
    out = []
    for c in cues:
        a, b = max(c["t0"], start), min(c["t1"], start + length)
        if b > a and b - a >= min(lines.MIN_SHOWN_S, c["t1"] - c["t0"]):
            out.append(Shown(a - start, b - start, c["text"], c.get("color"), a - c["t0"] < 0.02, a - c["t0"],
                             mark_for(c["t0"], marks or [])))
    return sorted(out, key=lambda s: (s.a, s.b))


def font_for(key: str, text: str, classic: str) -> str:
    """The family to draw this text in: the mark's font, else the look's; Japanese/Korean/
    Chinese text in the rounded CJK font (the display fonts don't have it)."""
    if not key and classic:
        return classic
    if lines.width(text) != len(text.replace("*", "")):
        return CJK_FONT
    return FONTS.get(key or "round", FONTS["round"])


def _esc(s: str) -> str:
    return s.replace("\\", "＼").replace("{", "｛").replace("}", "｝")


def _scale(fx: str, begins: bool, dur_ms: int, into_ms: int, k: float = 1.0) -> str:
    """The \\fscx/\\fscy tags (with their \\t) for an effect, at k times the size. Highlighted
    runs repeat them at their own k: an inline \\fscx would stop the line's animation."""
    p = lambda v: round(v * k * 100)            # noqa: E731
    out = f"\\fscx{p(1)}\\fscy{p(1)}"
    if begins and fx in ("pop", "shake", "wave", "wobble", "grow"):
        h = POP_MS // 2
        out = (f"\\fscx{p(0.4)}\\fscy{p(0.4)}\\t(0,{h},\\fscx{p(1.15)}\\fscy{p(1.15)})"
               f"\\t({h},{POP_MS},\\fscx{p(1)}\\fscy{p(1)})")
    elif begins and fx == "bounce":
        out += (f"\\t({BOUNCE_MS},{BOUNCE_MS + 60},\\fscx{p(1.25)}\\fscy{p(0.78)})"
                f"\\t({BOUNCE_MS + 60},{BOUNCE_MS + 170},\\fscx{p(1)}\\fscy{p(1)})")
    if fx == "grow":
        total = dur_ms + into_ms
        now = 1 + (GROW_TO - 1) * (into_ms / total if total else 0)
        if not begins:
            out = f"\\fscx{p(now)}\\fscy{p(now)}"
        out += f"\\t({POP_MS if begins else 0},{max(dur_ms, 1)},\\fscx{p(GROW_TO)}\\fscy{p(GROW_TO)})"
    return out


def _wave_tags(i: int, size: float, dur_ms: int, into_ms: int) -> str:
    """Letter i's \\frz keyframes: turning it round a point ORG_FAR to the left moves it up
    and down (by ORG_FAR × sin(angle)), so each letter bobs, a little after the one before."""
    amp = WAVE_AMP * size

    def angle(ms: int) -> float:
        dy = amp * math.sin(2 * math.pi * (ms + into_ms) / WAVE_PERIOD_MS - i * WAVE_PHASE)
        return math.degrees(math.asin(dy / ORG_FAR))

    out = f"\\frz{angle(0):.6f}"
    for ms in range(WAVE_STEP_MS, dur_ms + WAVE_STEP_MS, WAVE_STEP_MS):
        out += f"\\t({ms - WAVE_STEP_MS},{ms},\\frz{angle(ms):.6f})"
    return out


def _body(rows: list[str], fill: str, hi_fill: str, size: float, fx: str, begins: bool,
          dur_ms: int, into_ms: int) -> str:
    """The caption's text with its colour (and size, for highlighted words) runs; a wave
    gives each letter its own tags."""
    out, on, i = [], False, 0
    for row in rows:
        pieces, on_after = lines.runs(row, on)
        parts = []
        for text, hi in pieces:
            head = f"\\1c{_ass_color(hi_fill if hi else fill)}" + _scale(fx, begins, dur_ms, into_ms, HI_SCALE if hi else 1)
            if fx == "wave":
                for j, ch in enumerate(text):
                    parts.append("{" + (head if j == 0 else "") + _wave_tags(i, size, dur_ms, into_ms) + "}" + _esc(ch))
                    i += 1
            else:
                parts.append("{" + head + "}" + _esc(text))
                i += len(text)
        out.append("".join(parts))
        on = on_after
    return "\\N".join(out)


def _jitter(seed: int, k: int) -> tuple[float, float, float]:
    """Shake step k's offset and tilt, from -1 to 1 each (the same every render)."""
    def r(n: int) -> float:
        x = math.sin(seed * 12.9898 + k * 78.233 + n * 37.719) * 43758.5453
        return (x - math.floor(x)) * 2 - 1
    return r(1), r(2), r(3)


def _moves(fx: str, an: int, x: float, y: float, size: float, begins: bool, dur_ms: int, into_ms: int,
           seed: int) -> list[tuple[int, int, str]]:
    """(from ms, to ms, placing tags) for the caption's events: one, or a shake's many."""
    base = f"\\an{an}"
    if fx == "shake":
        out, ms = [], 0
        if begins:                                  # springs in first, then rattles
            out.append((0, min(POP_MS, dur_ms), f"{base}\\pos({x:.0f},{y:.0f})"))
            ms = POP_MS
        k = 0
        while ms < dur_ms:
            jx, jy, jr = _jitter(seed, k)
            amp = SHAKE_AMP * size
            out.append((ms, min(ms + SHAKE_STEP_MS, dur_ms),
                        f"{base}\\pos({x + jx * amp:.0f},{y + jy * amp:.0f})\\frz{jr * SHAKE_TILT:.1f}"))
            ms += SHAKE_STEP_MS
            k += 1
        return out
    if fx == "bounce" and begins:
        return [(0, dur_ms, f"{base}\\move({x:.0f},{y - size * 1.2:.0f},{x:.0f},{y:.0f},0,{BOUNCE_MS})")]
    if fx == "wave":
        return [(0, dur_ms, f"{base}\\pos({x:.0f},{y:.0f})\\org({x - ORG_FAR:.0f},{y:.0f})")]
    if fx == "wobble":
        tags, step = f"{base}\\pos({x:.0f},{y:.0f})", WOBBLE_PERIOD_MS // 4
        angle = lambda ms: WOBBLE_DEG * math.sin(2 * math.pi * (ms + into_ms) / WOBBLE_PERIOD_MS)   # noqa: E731
        tags += f"\\frz{angle(0):.1f}"
        for ms in range(step, dur_ms + step, step):
            tags += f"\\t({ms - step},{ms},\\frz{angle(ms):.1f})"
        return [(0, dur_ms, tags)]
    return [(0, dur_ms, f"{base}\\pos({x:.0f},{y:.0f})")]


def events(shown: list[Shown], style: dict[str, Any], w: int, h: int, *, title: bool = False,
           font: str = lines.FONT) -> list[str]:
    """ASS Dialogue lines (style Callout of subs.lines.ass, everything else in tags) for these
    captions. Bubble look: two layers each, the dark ring under the white outline. Classic
    look with a caption style: the plain white-on-black captions, moved, animated and in
    the style's font. Captions at an edge stack like libass stacks plain ones."""
    rules = lines.frame_rules(w, h)
    bubble = style.get("look") == "bubble"
    size = lines.SIZES.get(style.get("size"), lines.SIZES["normal"]) * (w if rules["vertical"] else h)
    size *= BUBBLE_SCALE if bubble else 1
    top_edge = h * (rules["top_title" if title else "top"])
    bottom_edge = h * (1 - rules["bottom"])
    stacked: list[tuple[float, float, str, float]] = []      # (a, b, edge, height) of edge captions so far
    out = []
    for n, s in enumerate(shown):
        mark = s.mark
        fx = mark.fx if mark else ("pop" if bubble else "none")
        rows = lines.wrap(s.text, rules["max_line"], rules["max_lines"])
        height = len(rows) * size * LINE_H
        edge = (mark.at if mark and mark.at in ("top", "bottom") else
                None if mark and mark.xy else ("top" if style.get("position") == "top" else "bottom"))
        if edge:
            below = sum(hh for a, b, e, hh in stacked if e == edge and a < s.b and s.a < b)
            stacked.append((s.a, s.b, edge, height))
            x, y, an = w / 2, (top_edge + below if edge == "top" else bottom_edge - below), (8 if edge == "top" else 2)
        else:
            assert mark and mark.xy
            widest = max(lines.width(r) for r in rows) * CHAR_W * size * (HI_SCALE if "*" in s.text else 1)
            half_w = min(widest / 2 + size * 0.3, w / 2)
            x = min(max(mark.xy[0] * w, half_w), w - half_w)
            y = min(max(mark.xy[1] * h, height / 2 + h * 0.04), h * 0.96 - height / 2)
            an = 5
        dur_ms, into_ms = max(1, round((s.b - s.a) * 1000)), round(s.into * 1000)
        family = font_for(mark.font if mark else ("round" if bubble else ""), s.text, "" if bubble else font)
        seed = n + 1 + round(s.a * 100)
        moves = _moves(fx, an, x, y, size, s.begins, dur_ms, into_ms, seed)
        if bubble:
            fill = deep(s.color)
            dark = ring(fill)
            layers = [(0, f"\\bord{size * OUTER:.1f}\\shad0\\3c{_ass_color(dark)}", dark, dark),
                      (1, f"\\bord{size * INNER:.1f}\\shad0\\3c&HFFFFFF&", fill, accent(fill))]
        else:
            fill = (s.color or "FFFFFF").upper()
            hi = lines.HIGHLIGHT if fill != lines.HIGHLIGHT else "FFFFFF"
            layers = [(0, f"\\bord{max(1.0, size * CLASSIC_BORD):.1f}\\shad{size * 0.045:.1f}\\3c&H000000&", fill, hi)]
        for layer, outline, col, hi_col in layers:
            body = _body(rows, col, hi_col, size, fx, s.begins, dur_ms, into_ms)
            for m0, m1, place in moves:
                if m1 <= m0:
                    continue
                # A shake's later steps start mid-way: their scale is already settled.
                body_here = body if m0 == 0 else _body(rows, col, hi_col, size, fx, False, dur_ms, into_ms)
                out.append(f"Dialogue: {layer},{lines._ass_time(s.a + m0 / 1000)},{lines._ass_time(s.a + m1 / 1000)},"
                           f"Callout,,0,0,0,,{{\\fn{family}\\fs{round(size)}\\b1\\q2{place}{outline}}}{body_here}")
    return out


def font_files() -> list[Path]:
    return [FONT_DIR / f for f in (*FONT_FILES.values(), CJK_FILE) if (FONT_DIR / f).is_file()]


def info() -> dict[str, Any]:
    """What the editor needs to draw the same (sub_layout.captions)."""
    return {"fonts": FONTS, "font_files": FONT_FILES, "cjk_font": CJK_FONT, "cjk_file": CJK_FILE, "fx": FX,
            "places": PLACES, "looks": lines.LOOKS, "d": D_LIMITS, "outer": OUTER, "inner": INNER,
            "classic_bord": CLASSIC_BORD, "hi_scale": HI_SCALE, "bubble_scale": BUBBLE_SCALE, "default_fill": DEFAULT_FILL, "low": LOW,
            "pop_ms": POP_MS, "shake": [SHAKE_STEP_MS, SHAKE_AMP, SHAKE_TILT],
            "wave": [WAVE_AMP, WAVE_PERIOD_MS, WAVE_PHASE], "wobble": [WOBBLE_DEG, WOBBLE_PERIOD_MS],
            "bounce_ms": BOUNCE_MS, "grow_to": GROW_TO, "line_h": LINE_H, "char_w": CHAR_W}
