"""Sizes and file names of the thumbnails, and the pieces their layers are drawn with
(brand.canvas): text in the brand font with colour emoji and *starred* words, and a
frame in the Short's layout for the Short's cover."""

from __future__ import annotations

import re

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from chatwatcher.brand import art

W, H = 1280, 720
TW, TH = 1080, 1920
SHAPES = ("wide", "tall")
THUMB_FILE = "thumbnail.jpg"
COVER_FILE = "short_cover.jpg"


def canvas_size(shape: str) -> tuple[int, int]:
    return (TW, TH) if shape == "tall" else (W, H)


# ── text ─────────────────────────────────────────────────────────────────────

_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]")
_JOINERS = re.compile("[\uFE0F\u200D]")
_CJK = re.compile("[\u3040-\u30FF\u3400-\u9FFF\uAC00-\uD7AF\uFF00-\uFFEF]")


def _tokens(text: str) -> list[tuple[str, bool]]:
    """'a *big* deal' → [('a ', False), ('big ', True), ('deal', False)]; words keep their
    trailing space. Text without spaces (Japanese) breaks between characters."""
    out, on = [], False
    text = _JOINERS.sub("", text)
    spaced = " " in text.strip() or not _CJK.search(text)       # one English word stays whole
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
