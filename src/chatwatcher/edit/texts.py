"""Text callouts: big words over the picture for a moment ("NO NO NO" over the Yes button,
"← that was the backup", "×3"), placed by you or the AI edit. They say what the viewer
would otherwise miss; the captions say what she says.

A callout on an edit is an effect {"type": "text", "t", "d", "text", "style", "color", "at",
"dx", "dy", "size"}: style is one of STYLES (pop: springs in; stamp: slams in, tilted, in
capitals; label: small, on a black box, fades in), color a COLORS name, at / dx / dy place
its centre like an overlay's (edit.overlays.place), size its font size as a share of the
picture's width. *Stars* around words show them in the highlight colour, like captions.

They're drawn by libass, in the same .ass file as the captions (subs.lines.ass), so they
sit above the zooms, shakes and overlays, and don't move with the camera. editor.js draws
the same thing for its preview (drawTexts): change both together."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from chatwatcher.edit import layout as lay
from chatwatcher.edit import overlays as ovl
from chatwatcher.subs import lines

TEXT = "text"
STYLES = ("pop", "stamp", "label")
COLORS = {"white": "FFFFFF", "yellow": "FFD600", "red": "FF3B30", "green": "3DDC84", "blue": "4DA3FF",
          "pink": "FF5FA2"}
DEFAULT_COLOR = {"pop": "white", "stamp": "red", "label": "white"}
MAX_CHARS = 40
LINE, LABEL_LINE = 14, 24       # wider than this (CJK counts 2) wraps into two lines (labels are smaller)
D_LIMITS = (1.5, 0.3, 8.0)      # default, shortest and longest, seconds
SIZE_RANGE = (0.03, 0.3)        # font size, as a share of the picture's width
STAMP_TILT = 7                  # degrees, anticlockwise
INTRO_MS, OUTRO_MS = 170, 140


def default_size(style: str, short: bool) -> float:
    """Big on a Short (the picture is narrow), smaller across a 16:9 picture; labels smaller."""
    base = 0.11 if short else 0.06
    return round(base * (0.55 if style == "label" else 1.0), 3)


_EMOJI = re.compile("[\U00010000-\U0010FFFF️‍]")   # libass draws no colour emoji: they'd be boxes


def clean_text(text: str) -> str:
    return " ".join(_EMOJI.sub("", str(text or "")).split())[:MAX_CHARS]


def shown_text(text: str, style: str) -> str:
    return text.upper() if style == "stamp" else text


def line_width(style: str) -> int:
    return LABEL_LINE if style == "label" else LINE


def wrapped(text: str, style: str) -> list[str]:
    """Its lines: editor.js wraps the same way (wrapSub with line_width, 2)."""
    return lines.wrap(shown_text(text, style), line_width(style), 2)


# ── on an edit ───────────────────────────────────────────────────────────────

@dataclass
class Placed:
    t: float                    # clip time
    d: float
    x: float                    # its centre, as shares of the output picture
    y: float
    text: str
    style: str
    color: str                  # RRGGBB
    size: float                 # font size, as a share of the picture's width


def placed(effects: list[dict[str, Any]], boxes: dict[str, dict[str, float]],
           panels: list[lay.Panel] | None) -> list[Placed]:
    """The callouts of these effects (clip time), placed on the output picture. panels: a
    Short's picture (None for 16:9)."""
    out = []
    for fx in effects:
        if fx.get("type") != TEXT or not (fx.get("text") or "").strip():
            continue
        style = fx.get("style") if fx.get("style") in STYLES else "pop"
        x, y = ovl.place(fx.get("at") or "middle", boxes, panels, float(fx.get("dx") or 0), float(fx.get("dy") or 0))
        out.append(Placed(fx["t"], fx["d"], x, y, fx["text"], style,
                          COLORS.get(fx.get("color") or "", COLORS[DEFAULT_COLOR[style]]),
                          float(fx.get("size") or default_size(style, panels is not None))))
    return out


def local(items: list[Placed], start: float, length: float) -> list[tuple[Placed, float, float, bool, bool]]:
    """(callout, when it starts in this part, how long it shows, whether it begins here,
    whether it ends here) for each one that shows in [start, start + length): a callout
    split by a cut only springs in once and fades out once."""
    out = []
    for p in items:
        a, b = max(p.t, start), min(p.t + p.d, start + length)
        if b - a > 0.04:
            out.append((p, a - start, b - a, a - p.t < 0.02, p.t + p.d - b < 0.02))
    return out


def events(items: list[tuple[Placed, float, float, bool, bool]], w: int, h: int) -> list[str]:
    """ASS Dialogue lines (styles Callout and Label in subs.lines.ass) for these callouts."""
    out = []
    for p, a, d, begins, ends in items:
        size = max(8, round(p.size * w))
        tags = [f"\\an5\\pos({round(p.x * w)},{round(p.y * h)})\\fs{size}\\q2"]
        if p.style == "label":
            tags.append(f"\\bord{max(2, round(size * 0.28))}\\shad0")
        else:
            tags.append(f"\\bord{max(2.0, size * (0.14 if p.style == 'stamp' else 0.11)):.1f}"
                        f"\\shad{max(0.0, size * 0.05):.1f}")
        if p.style == "stamp":
            tags.append(f"\\frz{STAMP_TILT}")
        if begins:
            if p.style == "pop":
                tags.append(f"\\fscx40\\fscy40\\t(0,{INTRO_MS // 2},\\fscx118\\fscy118)"
                            f"\\t({INTRO_MS // 2},{INTRO_MS},\\fscx100\\fscy100)")
            elif p.style == "stamp":
                tags.append(f"\\fscx260\\fscy260\\alpha&HFF&\\t(0,{INTRO_MS * 2 // 3},\\fscx100\\fscy100\\alpha&H00&)")
        fade_in = INTRO_MS if begins and p.style == "label" else 0
        fade_out = OUTRO_MS if ends else 0
        if fade_in or fade_out:
            tags.append(f"\\fad({fade_in},{min(fade_out, round(d * 400))})")
        body = lines._ass_text(shown_text(p.text, p.style), line_width(p.style), 2, p.color)
        style = "Label" if p.style == "label" else "Callout"
        out.append(f"Dialogue: 2,{lines._ass_time(a)},{lines._ass_time(a + d)},{style},,0,0,0,,"
                   f"{{{''.join(tags)}}}{body}")
    return out
