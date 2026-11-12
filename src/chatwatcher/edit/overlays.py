"""Green-screen overlays: meme clips (the LoL "?" ping spam, an explosion, a "WASTED"
banner) that play over the picture with their sound. Files under data/overlays/, details
in the `overlays` table, like the sound library (edit.sounds).

When one is added, the screen colour is found from the frame's corners and made
transparent once (chromakey), and the clip is saved as VP9 with an alpha channel. Chrome
plays that with its transparency in the editor's preview, and ffmpeg lays it over the
picture when rendering (edit.render). Its sound is saved apart, levelled like a sound
effect but not trimmed, so it stays in sync with the picture.

An overlay on an edit is an effect {"type": "overlay", "overlay": id, "t", "d", "at",
"dx", "dy", "size", "gain", "mode"}: at is "full" (centred on the whole picture), "middle",
or a region (centred where that region is on screen); dx, dy move it from there, as shares
of the picture (you drag it in the editor's preview); size is its width as a share of the
picture's width; gain is its sound's volume in dB; mode is what plays: MODES (both, only its
sound, only its picture). place() works out where it goes; editor.js does the same for its
preview: change both together."""

from __future__ import annotations

import re
import sqlite3
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from chatwatcher.clip.media import MediaError, _exe, probe
from chatwatcher.config import Config
from chatwatcher.edit import layout as lay

OVERLAY = "overlay"
MAX_SECONDS = 15.0
MAX_UPLOAD_BYTES = 80 * 1024 * 1024
MAX_HEIGHT = 720                  # stored at most this tall (an overlay is rarely seen bigger)
SIMILARITY, BLEND = 0.16, 0.06    # chromakey: how close to the key colour is see-through, and the soft edge
SIZE_RANGE = (0.1, 2.5)           # width as a share of the picture's (more than 1 spills off the sides)
GAIN_RANGE = (-30.0, 10.0)
OFFSET_RANGE = (-1.0, 1.0)        # dx, dy: how far it's moved from where it's centred, shares of the picture
PLACES = ("full", "middle")
MODES = ("both", "sound", "picture")   # what plays: its picture and sound, only its sound, only its picture
_ID = re.compile(r"^[a-z0-9_]{1,32}$")


class OverlayError(Exception):
    pass


def overlays_dir(cfg: Config) -> Path:
    return cfg.paths.data_dir / "overlays"


def list_overlays(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM overlays ORDER BY name COLLATE NOCASE")]


def get_overlay(conn: sqlite3.Connection, oid: str) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM overlays WHERE id = ?", (oid,)).fetchone() if valid_id(oid) else None
    return dict(r) if r else None


def valid_id(oid: str) -> bool:
    return bool(_ID.match(oid or ""))


def video_path(cfg: Config, ov: dict[str, Any]) -> Path:
    return overlays_dir(cfg) / ov["file"]


def sound_path(cfg: Config, ov: dict[str, Any]) -> Path | None:
    return overlays_dir(cfg) / ov["sound"] if ov.get("sound") else None


def make_id(conn: sqlite3.Connection, name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9]+", "_", text).strip("_")[:28] or "overlay"
    oid, n = base, 2
    while conn.execute("SELECT 1 FROM overlays WHERE id = ?", (oid,)).fetchone():
        oid, n = f"{base}_{n}", n + 1
    return oid


def _run(args: list[str], timeout: float = 300, data: bytes | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([_exe("ffmpeg"), "-v", "error", *args], input=data, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise OverlayError("that took too long; try a shorter clip") from e


def key_color(src: Path, at: float) -> str:
    """The screen colour: the median of the four corners of a frame, as RRGGBB. Raises
    OverlayError when the corners aren't one bright, saturated colour (no green screen)."""
    out = _run(["-ss", f"{at:.3f}", "-i", str(src), "-frames:v", "1", "-vf", "scale=64:36",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], timeout=60)
    if out.returncode != 0 or len(out.stdout) < 64 * 36 * 3:
        raise OverlayError("this isn't a video file ffmpeg can read")
    px = np.frombuffer(out.stdout[:64 * 36 * 3], dtype=np.uint8).reshape(36, 64, 3).astype(int)
    corners = np.concatenate([px[:4, :4], px[:4, -4:], px[-4:, :4], px[-4:, -4:]]).reshape(-1, 3)
    c = np.median(corners, axis=0).astype(int)
    spread = np.abs(corners - c).max()
    if c.max() - c.min() < 90 or c.max() < 120 or spread > 60:
        raise OverlayError(f"the corners aren't a green (or blue) screen (they're #{c[0]:02X}{c[1]:02X}{c[2]:02X}); "
                           "use a clip with a solid screen colour behind the effect")
    return f"{c[0]:02X}{c[1]:02X}{c[2]:02X}"


def _level(src: Path, dest: Path) -> bool:
    """Its sound as FLAC, levelled like a sound effect but not trimmed (it must stay in
    sync with the picture). False when it has no sound."""
    from chatwatcher.edit import sounds
    try:
        x = sounds._decode(src)
    except sounds.SoundError:
        return False
    if not len(x) or float(np.abs(x).max()) < 10 ** (-80 / 20):
        return False
    win = min(len(x), int(0.4 * sounds.SR))
    power = np.convolve((x.astype(np.float64) ** 2).mean(axis=1), np.ones(win) / win, mode="valid")
    level = 10 * np.log10(max(float(power.max()), 1e-12))
    peak = 20 * np.log10(max(float(np.abs(x).max()), 1e-9))
    gain = min(sounds.LEVEL_DBFS - level, sounds.PEAK_DBFS - peak)
    out = _run(["-y", "-f", "f32le", "-ar", str(sounds.SR), "-ac", "2", "-i", "-", "-c:a", "flac",
                "-sample_fmt", "s16", str(dest)], data=(x * 10 ** (gain / 20)).astype(np.float32).tobytes())
    if out.returncode != 0:
        raise OverlayError(f"couldn't save its sound: {out.stderr.decode(errors='replace')[:200]}")
    return True


def import_overlay(cfg: Config, conn: sqlite3.Connection, src: Path, *, original: str, name: str = "",
                   description: str = "", tags: str = "", source: str = "") -> dict[str, Any]:
    """Key a green-screen clip into data/overlays/<id>.webm (+ <id>.flac) and add it."""
    try:
        info = probe(src)
    except MediaError as e:
        raise OverlayError("this isn't a video file ffmpeg can read") from e
    if not info.width or not info.height:
        raise OverlayError("this file has no picture")
    if info.duration_s > MAX_SECONDS:
        raise OverlayError(f"overlays must be under {MAX_SECONDS:.0f} s (this one is {info.duration_s:.0f} s)")
    color = key_color(src, min(0.2, info.duration_s / 3))
    name = (name or Path(original).stem).strip()[:80] or "overlay"
    oid = make_id(conn, name)
    folder = overlays_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    video, tmp = folder / f"{oid}.webm", folder / f".{oid}.tmp.webm"
    h = min(MAX_HEIGHT, info.height - info.height % 2)
    out = _run(["-y", "-i", str(src), "-an", "-vf",
                f"scale=-2:{h},chromakey=0x{color}:{SIMILARITY}:{BLEND},format=yuva420p",
                "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32", "-deadline", "good", "-cpu-used", "4",
                "-auto-alt-ref", "0", "-row-mt", "1", str(tmp)])
    if out.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        raise OverlayError(f"couldn't key it: {out.stderr.decode(errors='replace')[-200:]}")
    tmp.replace(video)
    sound = folder / f"{oid}.flac"
    has_sound = _level(src, sound)
    w = round(info.width * h / info.height / 2) * 2
    conn.execute(
        """INSERT INTO overlays (id, name, description, tags, source, file, sound, duration_s, width, height,
                                 key_color, original) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (oid, name, description.strip()[:500], _tags(tags), source.strip()[:300], video.name,
         sound.name if has_sound else None, round(probe(video).duration_s or info.duration_s, 3), w, h, color,
         original[:200]))
    return get_overlay(conn, oid) or {}


def _tags(text: str) -> str:
    return ", ".join(dict.fromkeys(t.strip().lower() for t in text.split(",") if t.strip()))[:200]


def update_overlay(conn: sqlite3.Connection, oid: str, *, name: str, description: str, tags: str, source: str,
                   gain_db: float) -> None:
    if not get_overlay(conn, oid):
        raise OverlayError("no such overlay")
    conn.execute("UPDATE overlays SET name = ?, description = ?, tags = ?, source = ?, gain_db = ? WHERE id = ?",
                 (name.strip()[:80] or oid, description.strip()[:500], _tags(tags), source.strip()[:300],
                  round(min(max(gain_db, GAIN_RANGE[0]), GAIN_RANGE[1]), 1), oid))


def delete_overlay(cfg: Config, conn: sqlite3.Connection, oid: str) -> None:
    """Edits that used it keep their markers; rendering skips an overlay that's gone."""
    ov = get_overlay(conn, oid)
    if not ov:
        return
    for p in (video_path(cfg, ov), sound_path(cfg, ov)):
        if p and p.parent == overlays_dir(cfg) and p.is_file():
            p.unlink()
    conn.execute("DELETE FROM overlays WHERE id = ?", (oid,))


# ── on an edit ───────────────────────────────────────────────────────────────

def default_size(at: str, ov: dict[str, Any], out_w: int, out_h: int) -> float:
    """"full" covers the picture (a 16:9 overlay on a Short spills off the sides); anything
    else starts at about half the width."""
    if at == "full":
        return round(min(max(1.0, out_h / out_w * ov["width"] / ov["height"]), SIZE_RANGE[1]), 2)
    return 0.5


@dataclass
class Placed:
    file: Path
    t: float                      # clip time
    d: float
    x: float                      # its centre, as shares of the output picture
    y: float
    w: float                      # its width, as a share of the output picture's width
    aspect: float                 # width / height


def place(at: str, boxes: dict[str, dict[str, float]], panels: list[lay.Panel] | None,
          dx: float = 0.0, dy: float = 0.0) -> tuple[float, float]:
    """Where an overlay's centre goes on the output picture: the centre of `at`, moved by
    dx, dy. panels: a Short's picture (None for 16:9, where the picture is the clip's own frame)."""
    x, y = 0.5, 0.5
    box = boxes.get(at) if at not in PLACES else None
    if box:
        c = (box["x"] + box["w"] / 2, box["y"] + box["h"] / 2)
        p = c if panels is None else lay.map_point(*c, panels)
        if p:
            x, y = p
    return x + dx, y + dy


def placed(cfg: Config, conn: sqlite3.Connection, effects: list[dict[str, Any]],
           boxes: dict[str, dict[str, float]], panels: list[lay.Panel] | None) -> tuple[list[Placed], set[str]]:
    """The overlays of these effects (clip time) whose picture shows, ready to render, and
    the ids that are gone."""
    out, missing = [], set()
    for fx in effects:
        if fx.get("type") != OVERLAY or fx.get("mode") == "sound":
            continue
        ov = get_overlay(conn, fx.get("overlay") or "")
        if not ov or not video_path(cfg, ov).is_file():
            missing.add(fx.get("overlay") or "?")
            continue
        x, y = place(fx.get("at") or "full", boxes, panels, float(fx.get("dx") or 0), float(fx.get("dy") or 0))
        out.append(Placed(video_path(cfg, ov), fx["t"], fx["d"], x, y, float(fx.get("size") or 1.0),
                          ov["width"] / ov["height"]))
    return out, missing


def local(items: list[Placed], start: float, length: float) -> list[tuple[Placed, float, float, float]]:
    """(overlay, when it starts in this part, how long it shows, how far into the overlay
    the part starts) for each overlay that shows in [start, start + length)."""
    out = []
    for p in items:
        a, b = max(p.t, start), min(p.t + p.d, start + length)
        if b - a > 0.04:
            out.append((p, a - start, b - a, a - p.t))
    return out


def filter_graph(base: str, items: list[tuple[Placed, float, float, float]], first_input: int,
                 out_w: int, out_h: int) -> tuple[str, str]:
    """ffmpeg filters laying the overlays (inputs first_input, first_input + 1, …) over the
    stream labelled base. Returns (filters, the label of the result)."""
    chains, cur = [], base
    for k, (p, a, d, _skip) in enumerate(items):
        w = max(2, round(p.w * out_w / 2) * 2)
        h = max(2, round(w / p.aspect / 2) * 2)
        x, y = round(p.x * out_w - w / 2), round(p.y * out_h - h / 2)
        chains.append(f"[{first_input + k}:v]scale={w}:{h},format=yuva420p,setpts=PTS-STARTPTS+{a:.3f}/TB[ov{k}]")
        chains.append(f"{cur}[ov{k}]overlay=x={x}:y={y}:eof_action=pass:format=auto:"
                      f"enable='between(t,{a:.3f},{a + d:.3f})'[ovo{k}]")
        cur = f"[ovo{k}]"
    return ";".join(chains), cur
