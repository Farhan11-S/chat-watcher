"""All of a clip's frames tiled into one labelled image (a contact sheet).

Claude gets the sheet inside the prompt, so it sees every frame in one go. Opening the
frames one by one with the Read tool took extra round trips, each resending the whole
prompt, and cost several times the tokens."""

from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

TILE_W, TILE_H = 448, 252       # 3 across = 1344 px wide: about 1.4k tokens for 9 frames
COLS = 3
QUALITY = 82


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def contact_sheet(folder: Path, frames: list[dict[str, Any]]) -> bytes | None:
    """JPEG bytes of the frames in a grid, each tile labelled "N · m:ss" (N = its place in
    the frame list, from 1). None if no frame can be opened."""
    tiles = []
    for i, f in enumerate(frames, 1):
        try:
            with Image.open(folder / f["file"]) as im:
                tile = im.convert("RGB")
                tile.thumbnail((TILE_W, TILE_H))
        except (OSError, KeyError):
            continue
        t = float(f.get("clip_s") or 0)
        tiles.append((tile, f"{i} · {int(t // 60)}:{int(t % 60):02d}" + (" ★" if f.get("is_peak") else "")))
    if not tiles:
        return None
    cols = min(COLS, len(tiles))
    rows = math.ceil(len(tiles) / cols)
    sheet = Image.new("RGB", (cols * TILE_W, rows * TILE_H), (0, 0, 0))
    draw = ImageDraw.Draw(sheet)
    font = _font(20)
    for k, (tile, label) in enumerate(tiles):
        x, y = (k % cols) * TILE_W, (k // cols) * TILE_H
        sheet.paste(tile, (x + (TILE_W - tile.width) // 2, y + (TILE_H - tile.height) // 2))
        box = draw.textbbox((x + 6, y + 4), label, font=font)
        draw.rectangle((box[0] - 4, box[1] - 2, box[2] + 4, box[3] + 2), fill=(0, 0, 0))
        draw.text((x + 6, y + 4), label, fill=(255, 255, 0), font=font)
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=QUALITY)
    return buf.getvalue()
