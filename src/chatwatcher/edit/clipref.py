"""A downloaded clip and its saved edit rows: ClipRef, the file names and limits every
part of the editor shares, and the small readers (the stored row, its effects and
segments in clip time, words.json, moment.json, the latest analysis)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EDIT_FILE = "edit.mp4"
SRT_FILE = "edit.srt"
SHORT_FILE = "short.mp4"
SHORT_SRT = "short.srt"
KINDS = ("long", "short")
TABLE = {"long": "edits", "short": "shorts"}
STAGE = {"long": "render", "short": "short"}           # the job that renders each kind
WAVEFORM_FILE = "waveform.json"
MIN_SEGMENT_S = 0.2

Segment = list[float]          # [start, end, gain_db]


class EditError(Exception):
    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind       # not_clipped | invalid | error


@dataclass
class ClipRef:
    moment_id: int
    path: Path
    folder: Path
    zero_s: float              # stream time at clip time 0
    duration_s: float
    fps: float
    has_audio: bool
    width: int = 0
    height: int = 0
    voices: dict[str, dict[str, str]] = field(default_factory=dict)   # names and colours (subs.speakers)

    @property
    def edit_path(self) -> Path:
        return self.folder / EDIT_FILE

    @property
    def srt_path(self) -> Path:
        return self.folder / SRT_FILE

    def out_path(self, kind: str = "long") -> Path:
        return self.folder / (SHORT_FILE if kind == "short" else EDIT_FILE)

    def srt_for(self, kind: str = "long") -> Path:
        return self.folder / (SHORT_SRT if kind == "short" else SRT_FILE)


def check_kind(kind: Any) -> str:
    if kind not in KINDS:
        raise EditError("kind must be long or short.", "invalid")
    return kind


def clip_ref(conn: sqlite3.Connection, moment_pk: int) -> ClipRef:
    c = conn.execute("SELECT * FROM clips WHERE moment_id = ?", (moment_pk,)).fetchone()
    if c is None:
        raise EditError("This moment isn't downloaded yet.", "not_clipped")
    path = Path(c["path"])
    if not path.is_file():
        raise EditError("The clip video was deleted to free space. Download it again to edit it.", "not_clipped")
    from chatwatcher.subs import speakers
    return ClipRef(moment_pk, path, Path(c["folder"] or path.parent), float(c["zero_s"] or 0),
                   float(c["duration_s"] or 0), float(c["fps"] or 30), bool(c["acodec"]),
                   int(c["width"] or 0), int(c["height"] or 0), speakers.get(conn, moment_pk))


# ── reading ──────────────────────────────────────────────────────────────────

def latest_analysis(conn: sqlite3.Connection, moment_pk: int) -> dict[str, Any]:
    row = conn.execute("SELECT analysis_json FROM analyses WHERE moment_id = ? ORDER BY id DESC LIMIT 1",
                       (moment_pk,)).fetchone()
    try:
        return json.loads(row["analysis_json"]) if row else {}
    except json.JSONDecodeError:
        return {}


def whole(clip: ClipRef) -> list[Segment]:
    """The cut an unedited clip starts from: all of it (✨ AI edit makes the real one)."""
    return [[0.0, round(clip.duration_s, 3), 0.0]]


def stored(conn: sqlite3.Connection, moment_pk: int, kind: str = "long") -> sqlite3.Row | None:
    return conn.execute(f"SELECT * FROM {TABLE[check_kind(kind)]} WHERE moment_id = ?", (moment_pk,)).fetchone()


def meta(clip: ClipRef) -> dict[str, Any]:
    p = clip.folder / "moment.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def size(clip: ClipRef) -> tuple[int, int]:
    if clip.width and clip.height:
        return clip.width, clip.height
    from chatwatcher.clip.media import probe
    pr = probe(clip.path)
    return pr.width or 1920, pr.height or 1080


def effects_to_clip(stream_effects: list[dict[str, Any]], clip: ClipRef) -> list[dict[str, Any]]:
    return [fx | {"t": round(fx["t"] - clip.zero_s, 3)} for fx in stream_effects
            if 0 <= fx["t"] - clip.zero_s <= clip.duration_s]


def effects(row: sqlite3.Row | None) -> list[dict[str, Any]]:
    try:
        return json.loads(row["effects_json"] or "[]") if row else []
    except json.JSONDecodeError:
        return []


def to_clip(stream_segments: list[Segment], clip: ClipRef) -> list[Segment]:
    """Stream time → clip time, clamped to the clip; segments now outside it are dropped."""
    out = []
    for s, e, g in stream_segments:
        s, e = max(s - clip.zero_s, 0.0), min(e - clip.zero_s, clip.duration_s)
        if e - s >= MIN_SEGMENT_S:
            out.append([round(s, 3), round(e, 3), g])
    return out


def words(clip: ClipRef) -> dict[str, Any] | None:
    """words.json (aligned on the audio when it was transcribed: subs.align)."""
    from chatwatcher.subs.transcribe import WORDS_FILE
    p = clip.folder / WORDS_FILE
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    except (OSError, json.JSONDecodeError):
        return None
