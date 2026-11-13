"""Claude composes thumbnails from what's there: frames of the clip, her cut-out, words,
arrows, rings, emoji and your stickers. It never makes a picture; it answers with lines
like the AI edit's, and place() turns them into layers (brand.layers) you can then move
and restyle in the thumbnail maker.

One call gives up to three versions that differ in idea, for YouTube's Test & Compare.
Places are words ("left top", "right"), sizes are shares of the picture, and things are
named (her region, "text", "cutout"), so the answer never needs pixel coordinates and a
bad line only drops that line."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from chatwatcher.brand import layers as L
from chatwatcher.brand.canvas import bg_crop
from chatwatcher.brand.cutout import grow

MAX_VERSIONS = 3
MAX_LINES = 10

SYSTEM = """You design YouTube thumbnails for a VTuber clip channel. You don't draw or generate \
pictures: you compose a thumbnail from real material, by writing lines that place things. \
Code draws them, and the editor can move and restyle every piece afterwards.

What makes people click a clip thumbnail:
- One focal point: her face, big, with a strong readable expression (shock, laughing, crying, \
rage, smug). Usually her cut-out (her model cut out of the frame, with a white sticker outline) \
at 70–115% of the picture's height on one side. Pick frames where her expression is strongest \
and her model is clearly visible in her region.
- Few words, big: 1–4 words that add tension or a reaction ("MY EARS", "SHE DID WHAT", \
"NOT AGAIN"), never the video title repeated, never a sentence. At most one *starred* word (it \
gets the accent colour). A version with no text at all is allowed when the face says it all.
- Contrast and simplicity: a blurred or dimmed background makes the cut-out pop; a zoomed frame \
works when the game moment itself is the story (then point at it with an arrow or a ring). \
No clutter: at most about 5 things on top of the background.
- Don't show her twice: a cut-out of her goes on a blurred, coloured or zoomed-away background, not over a frame where her model is already visible.
- Text never covers her face. Keep the bottom-right corner empty (YouTube puts the video length \
there). Keep things off the very edges.
- The channel's colours are for accents (a word, an outline, a ring), not a wash over the picture.
- The versions must be different ideas, not the same layout with other words, for example: \
(1) reaction: big cut-out + 2–3 words; (2) situation: the moment itself (zoomed frame) with her \
reacting smaller in a corner and an arrow or ring at what happened; (3) bold: an extreme close-up \
or a single colour background with her cut-out and one word, or no text.

Write each version as lines, one thing per line, fields split by " | ". The first line is the \
background; the rest are drawn in order (later lines on top). Frames are the numbers on the \
contact sheet. A region is a named box on the frame (listed in the prompt): whose face or model is where.

bg | frame | N | ZOOM | FOCUS        frame N filling the picture, ZOOM 1–3, centred on FOCUS (a region or "centre")
bg | blur | N                       frame N blurred and darkened: a clean backdrop for a cut-out
bg | brand                          the channel's gradient
bg | colour | C                     one colour
cutout | N | WHO | SIDE | HEIGHT | C   WHO's model cut out of frame N (WHO: a region), standing on the bottom edge \
at SIDE (left, right, centre), HEIGHT = share of the picture's height (0.4–1.2), C = outline colour; add "| flip" to mirror her
frame | N | WHAT | WHERE | WIDTH | SHAPE   a part of frame N (WHAT: a region, or "whole") as a picture-in-picture, \
WIDTH = share of the picture's width (0.15–0.7), SHAPE rect, round or circle
text | WORDS | WHERE | SIZE | C | STYLE | TILT   WORDS in capitals as they should read, SIZE = letter height as a \
share of the picture's height (0.08–0.3; 0.14+ reads on a phone), STYLE stroke (outlined), box (on a C box) or shadow, TILT degrees (−10–10)
arrow | FROM | TO | C               an arrow from FROM to TO: "text", "cutout", "frame", a region, or a WHERE
ring | TARGET | C                   a ring round TARGET (a region, "cutout", "frame", "text")
emoji | E | WHERE-OR-TARGET | SIZE  one emoji, SIZE = share of the picture's width (0.06–0.25)
burst | WHERE | SIZE | C            a comic burst shape (put text on it after)
sticker | NAME | WHERE | SIZE       a picture from the sticker library (listed in the prompt), or "logo"

WHERE is a place: left top, top, right top, left, centre, right, left bottom, bottom (not right bottom).
C is a colour: white, black, red, yellow, orange, green, blue, pink, cyan, the channel's deep, purple, \
lavender, light, accent, or #RRGGBB.

Write the thumbnail's words in {language}, matching the clip's audience. In "why", say in one \
sentence what the version is betting on. "note": anything the editor should know (a better frame \
not on the sheet, a region that seems wrong)."""


def schema() -> dict[str, Any]:
    return {"type": "object", "required": ["versions"], "properties": {
        "versions": {"type": "array", "minItems": 1, "maxItems": MAX_VERSIONS, "items": {
            "type": "object", "required": ["name", "why", "lines"], "properties": {
                "name": {"type": "string"}, "why": {"type": "string"},
                "lines": {"type": "array", "maxItems": MAX_LINES, "items": {"type": "string"}}}}},
        "note": {"type": "string"}}}


def _pct(a: float, b: float) -> str:
    return f"{round(a * 100)}–{round(b * 100)}%"


def build_prompt(*, shape: str, meta: dict[str, Any], analysis: dict[str, Any], frames: list[dict[str, Any]],
                 boxes: dict[str, dict[str, float]], stickers: list[str], brand: Any, words: list[str]) -> str:
    out = []
    if shape == "tall":
        out.append("Make the Short's cover: 1080×1920, upright. YouTube covers the bottom quarter with the title "
                   "and the right edge with buttons: keep words and faces out of them.")
    else:
        out.append("Make the video's thumbnail: 1280×720. It's seen small, on a phone, next to the title.")
    out.append(f"\nStream: {meta.get('title', '')}")
    a = analysis or {}
    for k, label in (("summary", "What happens"), ("context", "Context"), ("why_it_works", "Why it works"),
                     ("category", "Kind")):
        if a.get(k):
            out.append(f"{label}: {a[k]}")
    titles = [t.get("text", "") for t in a.get("titles") or [] if isinstance(t, dict)]
    if titles:
        out.append("Titles the video may get (don't repeat them on the thumbnail): " + " / ".join(titles[:3]))
    th = a.get("thumbnail") or {}
    if th.get("text") or th.get("notes"):
        out.append(f"An earlier idea: text \"{th.get('text', '')}\" — {th.get('notes', '')}")
    if words:
        out.append("Lines she says around the peak: " + " / ".join(words[:8]))
    out.append("\nFrames (the contact sheet): " + ", ".join(
        f"{i} at {int(f['clip_s'] // 60)}:{int(f['clip_s'] % 60):02d}" + (" (peak)" if f.get("is_peak") else "")
        for i, f in enumerate(frames, 1)))
    if boxes:
        out.append("Regions on the frame (x from the left, y from the top):")
        for n, b in boxes.items():
            out.append(f"- {n}: x {_pct(b['x'], b['x'] + b['w'])}, y {_pct(b['y'], b['y'] + b['h'])}")
    else:
        out.append("No regions are drawn for this channel: use \"cutout | N | auto | …\" (the whole frame is searched).")
    out.append(f"Channel colours: deep {brand.deep}, purple {brand.purple}, lavender {brand.lavender}, "
               f"light {brand.light}, accent {brand.accent}.")
    out.append("Sticker library: " + (", ".join(stickers) if stickers else "(empty)") + "; \"logo\" is the channel's logo.")
    out.append(f"\nGive {MAX_VERSIONS} versions.")
    return "\n".join(out)


# ── reading the answer ───────────────────────────────────────────────────────

@dataclass
class Board:
    """What placing needs to know about this clip and picture."""
    shape: str
    size: tuple[int, int]                                     # the picture's pixels
    frames: list[float]                                       # contact sheet number - 1 → clip time
    boxes: dict[str, dict[str, float]]                        # regions, fractions of the frame
    picture_size: tuple[int, int]                             # what the background crops (a Short: its layout)
    on_picture: Callable[[float, float], tuple[float, float] | None]   # frame point → picture point (fractions)
    measure: Callable[[dict[str, Any]], tuple[float, float]]  # a layer → its box (w, h), fractions of the picture
    stickers: list[str] = field(default_factory=list)

    @property
    def tall(self) -> bool:
        return self.shape == "tall"


@dataclass
class Idea:
    name: str
    why: str
    layers: list[dict[str, Any]]
    warnings: list[str]
    lines: list[str]


def parts(line: str) -> list[str]:
    return [p.strip() for p in str(line).split("|")]


def _num(s: Any, default: float, lo: float, hi: float) -> float:
    try:
        v = float(str(s).strip().rstrip("%x×"))
        if not math.isfinite(v):
            raise ValueError
    except (TypeError, ValueError):
        return default
    return min(max(v, lo), hi)


def place_of(s: str) -> tuple[float, float] | None:
    """'left top' → (0, 0), 'right' → (1, 0.5), 'centre' → (0.5, 0.5); None if it isn't a place."""
    w = set(re.findall(r"[a-z]+", s.lower()))
    if not w or not w <= {"left", "right", "top", "bottom", "centre", "center", "middle", "upper", "lower"}:
        return None
    ax = 0.0 if "left" in w else 1.0 if "right" in w else 0.5
    ay = 0.0 if w & {"top", "upper"} else 1.0 if w & {"bottom", "lower"} else 0.5
    return ax, ay


def _colour(s: Any, default: str) -> str:
    s = str(s or "").strip()
    if L.is_colour(s.upper() if s.startswith("#") else s.lower()):
        return s.upper() if s.startswith("#") else s.lower()
    return default


class _Composer:
    def __init__(self, b: Board):
        self.b = b
        W, H = b.size
        self.W, self.H = W, H
        self.mx = 0.035 if not b.tall else 0.06
        self.my = 0.05 if not b.tall else 0.06
        self.right = 1 - self.mx - (0.08 if b.tall else 0)            # a Short's buttons
        self.bottom = 0.74 if b.tall else 1 - self.my                  # a Short's title
        self.layers: list[dict[str, Any]] = []
        self.bg: dict[str, Any] | None = None
        self.warnings: list[str] = []

    # where things are
    def t_of(self, n: Any) -> float | None:
        try:
            i = int(float(str(n).strip().lstrip("#")))
        except ValueError:
            return None
        return self.b.frames[i - 1] if 1 <= i <= len(self.b.frames) else None

    def region(self, name: str) -> dict[str, float] | None:
        n = name.strip().lower()
        return self.b.boxes.get(n)

    def on_canvas(self, fx: float, fy: float) -> tuple[float, float] | None:
        """A point of the frame → where it shows on the thumbnail (under the background), or None."""
        bg = self.bg
        if bg is None or bg["source"] not in ("frame", "blur"):
            return None
        p = self.b.on_picture(fx, fy)
        if p is None:
            return None
        pw, ph = self.b.picture_size
        x0, y0, x1, y1 = bg_crop(pw, ph, bg, self.W / self.H)
        x, y = (p[0] * pw - x0) / max(1, x1 - x0), (p[1] * ph - y0) / max(1, y1 - y0)
        return (x, y) if 0 <= x <= 1 and 0 <= y <= 1 else None

    def box_of(self, ly: dict[str, Any]) -> tuple[float, float, float, float]:
        w, h = self.b.measure(ly)
        return ly["x"], ly["y"], w, h

    def target(self, s: str) -> tuple[float, float, float, float] | None:
        """(x, y, w, h) on the thumbnail of a thing named in a line, as fractions."""
        name = s.strip().lower()
        kinds = {"text": "text", "cutout": "cutout", "frame": "frame", "her": "cutout", "face": None}
        if name in kinds and kinds[name]:
            for ly in reversed(self.layers):
                if ly["kind"] == kinds[name] or (kinds[name] == "cutout" and ly.get("_her")):
                    x, y, w, h = self.box_of(ly)
                    if ly["kind"] == "cutout":        # her head: the top of the cut-out
                        return x, y - h * 0.28, w * 0.6, h * 0.36
                    return x, y, w, h
            return None
        for ly in reversed(self.layers):              # her cut-out, named by her region
            if ly.get("_who") == name and ly["kind"] == "frame":
                return self.box_of(ly)
            if ly["kind"] == "cutout" and ly.get("_who") == name:
                x, y, w, h = self.box_of(ly)
                return x, y - h * 0.28, w * 0.6, h * 0.36
        r = self.region(name)
        if r:
            a = self.on_canvas(r["x"], r["y"])
            c = self.on_canvas(r["x"] + r["w"], r["y"] + r["h"])
            if a and c:
                return (a[0] + c[0]) / 2, (a[1] + c[1]) / 2, abs(c[0] - a[0]), abs(c[1] - a[1])
        p = place_of(s)
        if p:
            return self.at(p, 0.1, 0.1)[0], self.at(p, 0.1, 0.1)[1], 0.1, 0.1
        return None

    def at(self, p: tuple[float, float], w: float, h: float) -> tuple[float, float]:
        """The centre of a w×h thing at place p, inside the margins (and out of the corner
        YouTube's timestamp covers)."""
        ax, ay = p
        x = self.mx + w / 2 if ax == 0 else self.right - w / 2 if ax == 1 else 0.5
        y = self.my + h / 2 if ay == 0 else self.bottom - h / 2 if ay == 1 else 0.5
        if not self.b.tall and ax == 1 and ay == 1:
            y -= 0.12
        return round(x, 4), round(y, 4)

    # the lines
    def line(self, raw: str) -> None:
        p = parts(raw)
        kind = p[0].lower()
        fn = getattr(self, "_" + kind, None) if kind in ("bg", "cutout", "frame", "text", "arrow", "ring", "emoji",
                                                        "burst", "sticker") else None
        if fn is None:
            self.warnings.append(f"Skipped “{raw[:60]}”: unknown kind.")
            return
        try:
            out = fn(p[1:] + [""] * 8)
        except Exception as e:  # a line we can't place: drop it, keep the rest
            self.warnings.append(f"Skipped “{raw[:60]}”: {e}")
            return
        if isinstance(out, str):
            self.warnings.append(f"Skipped “{raw[:60]}”: {out}")

    def _bg(self, a: list[str]) -> str | None:
        src = a[0].lower()
        if src in ("frame", "blur"):
            t = self.t_of(a[1])
            if t is None:
                return "no such frame"
            ly = L.make("bg", source=src, t=t, dim=0.32 if src == "blur" else 0.0)
            if src == "frame":
                ly["zoom"] = _num(a[2], 1.0, 1.0, 3.0)
                r = self.region(a[3]) if a[3] else None
                if r:
                    pt = self.b.on_picture(r["x"] + r["w"] / 2, r["y"] + r["h"] / 2)
                    if pt:
                        ly["cx"], ly["cy"] = round(pt[0], 4), round(pt[1], 4)
            else:
                ly["zoom"] = 1.15
        elif src == "brand":
            ly = L.make("bg", source="brand")
        elif src in ("colour", "color"):
            ly = L.make("bg", source="colour", colour=_colour(a[1], "deep"))
        else:
            return "unknown background"
        self.bg = ly
        return None

    def _crop_of(self, who: str) -> tuple[tuple[float, float, float, float], str]:
        r = self.region(who)
        if r is None and who.lower() in ("auto", "her", "face", ""):
            r = self.region("face") or (next(iter(self.b.boxes.values())) if self.b.boxes else None)
        if r is None:
            return (0.0, 0.0, 1.0, 1.0), who.lower()
        return grow(r), who.lower()

    def _cutout(self, a: list[str]) -> str | None:
        t = self.t_of(a[0])
        if t is None:
            return "no such frame"
        (sx, sy, sw, sh), who = self._crop_of(a[1])
        side = (place_of(a[2]) or (1.0, 1.0))[0]
        h = _num(a[3], 0.9, 0.35, 1.25) if not self.b.tall else _num(a[3], 0.5, 0.25, 0.7)
        ly = L.make("cutout", t=t, sx=sx, sy=sy, sw=sw, sh=sh, outline_colour=_colour(a[4], "white"),
                    flip=any(x.lower() == "flip" for x in a[4:]))
        ly["_who"] = who
        try:
            aw, ah = self.b.measure(ly | {"w": 0.3})   # her shape: width for a 0.3-wide cut-out
        except Exception as e:                       # no model, or nobody found: her region in a frame
            self.warnings.append(f"Her cut-out didn't work ({e}); used a framed picture of her instead.")
            r = self.region(who) or self.region("face")
            fr = L.make("frame", t=t, shape="round", border_colour=ly["outline_colour"],
                        **({"sx": r["x"], "sy": r["y"], "sw": r["w"], "sh": r["h"]} if r else {}))
            fr["w"] = 0.3
            _, fh = self.b.measure(fr)
            fr["w"] = round(min(0.7, h * 0.85 * 0.3 / max(fh, 1e-3)), 4)
            fw, fh = self.b.measure(fr)
            fr["x"], fr["y"] = self.at((side, 1.0), fw, fh)
            fr["_her"], fr["_who"] = True, who
            self.layers.append(fr)
            return None
        ly["w"] = round(min(1.2, h * 0.3 / max(ah, 1e-3)), 4)
        w, h = ly["w"], ah * ly["w"] / 0.3
        ly["x"] = round(w / 2 if side == 0 else 1 - w / 2 if side == 1 else 0.5, 4)
        bottom = 1.02 if not self.b.tall else self.bottom + 0.02
        ly["y"] = round(bottom - h / 2, 4)
        self.layers.append(ly)
        return None

    def _frame(self, a: list[str]) -> str | None:
        t = self.t_of(a[0])
        if t is None:
            return "no such frame"
        r = self.region(a[1])
        if r:
            m = 0.12
            sx, sy = max(0.0, r["x"] - r["w"] * m), max(0.0, r["y"] - r["h"] * m)
            box = (sx, sy, min(1 - sx, r["w"] * (1 + 2 * m)), min(1 - sy, r["h"] * (1 + 2 * m)))
        else:
            box = (0.0, 0.0, 1.0, 1.0)
        shape = a[4].lower() if a[4].lower() in L.FRAME_SHAPES else "round"
        ly = L.make("frame", t=t, sx=box[0], sy=box[1], sw=box[2], sh=box[3], shape=shape,
                    w=_num(a[3], 0.38, 0.15, 0.7))
        w, h = self.b.measure(ly)
        ly["x"], ly["y"] = self.at(place_of(a[2]) or (1.0, 0.0), w, h)
        self.layers.append(ly)
        return None

    def _text(self, a: list[str]) -> str | None:
        words = a[0].replace("\\n", "\n").strip()
        if re.fullmatch(r"\*[^*]+\*", words):       # all of it starred: it's all in its colour
            words = words[1:-1]
        if not words:
            return "no words"
        p = place_of(a[1]) or (0.0, 0.0)
        tall = self.b.tall
        size = _num(a[2], 0.16, 0.06, 0.3) if not tall else _num(a[2], 0.07, 0.03, 0.14)
        style = a[4].lower() if a[4].lower() in L.TEXT_STYLES else "stroke"
        c = _colour(a[3], "white")
        if style == "box":
            ly = L.make("text", style="box", box_colour=c, colour="black" if c in ("yellow", "white", "light", "accent") else "white",
                        accent="red" if c in ("yellow", "white", "light", "accent") else "yellow")
        else:
            ly = L.make("text", style=style, colour=c, accent="accent" if c == "white" else "white",
                        outline_colour="black")
        ly |= {"text": words[:L.MAX_TEXT], "size": size, "rot": _num(a[5], 0.0, -12.0, 12.0),
               "align": "left" if p[0] == 0 else "right" if p[0] == 1 else "center",
               "font": "title"}
        ly["w"] = (0.86 if p[0] == 0.5 else 0.5) if not tall else 0.8
        w, h = self.b.measure(ly)
        ly["x"], ly["y"] = self.at(p, ly["w"], h)
        self.layers.append(ly)
        return None

    def _arrow(self, a: list[str]) -> str | None:
        f, t = self.target(a[0]), self.target(a[1])
        if not f or not t:
            return "can't find what it points from or to"
        W, H = self.W, self.H
        (fx, fy, fw, fh), (tx, ty, tw, th) = f, t
        dx, dy = (tx - fx) * W, (ty - fy) * H
        dist = math.hypot(dx, dy)
        if dist < 1:
            return "it points at itself"
        ux, uy = dx / dist, dy / dist

        def edge(w: float, h: float) -> float:       # centre → the box's edge, along the arrow
            return min(w * W / 2 / max(abs(ux), 1e-6), h * H / 2 / max(abs(uy), 1e-6))
        gap_to = edge(tw, th) * 0.8 + 0.015 * W
        gap_from = edge(fw, fh) + 0.045 * W           # clear of the text's outline and tilt
        ex, ey = tx * W - ux * gap_to, ty * H - uy * gap_to
        length = min(max(dist - gap_to - gap_from, 0.12 * W), 0.32 * W)
        sx, sy = ex - ux * length, ey - uy * length
        ly = L.make("sticker", shape="arrow", colour=_colour(a[2], "red"), outline_colour="white",
                    x=round((sx + ex) / 2 / W, 4), y=round((sy + ey) / 2 / H, 4), w=round(length / W, 4),
                    rot=round(math.degrees(math.atan2(uy, ux)), 1), edge=0.03)
        self.layers.append(ly)
        return None

    def _ring(self, a: list[str]) -> str | None:
        t = self.target(a[0])
        if not t:
            return "can't find what to ring"
        x, y, w, h = t
        pw, ph = max(w * self.W, 0.06 * self.W) * 1.35, max(h * self.H, 0.06 * self.W) * 1.35
        self.layers.append(L.make("sticker", shape="ring", colour=_colour(a[1], "red"), outline_colour="white",
                                  x=round(x, 4), y=round(y, 4), w=round(pw / self.W, 4),
                                  aspect=round(min(max(ph / pw, 0.3), 3.0), 3), edge=0.012))
        return None

    def _emoji(self, a: list[str]) -> str | None:
        e = a[0].strip()[:8]
        if not e:
            return "no emoji"
        w = _num(a[2], 0.12, 0.05, 0.3)
        ly = L.make("sticker", shape="emoji", emoji=e, w=w, outline_colour="white", edge=0.0)
        p = place_of(a[1])
        t = None if p else self.target(a[1])
        _, h = self.b.measure(ly)
        if t:
            x, y, tw, th = t
            ly["x"], ly["y"] = round(min(self.right - w / 2, x + tw * 0.45 + w * 0.3), 4), round(max(self.my + h / 2, y - th * 0.45), 4)
        else:
            ly["x"], ly["y"] = self.at(p or (1.0, 0.0), w, h)
        ly["rot"] = 8.0
        self.layers.append(ly)
        return None

    def _burst(self, a: list[str]) -> str | None:
        w = _num(a[1], 0.3, 0.1, 0.6)
        ly = L.make("sticker", shape="burst", colour=_colour(a[2], "yellow"), outline_colour="black", w=w,
                    aspect=0.75, edge=0.02)
        ly["x"], ly["y"] = self.at(place_of(a[0]) or (0.0, 0.0), w, w * 0.75 * self.W / self.H)
        self.layers.append(ly)
        return None

    def _sticker(self, a: list[str]) -> str | None:
        name = a[0].strip()
        w = _num(a[2], 0.15, 0.05, 0.5)
        if name.lower() == "logo":
            ly = L.make("sticker", shape="logo", w=w, edge=0.0)
        else:
            found = next((s for s in self.b.stickers if s.lower() in (name.lower(), name.lower() + ".png")), None)
            if not found:
                return "no such sticker"
            ly = L.make("sticker", shape="image", image=found, w=w, outline_colour="white", edge=0.03)
        _, h = self.b.measure(ly)
        ly["x"], ly["y"] = self.at(place_of(a[1]) or (0.0, 0.0), w, h)
        self.layers.append(ly)
        return None

    def done(self, duration: float) -> list[dict[str, Any]]:
        if self.bg is None:
            self.warnings.append("No background line: used the first frame, blurred.")
            self.bg = L.make("bg", source="blur", t=self.b.frames[0] if self.b.frames else 0.0, dim=0.32)
        return L.clean([self.bg] + self.layers, duration)


def compose(version: dict[str, Any], board: Board, duration: float) -> Idea:
    """One version of Claude's answer → its layers (lines that can't be placed are skipped,
    with a warning)."""
    c = _Composer(board)
    lines = [str(x) for x in (version.get("lines") or [])][:MAX_LINES]
    for raw in lines:
        if raw.strip():
            c.line(raw)
    return Idea(str(version.get("name") or "")[:60], str(version.get("why") or "")[:300], c.done(duration),
                c.warnings, lines)


def read(answer: dict[str, Any], board: Board, duration: float) -> list[Idea]:
    vs = [v for v in (answer.get("versions") or []) if isinstance(v, dict)][:MAX_VERSIONS]
    return [compose(v, board, duration) for v in vs]
