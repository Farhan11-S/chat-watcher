"""A YouTube thumbnail (1280×720 JPEG) from one frame of the clip: zoomed on her face if
you like, big outlined text on the other side (*stars* make a word the accent colour),
a brand band or shade behind the text, and the channel badge in the corner.

The Short's cover (1080×1920, draw_tall) is the same idea upright: the frame in the
Short's layout (crop / stack / fit, see edit.layout), zoomed where you click, with the
text across the top, middle or lower middle, clear of the buttons YouTube draws over a
Short (bottom quarter and the right edge)."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from chatwatcher.brand import art
from chatwatcher.brand.brand import Brand

W, H = 1280, 720
STYLES = ("band", "shade", "clean")
SIDES = ("auto", "left", "right")
ZOOM_RANGE = (1.0, 3.0)
MAX_TEXT = 120
THUMB_FILE = "thumbnail.jpg"
DEFAULTS: dict[str, Any] = {"t": 0.0, "text": "", "zoom": 1.0, "cx": 0.5, "cy": 0.45, "side": "auto",
                            "style": "band", "logo": True, "tilt": True}
SHAPES = ("wide", "tall")
TW, TH = 1080, 1920
PLACES = ("top", "middle", "bottom")
COVER_FILE = "short_cover.jpg"
TALL_DEFAULTS: dict[str, Any] = {"t": 0.0, "text": "", "zoom": 1.0, "cx": 0.5, "cy": 0.5, "place": "top",
                                 "style": "band", "logo": True, "tilt": True}
# Where the text goes on the cover (y from, y to, as fractions of the height); "bottom"
# stops above the quarter YouTube covers with the title and channel name.
PLACE_BOX = {"top": (0.11, 0.36), "middle": (0.36, 0.62), "bottom": (0.52, 0.74)}
SIDE_MARGIN = 80               # px; also keeps clear of the like / share buttons on the right


class ThumbError(Exception):
    pass


def clean(raw: Any, duration: float, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Settings from the browser, on top of base (the defaults). Raises ThumbError."""
    s = dict(base or DEFAULTS)
    if not isinstance(raw, dict):
        raise ThumbError("Thumbnail settings must be an object.")
    try:
        for k in ("t", "zoom", "cx", "cy"):
            if k in raw and raw[k] is not None:
                v = float(raw[k])
                if not math.isfinite(v):
                    raise ValueError
                s[k] = v
    except (TypeError, ValueError):
        raise ThumbError("A thumbnail setting has a bad number.")
    if "text" in raw:
        s["text"] = re.sub(r"[ \t]+", " ", str(raw["text"] or "")).strip()[:MAX_TEXT]
    for k, options in (("side", SIDES), ("style", STYLES), ("place", PLACES)):
        if raw.get(k) in options:
            s[k] = raw[k]
    for k in ("logo", "tilt"):
        if k in raw:
            s[k] = bool(raw[k])
    s["t"] = round(min(max(s["t"], 0.0), max(0.0, duration - 0.05)), 3)
    s["zoom"] = round(min(max(s["zoom"], ZOOM_RANGE[0]), ZOOM_RANGE[1]), 2)
    s["cx"], s["cy"] = round(min(max(s["cx"], 0.0), 1.0), 4), round(min(max(s["cy"], 0.0), 1.0), 4)
    return s


# ── text ─────────────────────────────────────────────────────────────────────

_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]")
_JOINERS = re.compile("[\uFE0F\u200D]")


def _tokens(text: str) -> list[tuple[str, bool]]:
    """'a *big* deal' → [('a ', False), ('big ', True), ('deal', False)]; words keep their
    trailing space. Text without spaces (Japanese) breaks between characters."""
    out, on = [], False
    text = _JOINERS.sub("", text)
    spaced = " " in text.strip()
    for part in re.split(r"(\*)", text):
        if part == "*":
            on = not on
            continue
        units = re.findall(r"\S+\s*|\s+", part) if spaced else list(part)
        out += [(u, on) for u in units if u]
    return out


class Ink:
    """Measures and draws text in the brand font, with colour emoji from the system's
    emoji font (the brand font usually has none)."""

    def __init__(self, fnt_path: str | None, size: int):
        from chatwatcher.brand.brand import _fc_match
        self.fnt, self.size = art.font(fnt_path, size), size
        self.emoji_path = _fc_match("Noto Color Emoji")

    def _runs(self, text: str) -> list[tuple[str, bool]]:
        out: list[tuple[str, bool]] = []
        for ch in text:
            e = bool(self.emoji_path and _EMOJI.match(ch))
            if out and out[-1][1] == e and not e:
                out[-1] = (out[-1][0] + ch, e)
            else:
                out.append((ch, e))
        return out

    def length(self, text: str) -> float:
        return sum(self.size * 1.15 if e else self.fnt.getlength(t) for t, e in self._runs(text))

    def draw(self, layer: Image.Image, shadow: Image.Image, x: float, y: float, text: str, fill, deep,
             stroke: int) -> float:
        d, sd = ImageDraw.Draw(layer), ImageDraw.Draw(shadow)
        off = self.size * 0.07
        for t, e in self._runs(text):
            if e:
                from PIL import ImageFont
                big = Image.new("RGBA", (150, 150), (0, 0, 0, 0))
                ImageDraw.Draw(big).text((0, 0), t, font=ImageFont.truetype(self.emoji_path, 109), embedded_color=True)
                box = big.getbbox()
                if box:
                    big = big.crop(box)
                    s = self.size * 0.95 / max(big.size)
                    big = big.resize((max(1, int(big.width * s)), max(1, int(big.height * s))), Image.LANCZOS)
                    layer.alpha_composite(big, (int(x + self.size * 0.08), int(y - big.height / 2)))
                x += self.size * 1.15
                continue
            sd.text((x + off, y + off), t, font=self.fnt, fill=deep + (200,), anchor="lm",
                    stroke_width=stroke, stroke_fill=deep + (200,))
            d.text((x, y), t, font=self.fnt, fill=fill + (255,), anchor="lm", stroke_width=stroke,
                   stroke_fill=deep + (255,))
            x += self.fnt.getlength(t)
        return x


def _wrap(ink: Ink, paragraphs: list[str], width: float) -> list[list[tuple[str, bool]]]:
    lines: list[list[tuple[str, bool]]] = []
    for para in paragraphs:
        cur: list[tuple[str, bool]] = []
        for tok in _tokens(para):
            trial = "".join(p for p, _ in cur) + tok[0]
            if cur and ink.length(trial.rstrip()) > width:
                lines.append(cur)
                cur = [(tok[0].lstrip(), tok[1])]
            else:
                cur.append(tok)
        lines.append(cur)
    return lines


def _widest(ink: Ink, lines: list[list[tuple[str, bool]]]) -> float:
    return max((ink.length("".join(p for p, _ in ln).rstrip()) for ln in lines), default=0)


def layout(text: str, fnt_path: str | None, width: float, height: float, biggest: int = 170,
           smallest: int = 44, max_lines: int = 3):
    """The biggest size where the text fits width×height in at most max_lines lines, with
    the lines as even as they can be. Returns (ink, lines), a line being [(piece, highlighted)]."""
    paragraphs = [p for p in text.split("\n") if p.strip()] or [""]
    size = biggest
    while True:
        ink = Ink(fnt_path, size)
        lines = _wrap(ink, paragraphs, width)
        if size <= smallest or (len(lines) <= max_lines and size * 1.08 * len(lines) <= height
                                and _widest(ink, lines) <= width):
            break
        size -= 4
    lo, hi = width * 0.35, width            # narrowest width that keeps the same number of lines
    for _ in range(12):
        mid = (lo + hi) / 2
        if len(_wrap(ink, paragraphs, mid)) == len(lines):
            hi = mid
        else:
            lo = mid
    return ink, _wrap(ink, paragraphs, hi)


def _text_layer(text: str, b: Brand, fnt_path: str | None, box: tuple[float, float, float, float],
                align: str, tilt: bool, canvas: tuple[int, int] = (W, H), biggest: int = 170,
                max_lines: int = 3) -> Image.Image:
    x0, y0, x1, y1 = box
    ink, lines = layout(text, fnt_path, x1 - x0, y1 - y0, biggest=biggest, max_lines=max_lines)
    size = ink.size
    layer = Image.new("RGBA", canvas, (0, 0, 0, 0))
    shadow = Image.new("RGBA", canvas, (0, 0, 0, 0))
    stroke = max(3, int(size * 0.11))
    line_h = size * 1.08
    top = y0 + (y1 - y0 - line_h * len(lines)) / 2 + line_h / 2
    white, accent, deep = (255, 255, 255), b.rgb("accent"), b.rgb("deep")
    for i, ln in enumerate(lines):
        pieces = [(p.rstrip() if j == len(ln) - 1 else p, hi) for j, (p, hi) in enumerate(ln)]
        total = sum(ink.length(p) for p, _ in pieces)
        x = x0 if align == "left" else (x0 + x1 - total) / 2 if align == "center" else x1 - total
        for p, hi in pieces:
            x = ink.draw(layer, shadow, x, top + i * line_h, p, accent if hi else white, deep, stroke)
    shadow = shadow.filter(ImageFilter.GaussianBlur(max(1, size * 0.04)))
    shadow.alpha_composite(layer)
    if tilt:
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        shadow = shadow.rotate({"left": 4, "center": 3}.get(align, -4), resample=Image.BICUBIC, center=(cx, cy))
    return shadow


# ── the picture ──────────────────────────────────────────────────────────────

def crop_box(fw: int, fh: int, s: dict[str, Any]) -> tuple[int, int, int, int]:
    """The part of the frame shown: 16:9, 1/zoom of it, centred on (cx, cy) if it can be."""
    cw = fw / s["zoom"]
    ch = cw * 9 / 16
    if ch > fh / s["zoom"]:
        ch = fh / s["zoom"]
        cw = ch * 16 / 9
    x = min(max(s["cx"] * fw - cw / 2, 0), fw - cw)
    y = min(max(s["cy"] * fh - ch / 2, 0), fh - ch)
    return int(x), int(y), int(x + cw), int(y + ch)


def text_side(fw: int, fh: int, s: dict[str, Any]) -> str:
    if s["side"] in ("left", "right"):
        return s["side"]
    x0, _y0, x1, _y1 = crop_box(fw, fh, s)
    focus = (s["cx"] * fw - x0) / max(1, x1 - x0)
    return "left" if focus >= 0.5 else "right"


def draw(frame: Image.Image, s: dict[str, Any], b: Brand, logo_file: Path | None, title_font: str | None,
         text_font: str | None) -> Image.Image:
    fw, fh = frame.size
    img = frame.convert("RGB").crop(crop_box(fw, fh, s)).resize((W, H), Image.LANCZOS)
    img = ImageEnhance.Color(img).enhance(1.18)
    img = ImageEnhance.Contrast(img).enhance(1.06)
    img = ImageEnhance.Sharpness(img).enhance(1.3).convert("RGBA")
    side = text_side(fw, fh, s)
    left = side == "left"
    text = s.get("text") or ""
    if s["style"] == "band" and text:
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        a, c = W * 0.58, W * 0.46                     # top and bottom x of the band's inner edge

        def mirror(pts):
            return pts if left else [(W - x, y) for x, y in pts]
        d.polygon(mirror([(0, 0), (a, 0), (c, H), (0, H)]), fill=b.rgb("deep") + (150,))
        d.polygon(mirror([(0, 0), (a - 26, 0), (c - 26, H), (0, H)]), fill=b.rgb("purple") + (120,))
        d.polygon(mirror([(a, 0), (a + 16, 0), (c + 16, H), (c, H)]), fill=b.rgb("lavender") + (255,))
        d.polygon(mirror([(a + 16, 0), (a + 24, 0), (c + 24, H), (c + 16, H)]), fill=(255, 255, 255, 255))
        img.alpha_composite(layer)
    elif s["style"] == "shade" and text:
        import numpy as np
        ramp = np.clip(1 - np.arange(W) / (W * 0.62), 0, 1) ** 1.4 * 215
        if not left:
            ramp = ramp[::-1]
        alpha = Image.fromarray(np.tile(ramp.astype("uint8"), (H, 1)), "L")
        shade = Image.new("RGBA", (W, H), b.rgb("deep") + (255,))
        shade.putalpha(alpha)
        img.alpha_composite(shade)
    if text:
        margin = 54
        box = (margin, 150, W * 0.5, H - 60) if left else (W * 0.5, 150, W - margin, H - 60)
        img.alpha_composite(_text_layer(text, b, text_font or title_font, box, side, s.get("tilt", True)))
    if s.get("logo", True):
        badge = _badge(b, logo_file, title_font)
        x = 26 if left else W - badge.width - 26
        img.alpha_composite(badge, (int(x), 22))
    return img.convert("RGB")


# ── the Short's cover ────────────────────────────────────────────────────────

def compose(frame: Image.Image, panels: list, size: tuple[int, int] = (TW, TH)) -> Image.Image:
    """The frame in a Short's layout: each panel (edit.layout.Panel) cut out and scaled to
    its place; the fit layout's background blurred small and darkened, like the render."""
    from chatwatcher.edit import layout as lay
    fw, fh = frame.size
    ow, oh = size
    out = Image.new("RGB", size, (0, 0, 0))
    src = frame.convert("RGB")
    for p in panels:
        sx, sy, sw, sh = p.src
        dx, dy, dw, dh = p.dst
        x0, y0, x1, y1 = round(dx * ow), round(dy * oh), round((dx + dw) * ow), round((dy + dh) * oh)
        part = src.crop((round(sx * fw), round(sy * fh), round((sx + sw) * fw), round((sy + sh) * fh)))
        if p.blur:
            small = part.resize((max(2, (x1 - x0) // lay.BLUR_DOWN), max(2, (y1 - y0) // lay.BLUR_DOWN)), Image.BILINEAR)
            small = small.filter(ImageFilter.BoxBlur(4)).filter(ImageFilter.BoxBlur(4))
            part = ImageEnhance.Brightness(small.resize((x1 - x0, y1 - y0), Image.BICUBIC)).enhance(lay.BG_DARK)
        else:
            part = part.resize((x1 - x0, y1 - y0), Image.LANCZOS)
        out.paste(part, (x0, y0))
    return out


def tall_box(s: dict[str, Any]) -> tuple[int, int, int, int]:
    """The part of the composed cover shown: 1/zoom of it, centred on (cx, cy) if it can be."""
    cw, ch = TW / s["zoom"], TH / s["zoom"]
    x = min(max(s["cx"] * TW - cw / 2, 0), TW - cw)
    y = min(max(s["cy"] * TH - ch / 2, 0), TH - ch)
    return int(x), int(y), int(x + cw), int(y + ch)


def draw_tall(picture: Image.Image, s: dict[str, Any], b: Brand, logo_file: Path | None, title_font: str | None,
              text_font: str | None) -> Image.Image:
    """The Short's cover from the composed 1080×1920 picture (compose())."""
    img = picture.convert("RGB").crop(tall_box(s)).resize((TW, TH), Image.LANCZOS)
    img = ImageEnhance.Color(img).enhance(1.18)
    img = ImageEnhance.Contrast(img).enhance(1.06)
    img = ImageEnhance.Sharpness(img).enhance(1.3).convert("RGBA")
    text = s.get("text") or ""
    f0, f1 = PLACE_BOX.get(s.get("place", "top"), PLACE_BOX["top"])
    y0, y1 = TH * f0, TH * f1
    if text and s["style"] == "band":           # a slanted brand band across the cover, behind the text
        layer = Image.new("RGBA", (TW, TH), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        top, bot, slope = y0 - 30, y1 + 30, 70
        d.polygon([(0, top + slope), (TW, top), (TW, bot - slope), (0, bot)], fill=b.rgb("deep") + (155,))
        d.polygon([(0, top + slope + 22), (TW, top + 22), (TW, bot - slope - 22), (0, bot - 22)],
                  fill=b.rgb("purple") + (110,))
        d.polygon([(0, bot), (TW, bot - slope), (TW, bot - slope + 14), (0, bot + 14)], fill=b.rgb("lavender") + (255,))
        d.polygon([(0, bot + 14), (TW, bot - slope + 14), (TW, bot - slope + 21), (0, bot + 21)],
                  fill=(255, 255, 255, 255))
        img.alpha_composite(layer)
    elif text and s["style"] == "shade":        # darker towards the text's middle
        import numpy as np
        mid, reach = (y0 + y1) / 2, (y1 - y0) / 2 + TH * 0.14
        ramp = np.clip(1 - np.abs(np.arange(TH) - mid) / reach, 0, 1) ** 1.2 * 200
        shade = Image.new("RGBA", (TW, TH), b.rgb("deep") + (255,))
        shade.putalpha(Image.fromarray(np.tile(ramp.astype("uint8")[:, None], (1, TW)), "L"))
        img.alpha_composite(shade)
    if text:
        box = (SIDE_MARGIN, y0, TW - SIDE_MARGIN, y1)
        img.alpha_composite(_text_layer(text, b, text_font or title_font, box, "center", s.get("tilt", True),
                                        canvas=(TW, TH), biggest=200, max_lines=4))
    if s.get("logo", True):
        badge = _badge(b, logo_file, title_font)
        img.alpha_composite(badge, (36, 96))      # top left: the band starts lower on that side
    return img.convert("RGB")


def _badge(b: Brand, logo_file: Path | None, title_font: str | None) -> Image.Image:
    """The round logo and the channel name on a pill (like the intro's corner badge)."""
    size = 92
    fnt = art.font(title_font, 34)
    name = b.name.upper()
    tw = int(fnt.getlength(name))
    w, h = size + tw + 46, size + 12
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((size * 0.55, h / 2 - 28, w - 4, h / 2 + 28), radius=28, fill=b.rgb("deep") + (205,))
    lg = art.logo(b, logo_file, size, title_font, shadow=False)
    img.alpha_composite(lg, (0, (h - size) // 2))
    art.outlined_text(img, (size + 14, h / 2), name, fnt, (255, 255, 255), b.rgb("deep"), 4)
    return img
