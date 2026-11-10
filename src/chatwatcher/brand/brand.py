"""Your channel's brand: name, tagline, colours, logo and fonts. Set once on the Brand
page; the intro and the thumbnails use it.

Settings live in the `settings` table (key "brand"); the logo and fonts in
<data_dir>/brand/. A logo can be an SVG (drawn at 1024 px by ffmpeg's librsvg) or a
picture; transparent edges are trimmed so it sits where it's placed."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
from dataclasses import asdict, dataclass, fields
from functools import lru_cache
from pathlib import Path
from typing import Any

from chatwatcher import prefs
from chatwatcher.config import Config

KEY = "brand"
LOGO_FILE = "logo.png"
FONT_FILES = {"title": "title_font", "text": "text_font"}      # role → stored name (plus its suffix)
LOGO_SUFFIXES = {".svg", ".png", ".jpg", ".jpeg", ".webp"}
FONT_SUFFIXES = {".ttf", ".otf", ".ttc"}
MAX_LOGO_BYTES = 10 * 1024 * 1024
MAX_FONT_BYTES = 40 * 1024 * 1024
LOGO_PX = 1024
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

# Installed fonts tried when you haven't uploaded one, roundest and heaviest first.
TITLE_FONTS = ["M PLUS Rounded 1c:black", "M PLUS Rounded 1c:extrabold", "Nunito:black", "Arial Rounded MT Bold",
               "Noto Sans CJK JP:black", "Noto Sans CJK JP:bold", "DejaVu Sans:bold"]
TEXT_FONTS = ["M PLUS Rounded 1c:extrabold", "M PLUS Rounded 1c:bold", "Nunito:extrabold", "Noto Sans CJK JP:black",
              "Noto Sans CJK JP:bold", "DejaVu Sans:bold"]


class BrandError(Exception):
    pass


@dataclass
class Brand:
    name: str = "MY CLIPS"
    tagline: str = "VTUBER CLIPS"
    deep: str = "#2E1E52"          # outlines, shadows, the logo card's darkest purple
    purple: str = "#7A5CD6"        # gradient edge, glitch tint
    lavender: str = "#B9A0F2"      # wipe, bands
    light: str = "#DACCFA"         # gradient centre
    accent: str = "#FFD36E"        # highlighted *words* on thumbnails
    logo: bool = False             # data/brand/logo.png exists
    title_font: str = ""           # file in data/brand/, "" = an installed font
    text_font: str = ""

    def rgb(self, name: str) -> tuple[int, int, int]:
        h = getattr(self, name)
        return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


COLOURS = ("deep", "purple", "lavender", "light", "accent")


def brand_dir(cfg: Config) -> Path:
    return cfg.paths.data_dir / "brand"


def get(conn: sqlite3.Connection) -> Brand:
    raw = prefs.get(conn, KEY, {}) or {}
    known = {f.name for f in fields(Brand)}
    return Brand(**{k: v for k, v in raw.items() if k in known})


def put(conn: sqlite3.Connection, b: Brand) -> None:
    prefs.put(conn, KEY, asdict(b))


def update(conn: sqlite3.Connection, *, name: str | None = None, tagline: str | None = None,
           colours: dict[str, str] | None = None) -> Brand:
    b = get(conn)
    if name is not None:
        name = re.sub(r"\s+", " ", name).strip()[:40]
        if not name:
            raise BrandError("The channel name can't be empty.")
        b.name = name
    if tagline is not None:
        b.tagline = re.sub(r"\s+", " ", tagline).strip()[:80]
    for k, v in (colours or {}).items():
        if k in COLOURS and v:
            if not _HEX.match(v.strip()):
                raise BrandError(f"{k} isn't a colour like #7A5CD6.")
            setattr(b, k, v.strip().upper())
    put(conn, b)
    return b


# ── logo ─────────────────────────────────────────────────────────────────────

def logo_path(cfg: Config) -> Path:
    return brand_dir(cfg) / LOGO_FILE


def import_logo(cfg: Config, conn: sqlite3.Connection, src: Path, original: str = "") -> Brand:
    """An SVG or picture → data/brand/logo.png (RGBA, transparent edges trimmed)."""
    from PIL import Image, UnidentifiedImageError
    from chatwatcher.clip.media import _exe
    suffix = Path(original or src.name).suffix.lower()
    if suffix not in LOGO_SUFFIXES:
        raise BrandError("The logo must be an .svg, .png, .jpg or .webp file.")
    if src.stat().st_size > MAX_LOGO_BYTES:
        raise BrandError("The logo is bigger than 10 MB.")
    out_dir = brand_dir(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    raster = src
    if suffix == ".svg":
        raster = out_dir / ".logo_svg.png"
        r = subprocess.run([_exe("ffmpeg"), "-v", "error", "-y", "-width", str(LOGO_PX), "-keep_ar", "1",
                            "-i", str(src), "-frames:v", "1", str(raster)], capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not raster.is_file():
            raise BrandError("Couldn't draw that SVG: " + (r.stderr.strip().splitlines() or ["unknown error"])[-1][:200])
    try:
        img = Image.open(raster).convert("RGBA")
    except (UnidentifiedImageError, OSError) as e:
        raise BrandError(f"That isn't a picture this app can read ({e}).")
    finally:
        if raster != src:
            raster.unlink(missing_ok=True)
    box = img.getchannel("A").getbbox()
    if box is None:
        raise BrandError("The logo is completely transparent.")
    img = img.crop(box)
    img.thumbnail((LOGO_PX, LOGO_PX), Image.LANCZOS)
    img.save(logo_path(cfg))
    b = get(conn)
    b.logo = True
    put(conn, b)
    return b


def remove_logo(cfg: Config, conn: sqlite3.Connection) -> Brand:
    logo_path(cfg).unlink(missing_ok=True)
    b = get(conn)
    b.logo = False
    put(conn, b)
    return b


# ── fonts ────────────────────────────────────────────────────────────────────

def import_font(cfg: Config, conn: sqlite3.Connection, role: str, src: Path, original: str = "") -> Brand:
    from PIL import ImageFont
    if role not in FONT_FILES:
        raise BrandError("Unknown font role.")
    suffix = Path(original or src.name).suffix.lower()
    if suffix not in FONT_SUFFIXES:
        raise BrandError("A font must be a .ttf, .otf or .ttc file.")
    if src.stat().st_size > MAX_FONT_BYTES:
        raise BrandError("That font file is bigger than 40 MB.")
    try:
        ImageFont.truetype(str(src), 40)
    except OSError as e:
        raise BrandError(f"That font can't be read ({e}).")
    out_dir = brand_dir(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(FONT_FILES[role] + ".*"):
        old.unlink()
    dest = out_dir / (FONT_FILES[role] + suffix)
    shutil.copyfile(src, dest)
    b = get(conn)
    setattr(b, f"{role}_font", dest.name)
    put(conn, b)
    return b


def remove_font(cfg: Config, conn: sqlite3.Connection, role: str) -> Brand:
    b = get(conn)
    if role in FONT_FILES:
        for old in brand_dir(cfg).glob(FONT_FILES[role] + ".*"):
            old.unlink()
        setattr(b, f"{role}_font", "")
        put(conn, b)
    return b


@lru_cache(maxsize=32)
def _fc_match(query: str) -> str | None:
    """The file of an installed font, only if fontconfig has that family (not a fallback)."""
    try:
        r = subprocess.run(["fc-match", "-f", "%{family}|%{file}", query], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or "|" not in r.stdout:
        return None
    families, file = r.stdout.rsplit("|", 1)
    want = query.split(":")[0].strip().lower()
    return file if any(f.strip().lower() == want for f in families.split(",")) else None


def font_file(cfg: Config, b: Brand, role: str) -> str | None:
    """Your uploaded font for this role, else the best installed one; None = Pillow's own."""
    mine = getattr(b, f"{role}_font", "")
    if mine and (brand_dir(cfg) / mine).is_file():
        return str(brand_dir(cfg) / mine)
    for q in (TITLE_FONTS if role == "title" else TEXT_FONTS):
        f = _fc_match(q)
        if f:
            return f
    return None


def font_label(cfg: Config, b: Brand, role: str) -> str:
    mine = getattr(b, f"{role}_font", "")
    if mine and (brand_dir(cfg) / mine).is_file():
        return "your upload"
    f = font_file(cfg, b, role)
    return Path(f).stem if f else "Pillow's built-in font"


def fingerprint(cfg: Config, b: Brand) -> str:
    """Changes whenever anything that shows in an intro or thumbnail changes."""
    parts: list[Any] = [asdict(b)]
    for p in (logo_path(cfg) if b.logo else None, font_file(cfg, b, "title"), font_file(cfg, b, "text")):
        if p and Path(p).is_file():
            st = Path(p).stat()
            parts.append([str(p), st.st_size, int(st.st_mtime)])
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]
