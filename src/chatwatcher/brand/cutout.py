"""The VTuber cut out of a frame, for thumbnails: an anime-character segmentation model
(skytnt/anime-seg, ISNet, Apache-2.0) run with onnxruntime on the CPU, about a second a
frame. It's downloaded once into <data_dir>/models like the Whisper models.

Cut from the part of the frame where she is (her region, grown to take in her body), so
characters in the game aren't cut out with her. Each cut-out is kept as a PNG in the
clip's thumbs/ folder."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

REPO, FILE = "skytnt/anime-seg", "isnetis.onnx"
SIZE = 1024                     # the model's input
MIN_ALPHA = 24                  # below this is see-through (the model leaves a faint haze)

_lock = threading.Lock()
_session: dict[str, Any] = {}


class CutoutError(Exception):
    pass


def models_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "models"


def model_path(data_dir: Path) -> Path | None:
    """The downloaded model, or None."""
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return None
    p = try_to_load_from_cache(REPO, FILE, cache_dir=str(models_dir(data_dir)))
    return Path(p) if isinstance(p, str) and Path(p).is_file() else None


def download(data_dir: Path) -> Path:
    """Fetch the model (about 170 MB) if it isn't here yet. Slow: only from a job."""
    p = model_path(data_dir)
    if p:
        return p
    try:
        from huggingface_hub import hf_hub_download
        return Path(hf_hub_download(REPO, FILE, cache_dir=str(models_dir(data_dir))))
    except Exception as e:  # network, disk: the thumbnail falls back to framed crops
        raise CutoutError(f"Couldn't download the cut-out model: {e}")


def _model(data_dir: Path):
    p = model_path(data_dir)
    if p is None:
        raise CutoutError("The cut-out model isn't downloaded yet: press ✨ Claude's versions once "
                          "(it fetches it, about 170 MB).")
    if _session.get("path") != p:
        import onnxruntime as ort
        _session.update(path=p, s=ort.InferenceSession(str(p), providers=["CPUExecutionProvider"]))
    return _session["s"]


def mask(data_dir: Path, img: Image.Image) -> Image.Image:
    """How much each pixel is the character (L, the size of img)."""
    img = img.convert("RGB")
    w, h = img.size
    k = SIZE / max(w, h)
    nw, nh = max(1, round(w * k)), max(1, round(h * k))
    x = np.zeros((SIZE, SIZE, 3), np.float32)
    top, left = (SIZE - nh) // 2, (SIZE - nw) // 2
    x[top:top + nh, left:left + nw] = np.asarray(img.resize((nw, nh), Image.BILINEAR), np.float32) / 255
    with _lock:
        s = _model(data_dir)
        out = s.run(None, {s.get_inputs()[0].name: x.transpose(2, 0, 1)[None]})[0]
    m = np.clip(out[0, 0, top:top + nh, left:left + nw], 0, 1) * 255
    return Image.fromarray(m.astype(np.uint8), "L").resize((w, h), Image.BILINEAR)


def cut(data_dir: Path, frame: Image.Image, box: tuple[float, float, float, float]) -> Image.Image:
    """The character in the part box (sx, sy, sw, sh of the frame), RGBA, trimmed to her.
    Raises CutoutError when the model isn't there or finds nobody."""
    fw, fh = frame.size
    sx, sy, sw, sh = box
    part = frame.convert("RGB").crop((round(sx * fw), round(sy * fh), round((sx + sw) * fw), round((sy + sh) * fh)))
    a = mask(data_dir, part).point(lambda v: 0 if v < MIN_ALPHA else v)
    if sw < 0.95:                               # a part round her: only the character in its middle
        a = keep_middle(a)
    bbox = a.point(lambda v: 255 if v > 128 else 0).getbbox()
    if not bbox or (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) < part.width * part.height * 0.01:
        raise CutoutError("Nobody to cut out there: move the box onto her.")
    out = part.convert("RGBA")
    out.putalpha(a)
    return out.crop(bbox)


def keep_middle(alpha: Image.Image) -> Image.Image:
    """Only the shapes that cross the middle of the picture (her, when the part is centred on
    her region); a collab partner or a game character beside her is dropped. Worked out on a
    small copy, so the flood fills stay quick."""
    w, h = alpha.size
    k = 256 / max(w, h)
    sw, sh = max(1, round(w * k)), max(1, round(h * k))
    b = alpha.resize((sw, sh), Image.BILINEAR).point(lambda v: 255 if v > 96 else 0)
    for x in {sw // 2, sw // 2 - sw // 12, sw // 2 + sw // 12}:
        for y in range(sh):
            if b.getpixel((x, y)) == 255:
                ImageDraw.floodfill(b, (x, y), 128)
    keep = b.point(lambda v: 255 if v == 128 else 0)
    if keep.getbbox() is None:
        return alpha
    keep = keep.filter(ImageFilter.MaxFilter(5)).resize((w, h), Image.BILINEAR)
    return ImageChops.multiply(alpha, keep)


def cache_name(t: float, box: tuple[float, float, float, float], clip_mtime: float) -> str:
    key = f"2|{t:.2f}|{'|'.join(f'{v:.3f}' for v in box)}|{clip_mtime:.0f}"
    return "cut_" + hashlib.sha1(key.encode()).hexdigest()[:14] + ".png"


def grow(box: dict[str, float]) -> tuple[float, float, float, float]:
    """A region (her face or her whole model) → the part of the frame to cut her from:
    wider (her hair and arms: up to three times, at most a quarter of the frame more;
    whoever is beside her is dropped by keep_middle), a little higher, and down to the
    bottom of the frame (her body)."""
    cx = box["x"] + box["w"] / 2
    w = min(1.0, box["w"] + min(box["w"] * 2, 0.25))
    x = min(max(cx - w / 2, 0.0), 1 - w)
    y = max(0.0, box["y"] - box["h"] * 0.35)
    return round(x, 4), round(y, 4), round(w, 4), round(1 - y, 4)
