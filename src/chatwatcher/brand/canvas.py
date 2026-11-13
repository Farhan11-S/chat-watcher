"""Drawing a layered thumbnail (brand.layers) with Pillow.

Each layer but the background is drawn on its own as a sprite: its content (w of the
picture's width, as tall as it needs) unrotated, with a margin of layers.PAD × the
picture's height on every side for outlines and shadows. The thumbnail is the sprites
rotated and laid over the background, centred on each layer's x, y. The editor
(static/thumb.js) shows the same sprites and moves, scales and turns them itself, so
what you see while dragging is what render() draws."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from chatwatcher.brand import art
from chatwatcher.brand import layers as L
from chatwatcher.brand.brand import Brand
from chatwatcher.brand.thumb import Ink, _wrap

Box = tuple[float, float, float, float]


@dataclass
class Source:
    """What a thumbnail is drawn from."""
    size: tuple[int, int]                                   # the picture: 1280×720 or 1080×1920
    brand: Brand
    logo: Path | None
    fonts: dict[str, str | None]                            # title / text → font file
    picture: Callable[[float], Image.Image]                 # what the background shows at t (a Short: its layout)
    frame: Callable[[float], Image.Image]                   # the clip's frame at t
    cutout: Callable[[float, Box], Image.Image]             # her, cut out of the frame at t (RGBA, trimmed)
    stickers: Path | None = None                            # the sticker library's folder

    @property
    def pad(self) -> int:
        return round(L.PAD * self.size[1])


# ── shared pieces ────────────────────────────────────────────────────────────

def dilate(alpha: Image.Image, r: float) -> Image.Image:
    """The shape grown by about r px with rounded corners (two box blurs and a threshold)."""
    if r < 0.5:
        return alpha
    half = max(1, round(r / 2))
    grown = alpha.filter(ImageFilter.BoxBlur(half)).filter(ImageFilter.BoxBlur(half))
    return grown.point(lambda v: 255 if v > 6 else v * 42).filter(ImageFilter.GaussianBlur(0.8))


def _solid(size: tuple[int, int], colour: tuple[int, int, int], alpha: Image.Image) -> Image.Image:
    out = Image.new("RGBA", size, colour + (255,))
    out.putalpha(alpha)
    return out


def _with_edges(body: Image.Image, *, outline: float = 0, outline_rgb=(255, 255, 255), glow: float = 0,
                glow_rgb=(255, 255, 255), shadow: bool = False, k: float = 1.0) -> Image.Image:
    """body (RGBA, padded) with an outline round its shape, a glow and a drop shadow under it."""
    a = body.getchannel("A")
    out = Image.new("RGBA", body.size, (0, 0, 0, 0))
    edge = dilate(a, outline) if outline >= 0.5 else a
    if shadow:
        sh = _solid(body.size, (0, 0, 0), edge.point(lambda v: v * 150 // 255))
        sh = sh.filter(ImageFilter.GaussianBlur(10 * k))
        out.alpha_composite(sh, (round(6 * k), round(12 * k)))
    if glow > 0:
        g = dilate(edge, 14 * k).filter(ImageFilter.GaussianBlur(16 * k))
        out.alpha_composite(_solid(body.size, glow_rgb, g.point(lambda v: min(255, int(v * glow * 1.6)))))
    if outline >= 0.5:
        out.alpha_composite(_solid(body.size, outline_rgb, edge))
    out.alpha_composite(body)
    return out


def _padded(content: Image.Image, pad: int) -> Image.Image:
    out = Image.new("RGBA", (content.width + 2 * pad, content.height + 2 * pad), (0, 0, 0, 0))
    out.alpha_composite(content.convert("RGBA"), (pad, pad))
    return out


# ── the background ───────────────────────────────────────────────────────────

def bg_crop(pw: int, ph: int, ly: dict[str, Any], aspect: float) -> tuple[int, int, int, int]:
    """The part of the picture the background shows: the picture's aspect, 1/zoom of it,
    centred on (cx, cy) if it can be."""
    cw = pw / ly["zoom"]
    ch = cw / aspect
    if ch > ph / ly["zoom"]:
        ch = ph / ly["zoom"]
        cw = ch * aspect
    x = min(max(ly["cx"] * pw - cw / 2, 0), pw - cw)
    y = min(max(ly["cy"] * ph - ch / 2, 0), ph - ch)
    return round(x), round(y), round(x + cw), round(y + ch)


def draw_bg(ly: dict[str, Any] | None, src: Source) -> Image.Image:
    W, H = src.size
    if ly is None or ly.get("hidden"):
        return Image.new("RGB", (W, H), (0, 0, 0))
    b = src.brand
    if ly["source"] in ("frame", "blur"):
        pic = src.picture(ly["t"]).convert("RGB")
        img = pic.crop(bg_crop(pic.width, pic.height, ly, W / H)).resize((W, H), Image.LANCZOS)
        if ly["source"] == "blur":
            img = img.filter(ImageFilter.GaussianBlur(max(1.0, ly["blur"] * W)))
        else:
            img = ImageEnhance.Sharpness(img).enhance(1.2)
    elif ly["source"] == "brand":
        img = art.background(b, W, H).convert("RGB")
    else:
        img = Image.new("RGB", (W, H), L.rgb(ly["colour"], b))
    if abs(ly["sat"] - 1) > 0.01:
        img = ImageEnhance.Color(img).enhance(ly["sat"])
    if ly["dim"] > 0:
        img = Image.blend(img, Image.new("RGB", (W, H), (0, 0, 0)), ly["dim"])
    return img


# ── the layers ───────────────────────────────────────────────────────────────

def _frame(ly: dict[str, Any], src: Source) -> Image.Image:
    W, H = src.size
    k, pad = H / 720, src.pad
    fr = src.frame(ly["t"]).convert("RGB")
    fw, fh = fr.size
    part = fr.crop((round(ly["sx"] * fw), round(ly["sy"] * fh), round((ly["sx"] + ly["sw"]) * fw),
                    round((ly["sy"] + ly["sh"]) * fh)))
    cw = max(4, round(ly["w"] * W))
    if ly["shape"] == "circle":                   # the middle square of the part
        s = min(part.size)
        part = part.crop(((part.width - s) // 2, (part.height - s) // 2, (part.width + s) // 2, (part.height + s) // 2))
    ch = max(4, round(cw * part.height / max(1, part.width)))
    part = part.resize((cw, ch), Image.LANCZOS)
    if ly["flip"]:
        part = part.transpose(Image.FLIP_LEFT_RIGHT)
    mask = Image.new("L", (cw * 2, ch * 2), 0)
    d = ImageDraw.Draw(mask)
    if ly["shape"] == "circle":
        d.ellipse((0, 0, cw * 2 - 1, ch * 2 - 1), fill=255)
    elif ly["shape"] == "round":
        d.rounded_rectangle((0, 0, cw * 2 - 1, ch * 2 - 1), radius=round(min(cw, ch) * 0.16), fill=255)
    else:
        d.rectangle((0, 0, cw * 2, ch * 2), fill=255)
    part = part.convert("RGBA")
    part.putalpha(mask.resize((cw, ch), Image.LANCZOS))
    return _with_edges(_padded(part, pad), outline=ly["border"] * H, outline_rgb=L.rgb(ly["border_colour"], src.brand),
                       shadow=ly["shadow"], k=k)


def _cutout(ly: dict[str, Any], src: Source) -> Image.Image:
    W, H = src.size
    k, pad = H / 720, src.pad
    her = src.cutout(ly["t"], (ly["sx"], ly["sy"], ly["sw"], ly["sh"]))
    cw = max(4, round(ly["w"] * W))
    ch = max(4, round(cw * her.height / max(1, her.width)))
    her = her.resize((cw, ch), Image.LANCZOS)
    if ly["flip"]:
        her = her.transpose(Image.FLIP_LEFT_RIGHT)
    b = src.brand
    return _with_edges(_padded(her, pad), outline=ly["outline"] * H, outline_rgb=L.rgb(ly["outline_colour"], b),
                       glow=ly["glow"], glow_rgb=L.rgb(ly["glow_colour"], b), shadow=ly["shadow"], k=k)


def text_lines(ly: dict[str, Any], src: Source) -> tuple[Ink, list, float]:
    """(the ink, the wrapped lines, the line height) of a text layer."""
    W, H = src.size
    px = max(8, round(ly["size"] * H))
    font = src.fonts.get(ly["font"]) or src.fonts.get("title")
    ink = Ink(font, px)
    width = max(10.0, ly["w"] * W)
    paragraphs = [p for p in ly["text"].split("\n") if p.strip()] or [" "]
    lines = _wrap(ink, paragraphs, width)
    widest = max((ink.length("".join(p for p, _ in ln).rstrip()) for ln in lines), default=0)
    if widest > width:                            # a word longer than the width: smaller, not broken
        ink = Ink(font, max(8, int(px * width / widest)))
        lines = _wrap(ink, paragraphs, width)
    return ink, lines, ink.size * (1.22 if ly["style"] == "box" else 1.04)


def _text(ly: dict[str, Any], src: Source) -> Image.Image:
    W, H = src.size
    pad, b = src.pad, src.brand
    ink, lines, line_h = text_lines(ly, src)
    px = ink.size
    cw, ch = max(4, round(ly["w"] * W)), max(4, round(line_h * len(lines)))
    size = (cw + 2 * pad, ch + 2 * pad)
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    shadow = Image.new("RGBA", size, (0, 0, 0, 0))
    fill, accent, edge = L.rgb(ly["colour"], b), L.rgb(ly["accent"], b), L.rgb(ly["outline_colour"], b)
    style = ly["style"]
    stroke = round(ly["outline"] * px) if style == "stroke" else max(0, round(ly["outline"] * px * 0.35))
    if style == "box":
        stroke = 0
    for i, ln in enumerate(lines):
        pieces = [(p.rstrip() if j == len(ln) - 1 else p, hi) for j, (p, hi) in enumerate(ln)]
        total = sum(ink.length(p) for p, _ in pieces)
        x0 = pad + {"left": 0, "center": (cw - total) / 2, "right": cw - total}[ly["align"]]
        y = pad + line_h * (i + 0.5)
        if style == "box":
            m = px * 0.16
            ImageDraw.Draw(layer).rounded_rectangle((x0 - m, y - line_h / 2 + px * 0.05, x0 + total + m, y + line_h / 2 - px * 0.05),
                                                    radius=px * 0.12, fill=L.rgb(ly["box_colour"], b) + (255,))
        x = x0
        # Ink.draw also draws an offset shadow; on a box there's none (it goes to a throwaway image).
        sink = Image.new("RGBA", size, (0, 0, 0, 0)) if style == "box" else shadow
        for p, hi in pieces:
            x = ink.draw(layer, sink, x, y, p, accent if hi else fill, edge, stroke)
    if style == "shadow":
        shadow = shadow.filter(ImageFilter.GaussianBlur(max(2, px * 0.09)))
    shadow.alpha_composite(layer)
    return shadow


def _emoji(ch: str) -> Image.Image | None:
    from chatwatcher.brand.brand import _fc_match
    path = _fc_match("Noto Color Emoji")
    if not path:
        return None
    big = Image.new("RGBA", (160, 160), (0, 0, 0, 0))
    try:
        ImageDraw.Draw(big).text((4, 4), ch, font=ImageFont.truetype(path, 109), embedded_color=True)
    except OSError:
        return None
    box = big.getbbox()
    return big.crop(box) if box else None


def _shape_mask(kind: str, cw: int, ch: int) -> Image.Image:
    """An arrow (pointing right), ring or burst as a mask, drawn at twice the size and shrunk."""
    S = 2
    m = Image.new("L", (cw * S, ch * S), 0)
    d = ImageDraw.Draw(m)
    w, h = cw * S, ch * S
    if kind == "arrow":
        head = min(h * 1.15, w * 0.5)
        shaft = h * 0.44
        d.polygon([(0, h / 2 - shaft / 2), (w - head, h / 2 - shaft / 2), (w - head, 0), (w, h / 2),
                   (w - head, h), (w - head, h / 2 + shaft / 2), (0, h / 2 + shaft / 2)], fill=255)
    elif kind == "ring":
        t = max(6, min(w, h) * 0.1)
        d.ellipse((0, 0, w - 1, h - 1), fill=255)
        d.ellipse((t, t, w - 1 - t, h - 1 - t), fill=0)
    else:                                          # a comic burst
        n, pts = 12, []
        for i in range(n * 2):
            a = math.pi * i / n - math.pi / 2
            r = 0.5 if i % 2 == 0 else 0.33 + 0.05 * ((i // 2) % 2)
            pts.append((w / 2 + math.cos(a) * r * w, h / 2 + math.sin(a) * r * h))
        d.polygon(pts, fill=255)
    return m.resize((cw, ch), Image.LANCZOS)


def _sticker(ly: dict[str, Any], src: Source) -> Image.Image:
    W, H = src.size
    k, pad, b = H / 720, src.pad, src.brand
    cw = max(4, round(ly["w"] * W))
    shape = ly["shape"]
    if shape in ("arrow", "ring", "burst"):
        ch = max(4, round(cw * (0.42 if shape == "arrow" else ly["aspect"])))
        body = _solid((cw, ch), L.rgb(ly["colour"], b), _shape_mask(shape, cw, ch))
    else:
        if shape == "emoji":
            pic = _emoji(ly["emoji"])
        elif shape == "logo":
            pic = art.logo(b, src.logo, max(8, cw), src.fonts.get("title"), shadow=False)
        else:
            p = (src.stickers / ly["image"]) if src.stickers and ly["image"] else None
            pic = Image.open(p).convert("RGBA") if p and p.is_file() else None
        if pic is None:                            # a missing picture: a grey "?" so you see it's gone
            pic = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
            ImageDraw.Draw(pic).text((50, 50), "?", font=art.font(src.fonts.get("title"), 80), fill=(200, 200, 200, 255), anchor="mm")
        ch = max(4, round(cw * pic.height / max(1, pic.width)))
        body = pic.resize((cw, ch), Image.LANCZOS)
    if ly["flip"]:
        body = body.transpose(Image.FLIP_LEFT_RIGHT)
    return _with_edges(_padded(body, pad), outline=ly["edge"] * cw, outline_rgb=L.rgb(ly["outline_colour"], b),
                       shadow=True, k=k * 0.6)


DRAW = {"frame": _frame, "cutout": _cutout, "text": _text, "sticker": _sticker}


def sprite(ly: dict[str, Any], src: Source) -> Image.Image:
    """A layer's sprite (RGBA, unrotated, its full opacity); the background: the whole picture."""
    if ly["kind"] == "bg":
        return draw_bg(ly, src).convert("RGBA")
    return DRAW[ly["kind"]](ly, src)


def place(base: Image.Image, ly: dict[str, Any], spr: Image.Image) -> None:
    """Lay a sprite over the picture: turned by rot (clockwise), at its opacity, centred on x, y."""
    W, H = base.size
    if abs(ly["rot"]) > 0.05:
        spr = spr.rotate(-ly["rot"], resample=Image.BICUBIC, expand=True)
    if ly["opacity"] < 1:
        spr.putalpha(spr.getchannel("A").point(lambda v: int(v * ly["opacity"])))
    base.alpha_composite(spr, (round(ly["x"] * W - spr.width / 2), round(ly["y"] * H - spr.height / 2)))


def render(layers: list[dict[str, Any]], src: Source, *,
           on_error: Callable[[dict[str, Any], Exception], None] | None = None) -> Image.Image:
    """The thumbnail (RGB). A layer that can't be drawn (her cut-out, a frame) is left out
    and passed to on_error; without on_error it raises."""
    bg = next((ly for ly in layers if ly["kind"] == "bg"), None)
    img = draw_bg(bg, src).convert("RGBA")
    for ly in layers:
        if ly["kind"] == "bg" or ly.get("hidden"):
            continue
        try:
            place(img, ly, sprite(ly, src))
        except Exception as e:
            if on_error is None:
                raise
            on_error(ly, e)
    return img.convert("RGB")


def content_size(ly: dict[str, Any], spr: Image.Image, src: Source) -> tuple[float, float]:
    """The layer's own box (without the sprite's margin) as fractions of the picture."""
    W, H = src.size
    return (spr.width - 2 * src.pad) / W, (spr.height - 2 * src.pad) / H
