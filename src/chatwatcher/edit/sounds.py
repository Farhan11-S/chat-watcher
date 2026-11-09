"""Your sound-effect library: files under data/sounds/, details in the `sounds` table.

Every sound is cleaned up on import, so it behaves the same wherever it's placed: silence
at the start and end is trimmed (the sound starts exactly where its marker is), and it is
levelled so its loudest moment is about as loud as normal talking. Claude only ever picks
sounds from this library, using each sound's description.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from chatwatcher.clip.media import MediaError, _exe
from chatwatcher.config import Config

SR = 48000
LEVEL_DBFS = -14.0         # loudest 400 ms of every sound, after levelling
PEAK_DBFS = -1.0           # …unless that would clip
SILENCE_DB = 40.0          # quieter than the file's peak by this much = silence (trimmed from both ends)
MAX_SECONDS = 30.0
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_ID = re.compile(r"^[a-z0-9_]{1,32}$")


class SoundError(Exception):
    pass


def sounds_dir(cfg: Config) -> Path:
    return cfg.paths.data_dir / "sounds"


def list_sounds(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM sounds ORDER BY name COLLATE NOCASE")]


def get_sound(conn: sqlite3.Connection, sound_id: str) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM sounds WHERE id = ?", (sound_id,)).fetchone()
    return dict(r) if r else None


def sound_path(cfg: Config, sound: dict[str, Any]) -> Path:
    return sounds_dir(cfg) / sound["file"]


def make_id(conn: sqlite3.Connection, name: str) -> str:
    """'FAAAH!!' → 'faaah'; a number is added if that id is taken."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9]+", "_", text).strip("_")[:28] or "sound"
    sid, n = base, 2
    while conn.execute("SELECT 1 FROM sounds WHERE id = ?", (sid,)).fetchone():
        sid, n = f"{base}_{n}", n + 1
    return sid


def _decode(src: Path) -> np.ndarray:
    try:
        out = subprocess.run([_exe("ffmpeg"), "-v", "error", "-i", str(src), "-vn", "-ac", "2", "-ar", str(SR),
                              "-t", str(MAX_SECONDS + 1), "-f", "f32le", "-"], capture_output=True, timeout=120)
    except subprocess.TimeoutExpired as e:
        raise SoundError("reading the file took too long") from e
    if out.returncode != 0 or not out.stdout:
        raise SoundError("this isn't an audio or video file ffmpeg can read")
    return np.frombuffer(out.stdout, dtype="<f4").reshape(-1, 2)


@dataclass
class Cleaned:
    samples: np.ndarray        # float32 stereo, trimmed and levelled
    gain_db: float             # what levelling changed


def clean(x: np.ndarray) -> Cleaned:
    """Trim silent ends and level the loudest 400 ms to LEVEL_DBFS (peaks kept below PEAK_DBFS)."""
    if not len(x):
        raise SoundError("the file has no sound")
    mono = np.abs(x).max(axis=1)
    top = float(mono.max())
    if top < 10 ** (-80 / 20):
        raise SoundError("the file is silent")
    loud = np.flatnonzero(mono > top * 10 ** (-SILENCE_DB / 20))
    pad = int(0.01 * SR)
    x = x[max(loud[0] - pad, 0): loud[-1] + pad + 1]
    if len(x) / SR > MAX_SECONDS:
        raise SoundError(f"sound effects must be under {MAX_SECONDS:.0f} s (this one is longer); trim it first")
    win = min(len(x), int(0.4 * SR))
    power = np.convolve((x.astype(np.float64) ** 2).mean(axis=1), np.ones(win) / win, mode="valid")
    level = 10 * np.log10(max(float(power.max()), 1e-12))
    peak = 20 * np.log10(max(float(np.abs(x).max()), 1e-9))
    gain = min(LEVEL_DBFS - level, PEAK_DBFS - peak)
    return Cleaned((x * 10 ** (gain / 20)).astype(np.float32), gain)


def import_sound(cfg: Config, conn: sqlite3.Connection, src: Path, *, original: str, name: str = "",
                 description: str = "", tags: str = "", source: str = "") -> dict[str, Any]:
    """Clean `src` into data/sounds/<id>.flac and add it to the library."""
    name = (name or Path(original).stem).strip()[:80] or "sound"
    c = clean(_decode(src))
    sid = make_id(conn, name)
    folder = sounds_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{sid}.flac"
    tmp = folder / f".{sid}.tmp.flac"
    try:
        out = subprocess.run([_exe("ffmpeg"), "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "2",
                              "-i", "-", "-c:a", "flac", "-sample_fmt", "s16", str(tmp)],
                             input=c.samples.tobytes(), capture_output=True, timeout=120)
    except (subprocess.TimeoutExpired, MediaError) as e:
        tmp.unlink(missing_ok=True)
        raise SoundError(str(e)) from e
    if out.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise SoundError(f"couldn't save the sound: {out.stderr.decode(errors='replace')[:200]}")
    tmp.replace(dest)
    conn.execute(
        """INSERT INTO sounds (id, name, description, tags, source, file, duration_s, original)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (sid, name, description.strip()[:500], _tags(tags), source.strip()[:300], dest.name,
         round(len(c.samples) / SR, 3), original[:200]))
    return get_sound(conn, sid) or {}


def _tags(text: str) -> str:
    return ", ".join(dict.fromkeys(t.strip().lower() for t in text.split(",") if t.strip()))[:200]


def update_sound(conn: sqlite3.Connection, sound_id: str, *, name: str, description: str, tags: str,
                 source: str, gain_db: float) -> None:
    if not get_sound(conn, sound_id):
        raise SoundError("no such sound")
    conn.execute("UPDATE sounds SET name = ?, description = ?, tags = ?, source = ?, gain_db = ? WHERE id = ?",
                 (name.strip()[:80] or sound_id, description.strip()[:500], _tags(tags), source.strip()[:300],
                  round(min(max(gain_db, -30.0), 10.0), 1), sound_id))


def delete_sound(cfg: Config, conn: sqlite3.Connection, sound_id: str) -> None:
    """Edits that used it keep their markers; rendering skips a sound that's gone."""
    s = get_sound(conn, sound_id)
    if not s:
        return
    p = sound_path(cfg, s)
    if p.parent == sounds_dir(cfg) and p.is_file():
        p.unlink()
    conn.execute("DELETE FROM sounds WHERE id = ?", (sound_id,))


def valid_id(sound_id: str) -> bool:
    return bool(_ID.match(sound_id or ""))


# ── starter pack: known files from a free sound site, with descriptions ready ──

_COPY = re.compile(r"\s*[\(（]\d+[\)）]$")     # "tin1 (1).mp3", "tin1(2).mp3": browser duplicates


def _stem(filename: str) -> str:
    return _COPY.sub("", Path(filename).stem).strip().lower()


def starter_pack() -> dict[str, Any]:
    import json
    return json.loads((Path(__file__).parent / "starter_sounds.json").read_text(encoding="utf-8"))


def starter_for(filename: str, size: int | None = None) -> dict[str, Any] | None:
    """The starter-pack entry for a downloaded file, with the pack's source.

    A file is recognised by the site's file id ("tin1.mp3") or by the name the site saves it
    as ("チーン1.mp3"). Two sounds save under the same name (ドーン), so the size picks one.
    """
    pack = starter_pack()
    stem = _stem(filename)
    hits = ([x for x in pack["sounds"] if x["file"] == stem]
            or [x for x in pack["sounds"] if _stem(x.get("saved_as", "") + ".mp3") == stem])
    if len(hits) > 1:
        hits = [x for x in hits if x.get("bytes") == size]
    return hits[0] | {"source": pack["source"]} if len(hits) == 1 else None


def starter_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """The pack, with which of its sounds are already in the library."""
    pack = starter_pack()
    have = {_stem(r["original"] or "") for r in conn.execute("SELECT original FROM sounds")}
    for x in pack["sounds"]:
        x["have"] = x["file"] in have
    pack["have"] = sum(x["have"] for x in pack["sounds"])
    return pack


def already_imported(conn: sqlite3.Connection, entry: dict[str, Any]) -> bool:
    """Whether a starter-pack sound is in the library (it's stored under its file id)."""
    return any(_stem(r["original"] or "") == entry["file"] for r in conn.execute("SELECT original FROM sounds"))
