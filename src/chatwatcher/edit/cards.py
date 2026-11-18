"""Cards: a full screen (or a sticker) that says something the way clip channels do, placed
by you or the AI edit.

- died: the picture darkens, a dark band across the middle, a red serif "YOU DIED" fading
  in and slowly growing (the ironic death after "it's not our time yet").
- poster: a black screen with big words revealed one by one, the last line (or *starred*
  words) in a highlight colour (the demotivational / hopecore poster).
- flat: a plain screen with one line on it ("3 HOURS LATER", "MEANWHILE…").
- comment: a "reply to a comment" sticker (a white bubble, a grey avatar, the comment) that
  pops in over the picture, usually as the opener.

A card on an edit is an effect {"type": "card", "t", "d", "style", "text", "color", "bg",
"who", "at", "dx", "dy"}: color is the text's (poster: the highlight's), bg the screen's
(black, white, dim: the picture darkened), who the comment's author, at / dx / dy where a
comment sticker sits (like a text callout). Font sizes are shares of the picture's shorter
side, so a card reads the same on a Short and across 16:9.

Cards are drawn by libass in the same .ass file as the captions and text callouts, on
layers above them (a full card hides the captions under it). editor.js draws the same for
its preview (static/editor/overlays.js: drawCards): change both together."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from chatwatcher.edit import layout as lay
from chatwatcher.edit import overlays as ovl
from chatwatcher.subs import lines

CARD = "card"
STYLES: dict[str, dict[str, Any]] = {      # defaults per style
    "died": {"text": "YOU DIED", "d": 3.0, "bg": "dim", "color": "blood", "font": "Cinzel", "size": 0.13},
    "poster": {"text": "YOU DON'T NEED TO EXCEL AT ANYTHING", "d": 2.5, "bg": "black", "color": "lavender",
               "font": "Archivo Black", "size": 0.115},
    "flat": {"text": "3 HOURS LATER", "d": 1.5, "bg": "black", "color": "white", "font": "Archivo Black", "size": 0.085},
    "comment": {"text": "I'm giving burnt out gifted child", "d": 3.0, "bg": "", "color": "ink", "font": "Fredoka",
                "size": 0.042},
}
FONT_FILES = {"Cinzel": "Cinzel.ttf", "Archivo Black": "ArchivoBlack-Regular.ttf"}   # Fredoka: the captions'
COLORS = {"white": "FFFFFF", "blood": "A11A1A", "red": "FF3B30", "yellow": "FFD600", "lavender": "C9A2FF",
          "green": "3DDC84", "blue": "4DA3FF", "pink": "FF5FA2", "ink": "161823", "black": "000000"}
BGS = {"black": "000000", "white": "FFFFFF", "dim": ""}
MAX_CHARS = 80
MAX_WHO = 24
D_LIMITS = (2.0, 0.3, 10.0)
POSTER_LINE = 11            # a poster wraps at about this many characters a line
COMMENT_LINE = 26
DIM_ALPHA = 0.62            # how dark "dim" makes the picture
DIED_FADE_MS, DIED_GROW = 900, 1.08
POSTER_STEP = (0.06, 0.28)  # seconds between revealed words: at least, at most
FADE_MS = 120
POP_MS = 180
CHAR_W = 0.56               # an average character's width, as a share of the font size (comment bubble)
DEFAULT_DY = {"comment": -0.3}


def text_for(fx: dict[str, Any]) -> str:
    return fx.get("text") or STYLES[fx.get("style") or "flat"]["text"]


def clean(fx: dict[str, Any], i: int) -> dict[str, Any]:
    """The card fields of effect i (from the browser or Claude). Raises ValueError with a
    message for the editor."""
    style = fx.get("style") if fx.get("style") in STYLES else "flat"
    base = STYLES[style]
    text = " ".join(re.sub("[\U00010000-\U0010FFFF️‍]", "", str(fx.get("text") or "")).split())[:MAX_CHARS]
    try:
        d = float(fx.get("d", base["d"]))
        dx, dy = float(fx.get("dx") or 0), float(fx.get("dy", DEFAULT_DY.get(style, 0.0)) or 0)
    except (TypeError, ValueError):
        raise ValueError(f"Effect {i} has a bad length or position.")
    if not all(math.isfinite(v) for v in (d, dx, dy)):
        raise ValueError(f"Effect {i} has a bad length or position.")
    at = str(fx.get("at") or "middle")
    lo, hi = ovl.OFFSET_RANGE
    out = {"style": style, "text": text or base["text"],
           "d": round(min(max(d, D_LIMITS[1]), D_LIMITS[2]), 3),
           "color": fx.get("color") if fx.get("color") in COLORS else base["color"],
           "bg": fx.get("bg") if fx.get("bg") in BGS else base["bg"]}
    if style == "comment":
        from chatwatcher.edit import regions
        out |= {"who": " ".join(str(fx.get("who") or "").split())[:MAX_WHO],
                "at": at if at in ovl.PLACES else (regions.normal_name(at) or "middle"),
                "dx": round(min(max(dx, lo), hi), 4), "dy": round(min(max(dy, lo), hi), 4), "bg": ""}
    return out


# ── on an edit ───────────────────────────────────────────────────────────────

@dataclass
class Placed:
    t: float                    # clip time
    d: float
    style: str
    text: str
    color: str                  # RRGGBB
    bg: str                     # RRGGBB, or "" (dim / none)
    who: str = ""
    x: float = 0.5              # a comment's centre, as shares of the output picture
    y: float = 0.5


def placed(effects: list[dict[str, Any]], boxes: dict[str, dict[str, float]],
           panels: list[lay.Panel] | None) -> list[Placed]:
    out = []
    for fx in effects:
        if fx.get("type") != CARD:
            continue
        style = fx.get("style") if fx.get("style") in STYLES else "flat"
        base = STYLES[style]
        color = COLORS.get(fx.get("color") or "", COLORS[base["color"]])
        bg = BGS.get(fx.get("bg") if fx.get("bg") in BGS else base["bg"], "")
        if style == "flat" and bg and bg.upper() == color.upper():   # never white on white
            color = "000000" if bg.upper() == "FFFFFF" else "FFFFFF"
        x, y = 0.5, 0.5
        if style == "comment":
            x, y = ovl.place(fx.get("at") or "middle", boxes, panels, float(fx.get("dx") or 0),
                             float(fx.get("dy", DEFAULT_DY["comment"]) or 0))
        out.append(Placed(fx["t"], fx["d"], style, text_for(fx), color, bg, str(fx.get("who") or ""), x, y))
    return out


def local(items: list[Placed], start: float, length: float) -> list[tuple[Placed, float, float, float]]:
    """(card, when it starts in this part, how long it shows here, how far into the card
    this part begins) for each card showing in [start, start + length)."""
    out = []
    for p in items:
        a, b = max(p.t, start), min(p.t + p.d, start + length)
        if b - a > 0.04:
            out.append((p, a - start, b - a, a - p.t))
    return out


def _c(rgb: str) -> str:
    return f"&H{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}&"


def _esc(s: str) -> str:
    return s.replace("\\", "＼").replace("{", "｛").replace("}", "｝")


def _rect(w: float, h: float) -> str:
    return f"m 0 0 l {w:.0f} 0 {w:.0f} {h:.0f} 0 {h:.0f}"


def _round_rect(w: float, h: float, r: float) -> str:
    """An ASS drawing of a w × h box with corners of radius r (bezier quarter circles)."""
    k = r * 0.5523
    return (f"m {r:.0f} 0 l {w - r:.0f} 0 b {w - r + k:.0f} 0 {w:.0f} {r - k:.0f} {w:.0f} {r:.0f} "
            f"l {w:.0f} {h - r:.0f} b {w:.0f} {h - r + k:.0f} {w - r + k:.0f} {h:.0f} {w - r:.0f} {h:.0f} "
            f"l {r:.0f} {h:.0f} b {r - k:.0f} {h:.0f} 0 {h - r + k:.0f} 0 {h - r:.0f} "
            f"l 0 {r:.0f} b 0 {r - k:.0f} {r - k:.0f} 0 {r:.0f} 0")


def _circle(d: float) -> str:
    return _round_rect(d, d, d / 2)


def greedy(text: str, width: int) -> list[str]:
    """Words into lines of about `width` characters (a long word gets its own line)."""
    out: list[str] = []
    for word in text.split():
        if out and lines.width(out[-1]) + 1 + lines.width(word) <= width:
            out[-1] += " " + word
        else:
            out.append(word)
    return out or [""]


def poster_words(text: str) -> tuple[list[list[str]], set[int]]:
    """A poster's lines of words (capitals, stars removed) and the indexes (across all
    words) of the highlighted ones: the *starred* words, or else the last line."""
    words, hi, inside = [], set(), False
    for k, raw in enumerate(text.upper().split()):
        if raw.startswith("*"):
            inside = True
        if inside:
            hi.add(k)
        if raw.endswith("*") and (len(raw) > 1 or not raw.startswith("*")):
            inside = False
        words.append(raw.strip("*") or raw)
    rows, k = [], 0
    for line in greedy(" ".join(words), POSTER_LINE):
        n = len(line.split())
        rows.append(words[k:k + n])
        k += n
    if not hi and rows:
        hi = set(range(len(words) - len(rows[-1]), len(words)))
    return rows, hi


def poster_step(n: int, d: float) -> float:
    return min(max(0.5 * d / max(n, 1), POSTER_STEP[0]), POSTER_STEP[1])


def events(items: list[tuple[Placed, float, float, float]], w: int, h: int) -> list[str]:
    """ASS Dialogue lines for these cards (style Callout of subs.lines.ass, other fonts
    by name), on layers 10+ (above the captions and text callouts)."""
    out: list[str] = []
    unit = min(w, h)
    T = lines._ass_time

    def ev(layer: int, a: float, b: float, tags: str, body: str) -> None:
        if b - a > 0.005:
            out.append(f"Dialogue: {layer},{T(a)},{T(b)},Callout,,0,0,0,,{{{tags}}}{body}")

    for p, a, d, into in items:
        z = a + d
        begins, left_ms = into < 0.02, round(d * 1000)
        ends = p.t + p.d - (into + d) < 0.02
        out_ms = min(FADE_MS, left_ms // 3) if ends else 0
        st = STYLES[p.style]
        fs = max(8, round(st["size"] * unit))
        if p.bg or p.style == "died":                       # the screen under it
            if p.bg:
                fade = f"\\fad({FADE_MS if begins and p.style != 'poster' else 0},{out_ms})"
                ev(10, a, z, f"\\an7\\pos(0,0)\\p1\\bord0\\shad0\\1c{_c(p.bg)}\\1a&H00&{fade}", _rect(w, h))
            else:                                           # dim: the picture darkened
                alpha = f"{round(255 * (1 - DIM_ALPHA)):02X}"
                grow = f"\\1a&HFF&\\t(0,{DIED_FADE_MS // 2},\\1a&H{alpha}&)" if begins else f"\\1a&H{alpha}&"
                ev(10, a, z, f"\\an7\\pos(0,0)\\p1\\bord0\\shad0\\1c&H000000&{grow}\\fad(0,{out_ms})", _rect(w, h))
        if p.style == "died":
            band = round(fs * 2.4)
            fade = f"\\fad({DIED_FADE_MS // 2 if begins else 0},{out_ms})"
            ev(11, a, z, f"\\an4\\pos({-fs},{h // 2})\\p1\\bord0\\shad0\\1c&H000000&\\1a&H38&\\blur{fs * 0.35:.0f}{fade}",
               _rect(w + 2 * fs, band))
            grow = "" if not begins else (f"\\alpha&HFF&\\t(0,{DIED_FADE_MS},\\alpha&H00&)"
                                          f"\\t(0,{max(left_ms, 1)},\\fscx{DIED_GROW * 100:.0f}\\fscy{DIED_GROW * 100:.0f})")
            ev(12, a, z, f"\\an5\\pos({w // 2},{h // 2})\\fn{st['font']}\\fs{fs}\\fsp{fs * 0.08:.1f}\\bord0\\shad0"
                         f"\\blur0.8\\1c{_c(p.color)}{grow}\\fad(0,{out_ms})", _esc(p.text.upper()))
        elif p.style == "flat":
            ev(12, a, z, f"\\an5\\pos({w // 2},{h // 2})\\fn{st['font']}\\fs{fs}\\bord0\\shad0\\1c{_c(p.color)}"
                         f"\\q2\\fad({FADE_MS if begins else 0},{out_ms})",
               "\\N".join(_esc(r) for r in greedy(p.text.upper(), 16)))
        elif p.style == "poster":
            rows, hi = poster_words(p.text)
            n = sum(len(r) for r in rows)
            step = poster_step(n, p.d)
            x0, y0 = round(w * 0.09), round(h * 0.42)
            # One event per reveal: every word is laid out from the start (so nothing moves),
            # the ones not said yet are see-through. Card time c ↔ part time a + c - into.
            for k in range(n):
                ga = max(a, a + k * step - into)
                gb = min(z, a + (k + 1) * step - into) if k + 1 < n else z
                if gb - ga <= 0.005:
                    continue
                body, i = [], 0
                for r in rows:
                    ws = []
                    for word in r:
                        col = _c(p.color) if i in hi else "&HFFFFFF&"
                        ws.append(f"{{\\1c{col}\\alpha&H{'00' if i <= k else 'FF'}&}}{_esc(word)}")
                        i += 1
                    body.append(" ".join(ws))
                fade = f"\\fad(0,{out_ms})" if gb >= z - 0.005 else ""
                ev(12, ga, gb, f"\\an4\\pos({x0},{y0})\\fn{st['font']}\\fs{fs}\\bord0\\shad0\\q2{fade}",
                   "\\N".join(body))
        elif p.style == "comment":
            rows = greedy(p.text, COMMENT_LINE)[:3]
            who = f"Reply to {p.who}'s comment" if p.who else ""
            small = round(fs * 0.62)
            text_w = max([lines.width(r) for r in rows] + [round(lines.width(who) * 0.62)]) * CHAR_W * fs
            av = round(fs * 1.5)
            pad = round(fs * 0.55)
            bw = min(round(text_w + av + pad * 3), round(w * 0.92))
            bh = round(max(av, len(rows) * fs * 1.2 + (small * 1.25 if who else 0)) + pad * 2)
            cx, cy = round(p.x * w), round(p.y * h)
            left, top = cx - bw // 2, cy - bh // 2
            rise = round(fs * 0.4)

            def at(x: int, y: int) -> str:
                return (f"\\move({x},{y + rise},{x},{y},0,{POP_MS})" if begins else f"\\pos({x},{y})")
            fade = f"\\fad({POP_MS if begins else 0},{out_ms})"
            ev(11, a, z, f"\\an7{at(left, top)}\\p1\\bord0\\shad{max(1, fs // 10)}\\4c&H000000&\\4a&HB0&"
                         f"\\1c&HFFFFFF&{fade}", _round_rect(bw, bh, min(fs * 0.7, bh / 2)))
            ev(12, a, z, f"\\an7{at(left + pad, top + pad)}\\p1\\bord0\\shad0\\1c&HC8C8C8&{fade}", _circle(av))
            tx = left + pad * 2 + av
            body = (f"{{\\fs{small}\\1c&H8A8A8A&}}{_esc(who)}\\N{{\\fs{fs}\\1c{_c(p.color)}}}" if who else "") + \
                "\\N".join(_esc(r) for r in rows)
            ev(12, a, z, f"\\an7{at(tx, top + pad)}\\fnFredoka\\fs{fs}\\bord0\\shad0\\q2\\1c{_c(p.color)}{fade}", body)
    return out


def info() -> dict[str, Any]:
    """For the editor."""
    return {"styles": STYLES, "colors": COLORS, "bgs": BGS, "d": D_LIMITS, "poster_line": POSTER_LINE,
            "comment_line": COMMENT_LINE, "dim": DIM_ALPHA, "died_fade_ms": DIED_FADE_MS, "died_grow": DIED_GROW,
            "poster_step": POSTER_STEP, "fade_ms": FADE_MS, "pop_ms": POP_MS, "default_dy": DEFAULT_DY,
            "fonts": FONT_FILES}
