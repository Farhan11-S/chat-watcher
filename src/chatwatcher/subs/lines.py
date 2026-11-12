"""Subtitle lines ("cues") for an edit, built from Whisper's word timings.

A cue is {"id", "t0", "t1", "text", "by": "auto" | "you"} in clip seconds (stream time in
the database, like effects, so it stays on the same words when you trim or reorder).
Text may put *stars* around words to show them in the highlight colour. A style's
"emphasis" (words the AI edit picked) highlights them in cues built from the words, so
they stay highlighted when the clip is transcribed again.

Long text wraps into two balanced lines. wrap() here (the render) and wrap() in editor.js
(the preview) are the same rule, so change them together."""

from __future__ import annotations

import bisect
import math
import re
from typing import Any

MAX_LINE = 38          # width (CJK characters count 2) before a cue wraps into two lines
MAX_CUE = 80           # a new cue starts before one gets wider than this
MAX_CUE_S = 6.0
MIN_CUE_S = 0.8
GAP_S = 0.6            # a pause this long between words starts a new cue
REPLY_S = 0.6          # …and so does another voice talking this long in between
HOLD_S = 0.25          # a cue stays up this long after its last word
MIN_SHOWN_S = 0.25     # a cue cut down to less than this by a cut isn't shown
MAX_CUES = 500
MAX_TEXT = 200
SIZES = {"small": 0.055, "normal": 0.065, "large": 0.08}    # font size, as a fraction of the frame height
POSITIONS = ("bottom", "top")
HIGHLIGHT = "FFD600"   # *highlighted* words (RGB)
# A Short (taller than wide): text sized from the width, narrower lines (up to 4), shorter
# cues, and room at the bottom for YouTube's buttons and title.
SHORT_LINE = 22
SHORT_LINES = 4
SHORT_CUE = 44
WORD_CUE = 14         # "words" mode: one to three words at a time, like clip channels
MODES = ("lines", "words")
LOOKS = ("classic", "bubble")   # bubble: round font, the voice's colour, white outline (edit.captions)
MAX_EMPHASIS = 12
TITLE_SIZE = 0.075     # the Short's title, as a fraction of the width
TITLE_LINE = 20
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


def said(data: dict[str, Any]) -> list[tuple[float, float, str, int | None]]:
    """words_of with each word's voice (subs.speakers), None when unknown."""
    out = []
    for line in data.get("lines") or []:
        for w in line.get("words") or []:
            if str(w[2]).strip():
                v = w[4] if len(w) > 4 and isinstance(w[4], int) and not isinstance(w[4], bool) else None
                out.append((float(w[0]), float(w[1]), str(w[2]), v))
    return out


def _groups(words: list[tuple[float, float, str]], max_cue: int,
            others: list[tuple[float, float]]) -> list[list[tuple[float, float, str]]]:
    """Words → cues: a new one at a pause, when another voice answers in between (others:
    their words, by start), after a sentence, or before it gets too long (max_cue wide) or
    too slow to read (then preferably after a comma)."""
    starts = [a for a, _ in others]
    groups: list[list[tuple[float, float, str]]] = []
    cur: list[tuple[float, float, str]] = []
    text = lambda ws: "".join(w[2] for w in ws).strip()        # noqa: E731

    def answered(t0: float, t1: float) -> bool:
        busy = 0.0
        for a, b in others[max(0, bisect.bisect_left(starts, t0) - 1): bisect.bisect_right(starts, t1)]:
            busy += max(0.0, min(b, t1) - max(a, t0))
        return busy >= REPLY_S

    for w in words:
        if cur:
            gap = w[0] - cur[-1][1]
            if gap >= GAP_S or w[1] - cur[0][0] > MAX_CUE_S or (others and answered(cur[-1][1], w[0])):
                groups.append(cur)
                cur = []
            elif width(text(cur + [w])) > max_cue:
                cut = next((j for j in range(len(cur) - 1, len(cur) // 2 - 1, -1)
                            if _BREAK_AFTER.search(cur[j][2].strip())), len(cur) - 1)
                groups.append(cur[:cut + 1])
                cur = cur[cut + 1:]
            elif _SENTENCE_END.search(cur[-1][2].strip()) and (width(text(cur)) >= 12 or gap > 0.25
                                                                or max_cue <= WORD_CUE):
                groups.append(cur)                              # a line's "Okay." alone waits for company
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def build(data: dict[str, Any], max_cue: int = MAX_CUE) -> list[dict[str, Any]]:
    """Cues from Whisper's words (see _groups). max_cue=WORD_CUE gives word-by-word
    captions. With two or more voices each voice gets its own cues, marked "speaker", so
    when two talk at once their cues overlap (the render stacks them)."""
    ws = said(data)
    voices = {w[3] for w in ws if w[3] is not None}
    if len(voices) < 2:
        groups = [(g, None) for g in _groups([w[:3] for w in ws], max_cue, [])]
    else:
        groups = []
        for v in {w[3] for w in ws}:
            others = sorted((w[0], w[1]) for w in ws if w[3] != v)
            groups += [(g, v) for g in _groups([w[:3] for w in ws if w[3] == v], max_cue, others)]
        groups.sort(key=lambda gv: (gv[0][0][0], gv[0][-1][1]))
    text = lambda ws: "".join(w[2] for w in ws).strip()        # noqa: E731
    cues = []
    for k, (g, v) in enumerate(groups):
        start, last = g[0][0], g[-1][1]
        nxt = next((h[0][0] for h, u in groups[k + 1:] if u == v), math.inf)    # that voice's next cue
        end = max(min(max(last + HOLD_S, start + MIN_CUE_S), nxt - 0.04), last)
        cue = {"id": f"a{k + 1}", "t0": round(start, 3), "t1": round(end, 3), "text": text(g), "by": "auto"}
        if v is not None:
            cue["speaker"] = v
        cues.append(cue)
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
        cue = {"id": cid, "t0": round(t0, 3), "t1": round(t1, 3), "text": text,
               "by": "auto" if c.get("by") == "auto" else ("claude" if c.get("by") == "claude" else "you")}
        sp = c.get("speaker")
        if isinstance(sp, int) and not isinstance(sp, bool) and 0 <= sp < 8:
            cue["speaker"] = sp
        out.append(cue)
    return sorted(out, key=lambda c: c["t0"])


def clean_style(raw: Any, default: dict[str, Any]) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    return {"on": bool(raw.get("on", default["on"])),
            "size": raw.get("size") if raw.get("size") in SIZES else default["size"],
            "position": raw.get("position") if raw.get("position") in POSITIONS else default["position"],
            "mode": raw.get("mode") if raw.get("mode") in MODES else default.get("mode", "lines"),
            "look": raw.get("look") if raw.get("look") in LOOKS else default.get("look", "classic"),
            "emphasis": emphasis_terms(raw.get("emphasis"))}


def emphasis_terms(raw: Any) -> list[str]:
    terms = raw if isinstance(raw, list) else []
    out = []
    for t in terms:
        t = " ".join(str(t).replace("*", "").split())[:40]
        if t and t.lower() not in {x.lower() for x in out}:
            out.append(t)
    return out[:MAX_EMPHASIS]


def emphasise(cues: list[dict[str, Any]], terms: list[str]) -> list[dict[str, Any]]:
    """The cues with each term (a word or short phrase, any case) put in *stars*."""
    terms = sorted(emphasis_terms(terms), key=len, reverse=True)
    if not terms:
        return cues
    out = []
    for c in cues:
        text = c["text"]
        for term in terms:
            text = re.sub(rf"(?<![\w*])({re.escape(term)})(?![\w*])", r"*\1*", text, flags=re.I)
        out.append(c | {"text": text})
    return out


# ── layout: the same in the preview (editor.js) ──────────────────────────────

def frame_rules(width_px: int, height_px: int) -> dict[str, Any]:
    """How subtitles sit in a frame of this shape (the editor's preview gets the same numbers).
    base: the side font sizes are a fraction of; bottom / top / top_title: the margin as a
    fraction of the height (top_title: below a Short's title)."""
    if height_px > width_px:
        return {"vertical": True, "max_line": SHORT_LINE, "max_lines": SHORT_LINES, "base": "width",
                "bottom": 0.25, "top": 0.08, "top_title": 0.2, "title_size": TITLE_SIZE, "title_line": TITLE_LINE}
    return {"vertical": False, "max_line": MAX_LINE, "max_lines": 2, "base": "height",
            "bottom": 0.06, "top": 0.06, "top_title": 0.06, "title_size": TITLE_SIZE, "title_line": TITLE_LINE}


def wrap(text: str, max_line: int = MAX_LINE, max_lines: int = 2) -> list[str]:
    """One line, or two of about equal width split at the space nearest the middle (any
    character for text without spaces, like Japanese). With max_lines > 2, halves still
    wider than max_line are split again."""
    if width(text) <= max_line or max_lines < 2:
        return [text]
    half = width(text) / 2
    spaces = [i for i, ch in enumerate(text) if ch == " "]
    cuts = spaces or list(range(1, len(text)))
    best = min(cuts, key=lambda i: (abs(width(text[:i]) - half), i))
    a, b = text[:best].rstrip(), text[best:].lstrip()
    if not (a and b):
        return [text]
    if max_lines == 2:
        return [a, b]
    return wrap(a, max_line, max_lines // 2) + wrap(b, max_line, max_lines - max_lines // 2)


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

Shown = tuple[float, float, str, "dict[str, str] | None"]     # start, end, text, voice {"color", "name"}


def local(cues: list[dict[str, Any]], start: float, length: float) -> list[Shown]:
    """The cues that show in [start, start + length) of the clip, in that span's own time,
    with their voice's colour and name (subs.speakers.paint) when they have one."""
    out = []
    for c in cues:
        a, b = max(c["t0"], start), min(c["t1"], start + length)
        if b - a >= min(MIN_SHOWN_S, c["t1"] - c["t0"]) and b > a:
            who = {"color": c["color"], "name": c.get("name") or ""} if c.get("color") else None
            out.append((a - start, b - start, c["text"], who))
    return out


def on_edit(cues: list[dict[str, Any]], segments: list[list[float]], seg_lengths: list[float]) -> list[Shown]:
    """Every cue where it shows in the edit (once per segment it shows in)."""
    out, offset = [], 0.0
    for (s, *_), length in zip(segments, seg_lengths):
        out += [(offset + a, offset + b, t, who) for a, b, t, who in local(cues, s, length)]
        offset += length
    return sorted(out, key=lambda x: (x[0], x[1]))


# ── files ────────────────────────────────────────────────────────────────────

def _ass_time(t: float) -> str:
    cs = max(0, round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _who(rest: list[Any]) -> dict[str, str]:
    return (rest[0] if rest and isinstance(rest[0], dict) else None) or {}


def _ass_color(rgb: str) -> str:
    return f"{{\\c&H{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}&}}"


def _ass_text(text: str, max_line: int = MAX_LINE, max_lines: int = 2, color: str = "FFFFFF") -> str:
    """color: the text's colour (a voice's); *highlighted* words stay HIGHLIGHT, or white
    when the voice's colour is that yellow already."""
    esc = lambda s: s.replace("\\", "＼").replace("{", "｛").replace("}", "｝")     # noqa: E731
    color = color if re.fullmatch(r"[0-9A-Fa-f]{6}", color or "") else "FFFFFF"
    hi = _ass_color(HIGHLIGHT if color.upper() != HIGHLIGHT else "FFFFFF")
    base = _ass_color(color.upper())
    out, on = [], False
    for line in wrap(text, max_line, max_lines):
        pieces, on_after = runs(line, on)
        out.append("".join((hi if h else base) + esc(p) for p, h in pieces))
        on = on_after
    return "\\N".join(out)


def ass(cues: list[Shown], style: dict[str, Any], width_px: int, height_px: int,
        font: str = FONT, title: str = "", title_until: float = 0.0, extra: list[str] | None = None) -> str:
    """An ASS subtitle file for libass, sized for this video: bold white (or the voice's
    colour) with a thick black outline, bottom or top centre. Cues that overlap (two voices
    at once) are stacked by libass, the later one further from the edge. title: a Short's
    title in the top band, shown from 0 to title_until. extra: more Dialogue lines, the
    text callouts (edit.texts.events), with the Callout and Label styles."""
    w, h = width_px or 1280, height_px or 720
    rules = frame_rules(w, h)
    base = w if rules["vertical"] else h
    size = round(SIZES.get(style.get("size"), SIZES["normal"]) * base)
    outline, shadow, side = max(1.0, size * 0.1), max(0.0, size * 0.045), round(base * 0.06)
    top = style.get("position") == "top"
    align = 8 if top else 2
    margin = round(h * (rules["top_title" if title else "top"] if top else rules["bottom"]))
    tsize = round(rules["title_size"] * w)
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {w}\nPlayResY: {h}\nWrapStyle: 2\n"
            "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
            "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Clip,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,"
            f"{outline:.1f},{shadow:.1f},{align},{side},{side},{margin},1\n"
            f"Style: Title,{font},{tsize},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,"
            f"{max(1.0, tsize * 0.12):.1f},{max(0.0, tsize * 0.05):.1f},8,{side},{side},{round(h * 0.06)},1\n"
            f"Style: Callout,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,"
            f"{outline:.1f},{shadow:.1f},5,0,0,0,1\n"
            f"Style: Label,{font},{size},&H00FFFFFF,&H000000FF,&H26000000,&H26000000,-1,0,0,0,100,100,0,0,3,"
            f"{outline:.1f},0,5,0,0,0,1\n\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    body = ""
    if title and title_until > 0:
        body += f"Dialogue: 1,{_ass_time(0)},{_ass_time(title_until)},Title,,0,0,0,,{_ass_text(title, TITLE_LINE, 3)}\n"
    body += "".join(f"Dialogue: 0,{_ass_time(a)},{_ass_time(b)},Clip,,0,0,0,,"
                    f"{_ass_text(t, rules['max_line'], rules['max_lines'], _who(rest).get('color') or 'FFFFFF')}\n"
                    for a, b, t, *rest in cues)
    body += "".join(line + "\n" for line in extra or [])
    return head + body


def srt(cues: list[Shown]) -> str:
    """An .srt (for YouTube); with two or more named people, each line starts "Name: "."""
    def ts(t: float) -> str:
        ms = max(0, round(t * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    people = {(_who(rest).get("name") or "").lower() for *_, rest in ((c[:3], c[3:]) for c in cues)} - {""}
    out = []
    for k, (a, b, t, *rest) in enumerate(cues, 1):
        name = (_who(rest).get("name") or "") if len(people) >= 2 else ""
        out.append(f"{k}\n{ts(a)} --> {ts(b)}\n" + "\n".join(plain(x) for x in wrap(f"{name}: {t}" if name else t))
                   + "\n\n")
    return "".join(out)
