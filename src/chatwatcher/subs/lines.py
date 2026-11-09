"""Subtitle lines ("cues") for an edit, built from Whisper's word timings.

A cue is {"id", "t0", "t1", "text", "by": "auto" | "you"} in clip seconds (stream time in
the database, like effects, so it stays on the same words when you trim or reorder).
Text may put *stars* around words to show them in the highlight colour.

Long text wraps into two balanced lines. wrap() here (the render) and wrap() in editor.js
(the preview) are the same rule, so change them together."""

from __future__ import annotations

import math
import re
from typing import Any

MAX_LINE = 38          # width (CJK characters count 2) before a cue wraps into two lines
MAX_CUE = 80           # a new cue starts before one gets wider than this
MAX_CUE_S = 6.0
MIN_CUE_S = 0.8
GAP_S = 0.6            # a pause this long between words starts a new cue
HOLD_S = 0.25          # a cue stays up this long after its last word
MIN_SHOWN_S = 0.25     # a cue cut down to less than this by a cut isn't shown
MAX_CUES = 500
MAX_TEXT = 200
SIZES = {"small": 0.055, "normal": 0.065, "large": 0.08}    # font size, as a fraction of the frame height
POSITIONS = ("bottom", "top")
HIGHLIGHT = "FFD600"   # *highlighted* words (RGB)
FONT = "Noto Sans CJK JP"

_WIDE = re.compile("[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦]")
_SENTENCE_END = re.compile(r"[.?!。？！]$")
_BREAK_AFTER = re.compile(r"[,、;:，]$")


def width(text: str) -> int:
    t = text.replace("*", "")
    return len(t) + len(_WIDE.findall(t))


# ── building cues from words ─────────────────────────────────────────────────

def words_of(data: dict[str, Any]) -> list[tuple[float, float, str]]:
    """Every word in words.json, in order: (start, end, text with its leading space)."""
    out = []
    for line in data.get("lines") or []:
        for w in line.get("words") or []:
            if str(w[2]).strip():
                out.append((float(w[0]), float(w[1]), str(w[2])))
    return out


def build(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Cues from Whisper's words: a new one at a pause, after a sentence, or before it gets
    too long or too slow to read (then preferably after a comma)."""
    groups: list[list[tuple[float, float, str]]] = []
    cur: list[tuple[float, float, str]] = []
    text = lambda ws: "".join(w[2] for w in ws).strip()        # noqa: E731
    for w in words_of(data):
        if cur:
            gap = w[0] - cur[-1][1]
            if gap >= GAP_S or w[1] - cur[0][0] > MAX_CUE_S:
                groups.append(cur)
                cur = []
            elif width(text(cur + [w])) > MAX_CUE:
                cut = next((j for j in range(len(cur) - 1, len(cur) // 2 - 1, -1)
                            if _BREAK_AFTER.search(cur[j][2].strip())), len(cur) - 1)
                groups.append(cur[:cut + 1])
                cur = cur[cut + 1:]
            elif _SENTENCE_END.search(cur[-1][2].strip()) and (width(text(cur)) >= 12 or gap > 0.25):
                groups.append(cur)                              # "Okay." alone waits for company
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    cues = []
    for k, g in enumerate(groups):
        start, last = g[0][0], g[-1][1]
        nxt = groups[k + 1][0][0] if k + 1 < len(groups) else math.inf
        end = max(min(max(last + HOLD_S, start + MIN_CUE_S), nxt - 0.04), last)
        cues.append({"id": f"a{k + 1}", "t0": round(start, 3), "t1": round(end, 3), "text": text(g), "by": "auto"})
    return cues


# ── checking what the browser sends ──────────────────────────────────────────

class SubsError(ValueError):
    pass


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean(raw: Any, duration: float) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise SubsError("Subtitles must be a list.")
    if len(raw) > MAX_CUES:
        raise SubsError(f"At most {MAX_CUES} subtitle lines.")
    out, ids = [], set()
    for i, c in enumerate(raw, 1):
        try:
            t0, t1 = float(c["t0"]), float(c["t1"])
        except (KeyError, TypeError, ValueError):
            raise SubsError(f"Subtitle {i} needs a start and an end.")
        if not (math.isfinite(t0) and math.isfinite(t1)):
            raise SubsError(f"Subtitle {i} has a bad time.")
        t0, t1 = max(0.0, t0), min(t1, duration)
        text = " ".join(_CONTROL.sub(" ", str(c.get("text") or "")).replace("\n", " ").split())[:MAX_TEXT]
        if t1 - t0 < 0.1 or not text:
            continue                                     # emptied or squashed: dropped
        cid = re.sub(r"[^A-Za-z0-9_-]", "", str(c.get("id") or ""))[:24] or f"s{i}"
        while cid in ids:
            cid += "x"
        ids.add(cid)
        out.append({"id": cid, "t0": round(t0, 3), "t1": round(t1, 3), "text": text,
                    "by": "auto" if c.get("by") == "auto" else ("claude" if c.get("by") == "claude" else "you")})
    return sorted(out, key=lambda c: c["t0"])


def clean_style(raw: Any, default: dict[str, Any]) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    return {"on": bool(raw.get("on", default["on"])),
            "size": raw.get("size") if raw.get("size") in SIZES else default["size"],
            "position": raw.get("position") if raw.get("position") in POSITIONS else default["position"]}


# ── layout: the same in the preview (editor.js) ──────────────────────────────

def wrap(text: str) -> list[str]:
    """One line, or two of about equal width split at the space nearest the middle (any
    character for text without spaces, like Japanese)."""
    if width(text) <= MAX_LINE:
        return [text]
    half = width(text) / 2
    spaces = [i for i, ch in enumerate(text) if ch == " "]
    cuts = spaces or list(range(1, len(text)))
    best = min(cuts, key=lambda i: (abs(width(text[:i]) - half), i))
    a, b = text[:best].rstrip(), text[best:].lstrip()
    return [a, b] if a and b else [text]


def runs(line: str, on: bool = False) -> tuple[list[tuple[str, bool]], bool]:
    """A line as (text, highlighted) pieces; `on` carries a *highlight* over a line break."""
    out = []
    for i, part in enumerate(line.split("*")):
        if i:
            on = not on
        if part:
            out.append((part, on))
    return out, on


def plain(text: str) -> str:
    return text.replace("*", "")


# ── timelines ────────────────────────────────────────────────────────────────

def local(cues: list[dict[str, Any]], start: float, length: float) -> list[tuple[float, float, str]]:
    """The cues that show in [start, start + length) of the clip, in that span's own time."""
    out = []
    for c in cues:
        a, b = max(c["t0"], start), min(c["t1"], start + length)
        if b - a >= min(MIN_SHOWN_S, c["t1"] - c["t0"]) and b > a:
            out.append((a - start, b - start, c["text"]))
    return out


def on_edit(cues: list[dict[str, Any]], segments: list[list[float]], seg_lengths: list[float]) -> list[tuple[float, float, str]]:
    """Every cue where it shows in the edit (once per segment it shows in)."""
    out, offset = [], 0.0
    for (s, *_), length in zip(segments, seg_lengths):
        out += [(offset + a, offset + b, t) for a, b, t in local(cues, s, length)]
        offset += length
    return sorted(out)


# ── files ────────────────────────────────────────────────────────────────────

def _ass_time(t: float) -> str:
    cs = max(0, round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _ass_text(text: str) -> str:
    esc = lambda s: s.replace("\\", "＼").replace("{", "｛").replace("}", "｝")     # noqa: E731
    hi = f"{{\\c&H{HIGHLIGHT[4:6]}{HIGHLIGHT[2:4]}{HIGHLIGHT[0:2]}&}}"
    out, on = [], False
    for line in wrap(text):
        pieces, on_after = runs(line, on)
        out.append("".join((hi if h else "{\\c&HFFFFFF&}") + esc(p) for p, h in pieces))
        on = on_after
    return "\\N".join(out)


def ass(cues: list[tuple[float, float, str]], style: dict[str, Any], width_px: int, height_px: int,
        font: str = FONT) -> str:
    """An ASS subtitle file for libass, sized for this video: bold white with a thick black
    outline, bottom or top centre."""
    h = height_px or 720
    size = round(SIZES.get(style.get("size"), SIZES["normal"]) * h)
    outline, shadow, margin = max(1.0, size * 0.1), max(0.0, size * 0.045), round(h * 0.06)
    align = 8 if style.get("position") == "top" else 2
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {width_px or 1280}\nPlayResY: {h}\nWrapStyle: 2\n"
            "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
            "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Clip,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,"
            f"{outline:.1f},{shadow:.1f},{align},{margin},{margin},{margin},1\n\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    return head + "".join(f"Dialogue: 0,{_ass_time(a)},{_ass_time(b)},Clip,,0,0,0,,{_ass_text(t)}\n"
                          for a, b, t in cues)


def srt(cues: list[tuple[float, float, str]]) -> str:
    def ts(t: float) -> str:
        ms = max(0, round(t * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    return "".join(f"{k}\n{ts(a)} --> {ts(b)}\n" + "\n".join(plain(x) for x in wrap(t)) + "\n\n"
                   for k, (a, b, t) in enumerate(cues, 1))
