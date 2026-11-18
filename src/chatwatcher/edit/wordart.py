"""Word art: one word (or a short phrase) of a caption in its own look, the way clip
channels dress the key word: "BLOOD" white on a red box, "STICKY" in dripping green slime,
"pretty" glowing pink. It springs bigger the moment she says it.

An effect {"type": "word", "t", "d", "word", "style", "color"}: t is when she says the word
(clip time; the caption showing then is the one it styles, at the first place the word is
in it), d only how long it shows on the timeline. When the caption doesn't have the word
(retyped, or the effect moved), nothing is drawn.

    style  box: white (or black) on a solid box · slime: dripping green paint · glow: soft
           neon halo · fire: hot yellow with an orange edge (tuff) · scary: horror red ·
           big: much bigger, in colour
    color  a COLORS key: the box's, the glow's, the slime's… ("" = the style's own)

Drawn by edit.captions in the same ASS events as the caption (a box is libass's opaque
box, BorderStyle 3, from the Box style of subs.lines.ass); captions.js draws the same in
the editor (artPieces in util.js splits the text like pieces() here)."""

from __future__ import annotations

import colorsys
import math
import re
from dataclasses import dataclass
from typing import Any

from chatwatcher.subs import lines

WORD = "word"
COLORS = {"red": "E0261E", "orange": "FF7A00", "yellow": "FFD600", "green": "3BD14A", "blue": "2F8BFF",
          "purple": "9B4DFF", "pink": "FF5FB8", "white": "FFFFFF", "black": "151515"}
# font: a captions.FONTS key ("" = the caption's own); scale: times the caption's size.
STYLES = {
    "box":   {"color": "red", "upper": True, "scale": 1.12, "font": ""},
    "slime": {"color": "green", "upper": True, "scale": 1.2, "font": "angry"},
    "glow":  {"color": "pink", "upper": False, "scale": 1.12, "font": ""},
    "fire":  {"color": "orange", "upper": True, "scale": 1.35, "font": "loud"},
    "scary": {"color": "red", "upper": True, "scale": 1.2, "font": "scary"},
    "big":   {"color": "yellow", "upper": True, "scale": 1.5, "font": ""},
}
D_LIMITS = (1.0, 0.2, 10.0)       # the timeline block: default, shortest, longest (seconds)
MAX_CHARS = 30
MAX_WORDS = 3                     # a phrase of at most this many words
EARLY_S = 0.05                    # a word said this much before its caption shows still lands on it
PUNCH = (1.3, 90, 260)            # when she says it: this much bigger, up in ms, back by ms
BOX_PAD = 0.07                    # the box round the word (and a space each side), share of the font size
RING = 0.06                       # the bubble look's dark frame round a box
GLOW = (0.16, 0.14)               # a glow's width and blur, shares of the font size
FIRE_TEXT = "FFE14D"


def clean(fx: dict[str, Any], i: int) -> dict[str, Any]:
    """The word-art fields of effect i (from the browser or Claude). Raises ValueError."""
    word = " ".join(re.sub("[\U00010000-\U0010FFFF️‍*{}\\\\]", "", str(fx.get("word") or "")).split()[:MAX_WORDS])
    word = word[:MAX_CHARS].strip()
    if not word:
        raise ValueError(f"Effect {i} needs the word it styles.")
    try:
        d = float(fx.get("d", D_LIMITS[0]))
    except (TypeError, ValueError):
        raise ValueError(f"Effect {i} has a bad length.")
    if not math.isfinite(d):
        raise ValueError(f"Effect {i} has a bad length.")
    style = fx.get("style") if fx.get("style") in STYLES else "box"
    return {"word": word, "style": style, "color": fx.get("color") if fx.get("color") in COLORS else "",
            "d": round(min(max(d, D_LIMITS[1]), D_LIMITS[2]), 3)}


# ── on an edit ───────────────────────────────────────────────────────────────

@dataclass
class Art:
    t: float                    # clip time: when she says it
    word: str
    style: str
    color: str                  # RRGGBB
    upper: bool
    scale: float
    font: str                   # a captions.FONTS key, or "" for the caption's own


def placed(effects: list[dict[str, Any]]) -> list[Art]:
    out = []
    for fx in effects:
        if fx.get("type") != WORD or not fx.get("word"):
            continue
        st = fx.get("style") if fx.get("style") in STYLES else "box"
        base = STYLES[st]
        out.append(Art(float(fx["t"]), str(fx["word"]), st, COLORS.get(fx.get("color") or "", COLORS[base["color"]]),
                       base["upper"], base["scale"], base["font"]))
    return sorted(out, key=lambda a: a.t)


def find(text: str, word: str) -> tuple[int, int] | None:
    """Where the word first is in the caption's text, counted in its letters (spaces and
    *stars* not counted, so it holds however the text is wrapped); None if it isn't."""
    plain = lines.plain(text)
    m = (re.search(rf"(?<!\w){re.escape(word)}(?!\w)", plain, re.I)
         or re.search(re.escape(word).replace(r"\ ", r"\s*"), plain, re.I))
    if not m:
        return None
    g0 = len(plain[:m.start()].replace(" ", ""))
    return g0, g0 + len(m.group(0).replace(" ", ""))


def on_cue(text: str, t0: float, t1: float, arts: list[Art]) -> list[tuple[int, int, Art]]:
    """The word art said while this caption shows, where each word is in it."""
    out = []
    for a in arts:
        if t0 - EARLY_S <= a.t < t1 and (at := find(text, a.word)):
            if not any(g0 < at[1] and at[0] < g1 for g0, g1, _ in out):
                out.append((*at, a))
    return out


def pieces(rows: list[str], spans: list[tuple[int, int, Art]]) -> list[list[tuple[str, bool, Art | None]]]:
    """Each wrapped row as (text, highlighted, word art) pieces: lines.runs, split where a
    styled word starts and ends (a phrase's spaces are part of it when on one row)."""
    flat: list[list[Any]] = []                  # [row, char, highlighted, art]
    on = False
    for r, row in enumerate(rows):
        ps, on = lines.runs(row, on)
        flat += [[r, ch, hi, None] for text, hi in ps for ch in text]
    g = 0
    for c in flat:
        if c[1] != " ":
            c[3] = next((a for g0, g1, a in spans if g0 <= g < g1), None)
            g += 1
    for i, c in enumerate(flat):
        if c[1] == " ":
            prev = next((x for x in reversed(flat[:i]) if x[1] != " "), None)
            nxt = next((x for x in flat[i + 1:] if x[1] != " "), None)
            if prev and nxt and prev[3] is not None and prev[3] is nxt[3] and prev[0] == nxt[0] == c[0]:
                c[3] = prev[3]
    out: list[list[tuple[str, bool, Art | None]]] = [[] for _ in rows]
    for r, ch, hi, art in flat:
        ch = ch.upper() if art and art.upper else ch
        row = out[r]
        if row and row[-1][1] == hi and row[-1][2] is art:
            row[-1] = (row[-1][0] + ch, hi, art)
        else:
            row.append((ch, hi, art))
    return out


# ── drawing (ASS tags) ───────────────────────────────────────────────────────

def _c(rgb: str) -> str:
    return f"&H{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}&"


def shade(rgb: str, k: float) -> str:
    """The colour at k times its lightness."""
    h, li, s = colorsys.rgb_to_hls(*(int(rgb[i:i + 2], 16) / 255 for i in (0, 2, 4)))
    return "%02X%02X%02X" % tuple(round(v * 255) for v in colorsys.hls_to_rgb(h, max(0.0, min(1.0, li * k)), s))


def light(rgb: str) -> bool:
    r, g, b = (int(rgb[i:i + 2], 16) for i in (0, 2, 4))
    return 0.299 * r + 0.587 * g + 0.114 * b > 170


def look(a: Art, role: str, size: float, bord: float) -> dict[str, Any]:
    """How the word draws on one of the caption's layers. role: ring (the bubble look's
    dark layer underneath), top (the bubble look's text layer) or solo (the classic look's
    one layer). bord: the layer's own outline. Returns {"box", "fill", "edge", "bord",
    "blur"} (colours RRGGBB, sizes in px)."""
    col, under = a.color, role == "ring"
    edge_w = bord if under else max(bord, size * (0.1 if role == "solo" else 0.06))
    if a.style == "box":
        frame = shade(col, 0.35)
        return {"box": True, "fill": frame if under else ("151515" if light(col) else "FFFFFF"),
                "edge": frame if under else col, "bord": size * (BOX_PAD + (RING if under else 0)), "blur": 0.0}
    if a.style == "glow":
        if under:
            return {"box": False, "fill": col, "edge": col, "bord": size * GLOW[0], "blur": size * GLOW[1]}
        return {"box": False, "fill": "FFFFFF", "edge": col, "bord": size * 0.05 if role == "top" else size * GLOW[0],
                "blur": 0.0 if role == "top" else size * GLOW[1]}
    if a.style == "fire":
        if under:
            return {"box": False, "fill": shade(col, 0.5), "edge": shade(col, 0.5), "bord": bord, "blur": size * 0.06}
        return {"box": False, "fill": FIRE_TEXT, "edge": col, "bord": edge_w, "blur": 0.0}
    if a.style == "slime":
        dark = shade(col, 0.3)
        return {"box": False, "fill": dark if under else col, "edge": dark, "bord": edge_w, "blur": 0.0}
    if a.style == "scary":
        return {"box": False, "fill": "000000" if under else col, "edge": "000000", "bord": edge_w, "blur": 0.0}
    # big: the colour, with the layer's own edge
    return {"box": False, "fill": shade(col, 0.35) if under else col, "edge": None, "bord": bord, "blur": 0.0}


def tags(a: Art, role: str, size: float, bord: float, family: str, scale_tags: str, punch_ms: int | None) -> str:
    """The override tags that start the word (after them, the caption's own come back with
    \\r). family: the font to draw it in; scale_tags: its \\fscx/\\fscy (the caption's
    animation, at the word's scale); punch_ms: when she says it, from the event's start."""
    lk = look(a, role, size, bord)
    out = ("\\rBox" if lk["box"] else "") + f"\\fn{family}\\fs{round(size)}\\b1\\1c{_c(lk['fill'])}"
    if lk["edge"]:
        out += f"\\3c{_c(lk['edge'])}"
    out += f"\\bord{lk['bord']:.1f}\\shad0\\blur{lk['blur']:.1f}" + scale_tags
    if punch_ms is not None and punch_ms >= 0:
        k, up, back = PUNCH
        s = round(a.scale * 100)
        out += (f"\\t({punch_ms},{punch_ms + up},\\fscx{round(s * k)}\\fscy{round(s * k)})"
                f"\\t({punch_ms + up},{punch_ms + back},\\fscx{s}\\fscy{s})")
    return out


def info() -> dict[str, Any]:
    return {"styles": STYLES, "colors": COLORS, "d": D_LIMITS, "max_chars": MAX_CHARS, "max_words": MAX_WORDS,
            "early": EARLY_S, "punch": PUNCH, "box_pad": BOX_PAD, "ring": RING, "glow": GLOW, "fire_text": FIRE_TEXT}
