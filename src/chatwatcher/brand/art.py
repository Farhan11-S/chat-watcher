"""Drawing pieces shared by the intro and the thumbnails (Pillow): the logo with its
shadow, the brand background, sparkles, outlined text, glitches.

Sizes are written for 1080p and multiplied by k = height / 1080."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from chatwatcher.brand.brand import Brand

RGB = tuple[int, int, int]


def ease_out_back(t: float, s: float = 1.9) -> float:
    t -= 1
    return t * t * ((s + 1) * t + s) + 1


def ease_out_cubic(t: float) -> float:
    return 1 - (1 - t) ** 3


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def font(path: str | None, size: float) -> ImageFont.FreeTypeFont:
    size = max(6, int(size))
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def zoom(img: Image.Image, s: float, cx: float = 0.5, cy: float = 0.5) -> Image.Image:
    """Scale by s around (cx, cy), keeping the canvas size."""
    if abs(s - 1) < 1e-3:
        return img
    w, h = img.size
    nw, nh = max(1, int(w * s)), max(1, int(h * s))
    big = img.resize((nw, nh), Image.BICUBIC)
    if s >= 1:
        x, y = int((nw - w) * cx), int((nh - h) * cy)
        return big.crop((x, y, x + w, y + h))
    canvas = Image.new(img.mode, (w, h))
    canvas.paste(big, ((w - nw) // 2, (h - nh) // 2))
    return canvas


def rgb_split(img: Image.Image, dx: int) -> Image.Image:
    r, g, b = img.convert("RGB").split()
    return Image.merge("RGB", (ImageChops.offset(r, dx, 0), g, ImageChops.offset(b, -dx, 0)))


def slice_glitch(img: Image.Image, n: int, max_shift: int, rng: random.Random, k: float) -> Image.Image:
    w, h = img.size
    out = img.copy()
    for _ in range(n):
        y = rng.randrange(0, max(1, h - int(20 * k)))
        hh = rng.randrange(max(1, int(8 * k)), max(2, int(90 * k)))
        band = img.crop((0, y, w, min(h, y + hh)))
        out.paste(ImageChops.offset(band, rng.randint(-max_shift, max_shift), 0), (0, y))
    return out


def tint(img: Image.Image, color: RGB, amount: float) -> Image.Image:
    img = img.convert("RGB")
    return Image.blend(img, Image.new("RGB", img.size, color), amount)


def sparkle(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, fill) -> None:
    q = r * 0.28
    d.polygon([(cx, cy - r), (cx + q, cy - q), (cx + r, cy), (cx + q, cy + q),
               (cx, cy + r), (cx - q, cy + q), (cx - r, cy), (cx - q, cy - q)], fill=fill)


def triangle(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, ang: float, outline=None, width: int = 0) -> None:
    pts = [(cx + r * math.cos(ang + i * 2 * math.pi / 3), cy + r * math.sin(ang + i * 2 * math.pi / 3))
           for i in range(3)]
    d.polygon(pts, outline=outline, width=width)


def outlined_text(base: Image.Image, xy, text: str, fnt, fill: RGB, stroke: RGB, stroke_w: int, *,
                  anchor: str = "lm", alpha: int = 255, shadow: RGB | None = None, k: float = 1.0) -> None:
    """Text with an outline and (optionally) a soft offset shadow, onto an RGBA image."""
    if alpha <= 0 or not text:
        return
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if shadow:
        sx, sy = xy[0] + 8 * k, xy[1] + 10 * k
        d.text((sx, sy), text, font=fnt, fill=shadow + (150,), anchor=anchor, stroke_width=stroke_w,
               stroke_fill=shadow + (150,))
    d.text(xy, text, font=fnt, fill=fill + (255,), anchor=anchor, stroke_width=stroke_w, stroke_fill=stroke + (255,))
    if alpha < 255:
        layer.putalpha(layer.getchannel("A").point(lambda v: v * alpha // 255))
    base.alpha_composite(layer)


def fade_alpha(img: Image.Image, alpha: float) -> Image.Image:
    if alpha >= 1:
        return img
    out = img.copy()
    out.putalpha(out.getchannel("A").point(lambda v: int(v * alpha)))
    return out


# ── the logo ─────────────────────────────────────────────────────────────────

def plain_logo(b: Brand, size: int, title_font: str | None) -> Image.Image:
    """A round badge with the name's first letter, for a brand without a logo."""
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, s - 1, s - 1), fill=b.rgb("deep") + (255,))
    d.ellipse((s * 0.12, s * 0.12, s * 0.88, s * 0.88), outline=b.rgb("lavender") + (255,), width=int(s * 0.11))
    letter = (b.name.strip()[:1] or "C").upper()
    d.text((s / 2, s / 2), letter, font=font(title_font, s * 0.46), fill=(255, 255, 255, 255), anchor="mm")
    return img.resize((size, size), Image.LANCZOS)


def logo(b: Brand, logo_file: Path | None, size: int, title_font: str | None, *, shadow: bool = True,
         k: float = 1.0) -> Image.Image:
    """The logo fitted in size×size, with a soft drop shadow around it (so the image is
    bigger than size by a margin of 80·k on each side when shadow is on)."""
    if logo_file and logo_file.is_file():
        src = Image.open(logo_file).convert("RGBA")
        src.thumbnail((size, size), Image.LANCZOS)
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        img.alpha_composite(src, ((size - src.width) // 2, (size - src.height) // 2))
    else:
        img = plain_logo(b, size, title_font)
    if not shadow:
        return img
    pad = int(80 * k)
    out = Image.new("RGBA", (size + pad * 2, size + pad * 2), (0, 0, 0, 0))
    sh = Image.new("RGBA", out.size, (0, 0, 0, 0))
    sh.paste(Image.new("RGBA", img.size, b.rgb("deep") + (170,)), (pad + int(10 * k), pad + int(22 * k)),
             img.getchannel("A"))
    out.alpha_composite(sh.filter(ImageFilter.GaussianBlur(max(1, 22 * k))))
    out.alpha_composite(img, (pad, pad))
    return out


# ── the brand background ─────────────────────────────────────────────────────

def background(b: Brand, w: int, h: int, centre: tuple[float, float] = (0.36, 0.5)) -> Image.Image:
    """Light-to-purple radial gradient with two halftone dot patches (RGBA)."""
    cx, cy = w * centre[0], h * centre[1]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    d = np.minimum(1.0, np.hypot(xx - cx, yy - cy) / (math.hypot(w, h) * 0.62)) ** 1.3
    light, edge = np.array(b.rgb("light"), np.float32), np.array(b.rgb("purple"), np.float32)
    arr = light * (1 - d[..., None]) + edge * d[..., None]
    base = Image.fromarray(arr.astype(np.uint8), "RGB").convert("RGBA")
    k = h / 1080
    dots = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    dd = ImageDraw.Draw(dots)
    step = max(4, int(22 * k))
    for ox, oy, rad in ((w * 0.86, h * 0.2, int(330 * k)), (w * 0.1, h * 0.86, int(280 * k))):
        for gy in range(-rad, rad, step):
            for gx in range(-rad, rad, step):
                dist = math.hypot(gx, gy) / rad
                if dist < 1:
                    r = 6 * k * (1 - dist)
                    dd.ellipse((ox + gx - r, oy + gy - r, ox + gx + r, oy + gy + r), fill=(255, 255, 255, 70))
    base.alpha_composite(dots)
    return base


def rays(w: int, h: int, cx: float, cy: float, turn: float, alpha: int = 34) -> Image.Image:
    """12 slowly turning light rays from (cx, cy)."""
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    far = math.hypot(w, h) * 1.2
    for r in range(12):
        a0 = r * math.pi / 6 + turn
        d.polygon([(cx, cy), (cx + far * math.cos(a0), cy + far * math.sin(a0)),
                   (cx + far * math.cos(a0 + 0.22), cy + far * math.sin(a0 + 0.22))], fill=(255, 255, 255, alpha))
    return layer


@dataclass
class Particle:
    kind: str
    x: float
    y: float
    r: float
    vx: float
    vy: float
    ph: float
    rot: float
    vr: float


def particles(n: int, w: int, h: int, rng: random.Random, k: float) -> list[Particle]:
    return [Particle(rng.choice(["spark", "spark", "tri", "ring"]), rng.uniform(0, w), rng.uniform(0, h),
                     rng.uniform(10, 34) * k, rng.uniform(-1.2, 1.2) * k, rng.uniform(-2.4, -0.6) * k,
                     rng.uniform(0, 6.28), rng.uniform(0, 6.28), rng.uniform(-0.05, 0.05)) for _ in range(n)]


def draw_particles(img: Image.Image, parts: list[Particle], f: float, alpha: float, k: float) -> None:
    """f: frame number at 30 fps (may be fractional)."""
    w, h = img.size
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    lw = max(1, int(5 * k))
    for p in parts:
        x, y = (p.x + p.vx * f) % w, (p.y + p.vy * f) % h
        tw = 0.6 + 0.4 * math.sin(p.ph + f * 0.25)
        a = int(230 * alpha * tw)
        r = p.r * (0.8 + 0.2 * tw)
        if p.kind == "spark":
            sparkle(d, x, y, r, (255, 255, 255, a))
        elif p.kind == "tri":
            triangle(d, x, y, r * 1.2, p.rot + p.vr * f, outline=(255, 255, 255, a), width=lw)
        else:
            d.ellipse((x - r * 0.6, y - r * 0.6, x + r * 0.6, y + r * 0.6), outline=(255, 255, 255, a), width=lw)
    img.alpha_composite(layer)
