"""A thumbnail as layers you move, resize and restyle: what the thumbnail maker edits and
Claude composes (brand.thumbai). brand.canvas draws them.

A thumbnail is a list of layers, drawn in order (the first is underneath):
  bg       the background, always first and filling the picture: a frame of the clip (zoomed
           on cx, cy), the same frame blurred, the brand's gradient, or a plain colour
  frame    a part of a frame of the clip (sx, sy, sw, sh: fractions of the frame) as a box,
           rounded box or circle, with a border
  cutout   the VTuber cut out of a frame (brand.cutout), from the part sx, sy, sw, sh of it,
           with a sticker outline around her
  text     words, *stars* make a word the accent colour; wraps at its width
  sticker  an arrow, a ring, a comic burst, an emoji, the channel's logo, or a picture from
           the sticker library (brand.brand.stickers)

Every layer but bg has x, y (its centre) and w (its width), as fractions of the picture's
width and height, rot (degrees, clockwise) and opacity; its height follows from what it
shows. Colours are #RRGGBB or a name (COLOURS, or the brand's: deep, purple, lavender,
light, accent). t is clip time here; the service stores it in stream time."""

from __future__ import annotations

import math
import re
import secrets
from typing import Any

KINDS = ("bg", "frame", "cutout", "text", "sticker")
BG_SOURCES = ("frame", "blur", "brand", "colour")
FRAME_SHAPES = ("rect", "round", "circle")
TEXT_STYLES = ("stroke", "box", "shadow")
FONTS = ("title", "text")
ALIGNS = ("left", "center", "right")
STICKERS = ("arrow", "ring", "burst", "emoji", "logo", "image")
BRAND_COLOURS = ("deep", "purple", "lavender", "light", "accent")
COLOURS = {"white": "#FFFFFF", "black": "#000000", "red": "#FF2B2B", "yellow": "#FFE14D", "orange": "#FF8A1F",
           "green": "#3DDC5A", "blue": "#2E8BFF", "pink": "#FF5FB7", "cyan": "#2FE6E6"}
MAX_LAYERS = 16
MAX_TEXT = 80
PAD = 0.08            # a layer's picture (sprite) has this margin round it, a share of the picture's height
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_ID = re.compile(r"^[a-z0-9]{1,12}$")


class LayerError(Exception):
    pass


# Each field: (kind of value, default, then the range or the choices).
COMMON: dict[str, tuple] = {"x": ("num", 0.5, -0.5, 1.5), "y": ("num", 0.5, -0.5, 1.5), "w": ("num", 0.3, 0.02, 3.0),
                            "rot": ("num", 0.0, -180.0, 180.0), "opacity": ("num", 1.0, 0.05, 1.0),
                            "hidden": ("bool", False), "flip": ("bool", False)}
_CROP = {"sx": ("num", 0.0, 0.0, 0.98), "sy": ("num", 0.0, 0.0, 0.98), "sw": ("num", 1.0, 0.02, 1.0),
         "sh": ("num", 1.0, 0.02, 1.0)}
FIELDS: dict[str, dict[str, tuple]] = {
    "bg": {"source": ("opt", "frame", BG_SOURCES), "t": ("time", 0.0), "zoom": ("num", 1.0, 1.0, 4.0),
           "cx": ("num", 0.5, 0.0, 1.0), "cy": ("num", 0.5, 0.0, 1.0), "colour": ("colour", "deep"),
           "dim": ("num", 0.0, 0.0, 0.9), "sat": ("num", 1.1, 0.0, 2.0), "blur": ("num", 0.025, 0.0, 0.1),
           "hidden": ("bool", False)},
    "frame": {"t": ("time", 0.0), **_CROP, "shape": ("opt", "round", FRAME_SHAPES),
              "border": ("num", 0.012, 0.0, 0.05), "border_colour": ("colour", "white"), "shadow": ("bool", True)},
    "cutout": {"t": ("time", 0.0), **_CROP, "outline": ("num", 0.014, 0.0, 0.05),
               "outline_colour": ("colour", "white"), "glow": ("num", 0.0, 0.0, 1.0), "glow_colour": ("colour", "accent"),
               "shadow": ("bool", True)},
    "text": {"text": ("str", "", MAX_TEXT), "font": ("opt", "title", FONTS), "size": ("num", 0.16, 0.03, 0.5),
             "colour": ("colour", "white"), "accent": ("colour", "accent"), "outline": ("num", 0.14, 0.0, 0.35),
             "outline_colour": ("colour", "black"), "style": ("opt", "stroke", TEXT_STYLES),
             "box_colour": ("colour", "red"), "align": ("opt", "center", ALIGNS)},
    "sticker": {"shape": ("opt", "arrow", STICKERS), "colour": ("colour", "red"), "outline_colour": ("colour", "white"),
                "emoji": ("str", "💀", 8), "image": ("str", "", 80), "aspect": ("num", 1.0, 0.15, 5.0),
                "edge": ("num", 0.04, 0.0, 0.12)},           # edge: its outline, a share of its width
}


def new_id() -> str:
    return secrets.token_hex(4)


def is_colour(v: Any) -> bool:
    return isinstance(v, str) and (bool(_HEX.match(v)) or v in COLOURS or v in BRAND_COLOURS)


def hex_of(v: str, brand: Any) -> str:
    """A colour name or #hex → #hex (brand: brand.brand.Brand, for its colour names)."""
    if v in BRAND_COLOURS:
        return getattr(brand, v)
    return COLOURS.get(v, v if is_colour(v) else "#FFFFFF")


def rgb(v: str, brand: Any) -> tuple[int, int, int]:
    h = hex_of(v, brand)
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def make(kind: str, **fields: Any) -> dict[str, Any]:
    """A layer of this kind with its defaults (no checking: clean() does that)."""
    spec = FIELDS[kind] if kind == "bg" else COMMON | FIELDS[kind]
    return {"id": new_id(), "kind": kind} | {k: v[1] for k, v in spec.items()} | fields


def _num(v: Any) -> float:
    f = float(v)
    if not math.isfinite(f):
        raise ValueError
    return f


def clean_one(raw: Any, duration: float) -> dict[str, Any]:
    """One layer from the browser or Claude, every field checked and clamped. Raises LayerError."""
    if not isinstance(raw, dict) or raw.get("kind") not in KINDS:
        raise LayerError("A layer has no known kind.")
    kind = raw["kind"]
    spec = FIELDS[kind] if kind == "bg" else COMMON | FIELDS[kind]
    lid = str(raw.get("id") or "")
    out: dict[str, Any] = {"id": lid if _ID.match(lid) else new_id(), "kind": kind}
    for k, (typ, default, *rng) in spec.items():
        v = raw.get(k, default)
        try:
            if typ == "num":
                v = round(min(max(_num(v), rng[0]), rng[1]), 4)
            elif typ == "time":
                v = round(min(max(_num(v), 0.0), max(0.0, duration - 0.05)), 3)
            elif typ == "bool":
                v = bool(v)
            elif typ == "opt":
                v = v if v in rng[0] else default
            elif typ == "colour":
                v = v.upper() if isinstance(v, str) and _HEX.match(v) else (v if is_colour(v) else default)
            elif typ == "str":
                v = re.sub(r"[ \t]+", " ", str(v if v is not None else "")).strip()[:rng[0]]
        except (TypeError, ValueError):
            raise LayerError(f"The {kind} layer's {k} isn't a number.")
        out[k] = v
    if kind in ("frame", "cutout"):                # the part of the frame stays inside it
        out["sw"] = min(out["sw"], round(1 - out["sx"], 4))
        out["sh"] = min(out["sh"], round(1 - out["sy"], 4))
    if kind == "sticker" and out["shape"] == "image":
        out["image"] = re.sub(r"[^\w.\- ]", "", out["image"])
    return out


def clean(raw: Any, duration: float) -> list[dict[str, Any]]:
    """A whole thumbnail: at most one bg, and it goes first. Raises LayerError."""
    if not isinstance(raw, list):
        raise LayerError("The layers must be a list.")
    if len(raw) > MAX_LAYERS + 1:
        raise LayerError(f"At most {MAX_LAYERS} layers on a thumbnail.")
    layers = [clean_one(r, duration) for r in raw]
    bgs = [ly for ly in layers if ly["kind"] == "bg"][:1]
    rest = [ly for ly in layers if ly["kind"] != "bg"]
    seen: set[str] = set()
    for ly in bgs + rest:                          # ids are unique (the editor finds layers by them)
        if ly["id"] in seen:
            ly["id"] = new_id()
        seen.add(ly["id"])
    return bgs + rest


def shift_times(layers: list[dict[str, Any]], by: float) -> list[dict[str, Any]]:
    """Clip time ↔ stream time (by = ±zero_s)."""
    return [ly | {"t": round(ly["t"] + by, 3)} if "t" in ly else ly for ly in layers]


def from_settings(s: dict[str, Any], aspect_tall: bool = False) -> list[dict[str, Any]]:
    """The old fixed-style settings ({t, text, zoom, cx, cy, side, place, ...}) as layers:
    the frame as the background and the text on the side it was on (no band, no badge)."""
    layers = [make("bg", source="frame", t=float(s.get("t", 0)), zoom=float(s.get("zoom", 1)),
                   cx=float(s.get("cx", 0.5)), cy=float(s.get("cy", 0.5)))]
    text = str(s.get("text") or "")
    if text:
        if aspect_tall:
            y = {"top": 0.22, "middle": 0.48, "bottom": 0.62}.get(s.get("place"), 0.22)
            layers.append(make("text", text=text, x=0.5, y=y, w=0.84, size=0.075, align="center"))
        else:
            left = s.get("side") != "right"
            layers.append(make("text", text=text, x=0.27 if left else 0.73, y=0.3, w=0.48, size=0.17,
                               align="left" if left else "right"))
    return layers
