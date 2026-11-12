"""Your cut of one clip, kept in the `edits` table.

Segments are stored in stream time, so they stay right when the clip is downloaded again
with slightly different edges; the browser works in clip time (seconds into clip.mp4).
Each segment is [start, end, gain_db], in the order they play. Effects are tied to a
moment of the clip, not of the edit, so they move with the content when you trim or
reorder. Every effect has {"id", "type", "t", "by": "claude" | "you", "why"}, plus:
    sfx    "sound", "gain", "duck"           a sound effect
    punch  "d", "region", "zoom"             instant zoom on a region (see edit.regions)
    push   "d", "region", "zoom"             slow zoom towards a region
    shake  "d", "strength" (1-3)             camera shake
    flash  "d", "color"                      white / red / black flash
    frame  "d", "region"                     a Short shows the region full-screen
    transition "style", "d"                  how the cut into the segment starting at t looks
                                             (motion.TRANSITIONS: flash, fade, zoom, whip)

A clip can have two edits: the long one (16:9, table `edits`, edit.mp4) and a Short (9:16,
table `shorts`, short.mp4) with its own cut, effects and subtitles plus a layout (see
edit.layout). Functions take kind="long" | "short".
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from chatwatcher.config import Config
from chatwatcher.edit import captions as cap
from chatwatcher.edit import layout as lay
from chatwatcher.edit import overlays as ovl
from chatwatcher.edit import spots as spt
from chatwatcher.edit import texts as txt
from chatwatcher.edit import motion, regions
from chatwatcher.edit.audio import analyse

EDIT_FILE = "edit.mp4"
SRT_FILE = "edit.srt"
SHORT_FILE = "short.mp4"
SHORT_SRT = "short.srt"
KINDS = ("long", "short")
_TABLE = {"long": "edits", "short": "shorts"}
STAGE = {"long": "render", "short": "short"}           # the job that renders each kind
WAVEFORM_FILE = "waveform.json"
MAX_SEGMENTS = 80           # the AI edit cuts every pause, so a clip can have many
MAX_EFFECTS = 120
MIN_SEGMENT_S = 0.2
GAIN_RANGE = (-30.0, 10.0)
DUCK_RANGE = (-30.0, 0.0)

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

def _latest_analysis(conn: sqlite3.Connection, moment_pk: int) -> dict[str, Any]:
    row = conn.execute("SELECT analysis_json FROM analyses WHERE moment_id = ? ORDER BY id DESC LIMIT 1",
                       (moment_pk,)).fetchone()
    try:
        return json.loads(row["analysis_json"]) if row else {}
    except json.JSONDecodeError:
        return {}


def whole(clip: ClipRef) -> list[Segment]:
    """The cut an unedited clip starts from: all of it (✨ AI edit makes the real one)."""
    return [[0.0, round(clip.duration_s, 3), 0.0]]


_LINE = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)$")


def transcript_lines(folder: Path) -> list[dict[str, Any]]:
    p = folder / "transcript.txt"
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _LINE.match(line)
        if m and m.group(3).strip():
            out.append({"t": int(m.group(1)) * 60 + float(m.group(2)), "text": m.group(3).strip()})
    return out


def _stored(conn: sqlite3.Connection, moment_pk: int, kind: str = "long") -> sqlite3.Row | None:
    return conn.execute(f"SELECT * FROM {_TABLE[check_kind(kind)]} WHERE moment_id = ?", (moment_pk,)).fetchone()


def _meta(clip: ClipRef) -> dict[str, Any]:
    p = clip.folder / "moment.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _size(clip: ClipRef) -> tuple[int, int]:
    if clip.width and clip.height:
        return clip.width, clip.height
    from chatwatcher.clip.media import probe
    pr = probe(clip.path)
    return pr.width or 1920, pr.height or 1080


def current_layout(conn: sqlite3.Connection, moment_pk: int, clip: ClipRef,
                   row: sqlite3.Row | None = None) -> dict[str, Any]:
    """The Short's saved layout, or a first one from the channel's regions."""
    boxes = regions.get(conn, regions.scope_for(conn, moment_pk)[0])
    first = lay.default(boxes, _meta(clip).get("stream_type"))
    saved = None
    if row is not None and row["layout_json"]:
        try:
            saved = json.loads(row["layout_json"])
        except json.JSONDecodeError:
            saved = None
    return lay.clean(saved, first) if saved else first


def _short_regions(conn: sqlite3.Connection, moment_pk: int, effects: list[dict[str, Any]],
                   layout: dict[str, Any]) -> dict[str, dict[str, float]]:
    """The boxes a Short depends on: those its zooms aim at, and its stack panels."""
    have = regions.get(conn, regions.scope_for(conn, moment_pk)[0])
    names = {fx.get("region") for fx in effects if fx.get("type") in (*motion.ZOOMS, lay.FRAME) and fx.get("region")}
    names |= {fx.get("at") for fx in effects if fx.get("type") in (ovl.OVERLAY, txt.TEXT, cap.CAPTION)
              and fx.get("at") and fx.get("at") not in (*ovl.PLACES, *cap.PLACES)}
    names |= lay.used_regions(layout)
    return {n: have[n] for n in sorted(names) if n in have}


def _effects_to_clip(stream_effects: list[dict[str, Any]], clip: ClipRef) -> list[dict[str, Any]]:
    return [fx | {"t": round(fx["t"] - clip.zero_s, 3)} for fx in stream_effects
            if 0 <= fx["t"] - clip.zero_s <= clip.duration_s]


def _effects(row: sqlite3.Row | None) -> list[dict[str, Any]]:
    try:
        return json.loads(row["effects_json"] or "[]") if row else []
    except json.JSONDecodeError:
        return []


def _to_clip(stream_segments: list[Segment], clip: ClipRef) -> list[Segment]:
    """Stream time → clip time, clamped to the clip; segments now outside it are dropped."""
    out = []
    for s, e, g in stream_segments:
        s, e = max(s - clip.zero_s, 0.0), min(e - clip.zero_s, clip.duration_s)
        if e - s >= MIN_SEGMENT_S:
            out.append([round(s, 3), round(e, 3), g])
    return out


def load(cfg: Config, conn: sqlite3.Connection, moment_pk: int, kind: str = "long") -> dict[str, Any]:
    """Everything the editor needs, in clip time. A Short that was never saved starts from
    the whole clip, with the long edit's effects and subtitle fixes."""
    check_kind(kind)
    clip = clip_ref(conn, moment_pk)
    wf = analyse(clip.path, clip.folder / WAVEFORM_FILE)
    a = _latest_analysis(conn, moment_pk)
    row = _stored(conn, moment_pk, kind)
    base = row if row is not None or kind == "long" else _stored(conn, moment_pk, "long")
    segments = _to_clip(json.loads(row["segments_json"]), clip) if row else []
    source = row["source"] if row and segments else None
    if not segments:
        source, segments = "whole", whole(clip)
    shift = float((a.get("_meta") or {}).get("zero_s", clip.zero_s)) - clip.zero_s
    beats = [{"start": round(float(b.get("start", 0)) + shift, 2), "end": round(float(b.get("end", 0)) + shift, 2),
              "beat": b.get("beat", "")}
             for b in a.get("storyboard") or []]
    from chatwatcher.edit.sounds import list_sounds
    sounds = [{"id": x["id"], "name": x["name"], "description": x["description"], "length": x["duration_s"],
               "gain": x["gain_db"]} for x in list_sounds(conn)]
    scope, scope_label = regions.scope_for(conn, moment_pk)
    out = {
        "kind": kind, "duration": clip.duration_s, "fps": clip.fps, **wf.to_json(),
        "segments": segments, "source": source, "saved": bool(row),
        "effects": _effects_to_clip(_effects(base), clip), "sounds": sounds,
        "overlays": [{"id": o["id"], "name": o["name"], "description": o["description"], "length": o["duration_s"],
                      "aspect": o["width"] / o["height"], "gain": o["gain_db"], "sound": bool(o["sound"])}
                     for o in ovl.list_overlays(conn)],
        "overlay_info": {"places": ovl.PLACES, "size": ovl.SIZE_RANGE, "offset": ovl.OFFSET_RANGE, "out": list(_size(clip)) if kind == "long"
                         else [lay.OUT_W, lay.OUT_H]},
        "regions": regions.get(conn, scope), "region_scope": scope_label,
        "text_info": {"styles": txt.STYLES, "colors": txt.COLORS, "default_color": txt.DEFAULT_COLOR,
                      "size": txt.SIZE_RANGE, "line": txt.LINE, "label_line": txt.LABEL_LINE, "max": txt.MAX_CHARS, "tilt": txt.STAMP_TILT,
                      "intro_ms": txt.INTRO_MS, "outro_ms": txt.OUTRO_MS,
                      "default_size": {st: txt.default_size(st, kind == "short") for st in txt.STYLES}},
        "spot_info": {"styles": spt.STYLES, "colors": {k: "%02X%02X%02X" % v for k, v in spt.COLORS.items()},
                      "default_color": spt.DEFAULT_COLOR, "dim": spt.DIM, "feather": spt.FEATHER, "ring": spt.RING,
                      "fade": [spt.FADE_IN, spt.FADE_OUT], "min": spt.MIN_SIZE},
        "visual": {"types": motion.TYPES | {lay.FRAME: lay.FRAME_LIMITS, ovl.OVERLAY: (1.0, 0.1, ovl.MAX_SECONDS),
                                            txt.TEXT: txt.D_LIMITS, spt.SPOT: spt.D_LIMITS,
                                            cap.CAPTION: cap.D_LIMITS},
                   "transitions": motion.TRANSITIONS,
                   "transition_d": motion.TRANSITION_D, "zoom_range": motion.ZOOM_RANGE, "shake": motion.SHAKE_AMP,
                   "flash": motion.FLASH, "margin": motion.MARGIN, "waves": [motion.WAVES_X, motion.WAVES_Y]},
        "beats": beats, "lines": transcript_lines(clip.folder),
        "snap_s": cfg.edit.snap_s, "render": render_state(conn, moment_pk, clip, cfg=cfg, kind=kind),
        "transcript": transcript_state(cfg, conn, moment_pk, clip),
        "other": {k: render_state(conn, moment_pk, clip, cfg=cfg, kind=k)["state"] for k in KINDS if k != kind},
        **subtitles(cfg, conn, moment_pk, clip, kind, row=base),
    }
    if kind == "short":
        sw, sh = _size(clip)
        out |= {"layout": current_layout(conn, moment_pk, clip, row),
                "layout_info": {"types": lay.TYPES, "out": [lay.OUT_W, lay.OUT_H], "scale": lay.SCALE_RANGE,
                                "split": lay.SPLIT_RANGE, "max_title": lay.MAX_TITLE, "dark": lay.BG_DARK,
                                "blur_down": lay.BLUR_DOWN, "src": [sw, sh],
                                "frame": {"margin": lay.FRAME_MARGIN, "wide": lay.FRAME_WIDE,
                                          "limits": lay.FRAME_LIMITS}},
                "titles": [t for t in (a.get("titles") or []) if isinstance(t, str)][:3]}
    return out


def _words(clip: ClipRef) -> dict[str, Any] | None:
    """words.json (aligned on the audio when it was transcribed: subs.align)."""
    from chatwatcher.subs.transcribe import WORDS_FILE
    p = clip.folder / WORDS_FILE
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    except (OSError, json.JSONDecodeError):
        return None


def _sub_default(cfg: Config | None, clip: ClipRef) -> dict[str, Any]:
    from chatwatcher.config import SubtitlesConfig
    sc = cfg.subtitles if cfg else SubtitlesConfig()
    meta = clip.folder / "moment.json"
    try:
        karaoke = json.loads(meta.read_text(encoding="utf-8")).get("stream_type") == "karaoke" if meta.is_file() else False
    except (OSError, json.JSONDecodeError):
        karaoke = False
    return {"on": sc.on and not karaoke, "size": sc.size, "position": sc.position, "mode": "lines", "look": "classic",
            "emphasis": []}


def current_subs(cfg: Config | None, row: sqlite3.Row | None, clip: ClipRef,
                 kind: str = "long") -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    """(cues in clip time, where they come from: saved | auto | none, style). Until you edit
    them, cues are built fresh from Whisper's words, so transcribing again updates them
    (shorter ones for a Short). Cues with a voice carry its colour and name."""
    from chatwatcher.subs import lines, speakers
    style = lines.clean_style(json.loads(row["subs_style_json"]) if row and row["subs_style_json"] else {},
                              _sub_default(cfg, clip))
    if row and row["subs_json"] is not None:
        cues = [c | {"t0": round(c["t0"] - clip.zero_s, 3), "t1": round(c["t1"] - clip.zero_s, 3)}
                for c in json.loads(row["subs_json"])]
        cues = [c for c in cues if c["t1"] > 0 and c["t0"] < clip.duration_s]
        return speakers.paint(cues, clip.voices), "saved", style
    words = _words(clip)
    return (speakers.paint(auto_cues(words, kind, style), clip.voices), "auto", style) if words else ([], "none", style)


def auto_cues(words: dict[str, Any] | None, kind: str, style: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Cues built from Whisper's words in the style's mode, its emphasis words highlighted."""
    from chatwatcher.subs import lines
    if not words:
        return []
    return lines.emphasise(lines.build(words, _max_cue(kind, style)), (style or {}).get("emphasis") or [])


def _max_cue(kind: str, style: dict[str, Any] | None = None) -> int:
    from chatwatcher.subs import lines
    if (style or {}).get("mode") == "words":
        return lines.WORD_CUE
    return lines.SHORT_CUE if kind == "short" else lines.MAX_CUE


def subtitles(cfg: Config, conn: sqlite3.Connection, moment_pk: int, clip: ClipRef, kind: str = "long",
              row: sqlite3.Row | None = None) -> dict[str, Any]:
    """The editor's subtitle data: the cues, what Whisper's words would give (to start over
    from), the word times (edges snap to them) and the language. row: where the cues come
    from (default: this kind's saved edit)."""
    from chatwatcher.subs import lines
    cues, source, style = current_subs(cfg, row if row is not None else _stored(conn, moment_pk, kind), clip, kind)
    words = _words(clip)
    w, h = (lay.OUT_W, lay.OUT_H) if kind == "short" else _size(clip)
    from chatwatcher.subs import names
    from chatwatcher.subs import speakers
    scope = regions.scope_for(conn, moment_pk)[0]
    count = max([int((words or {}).get("voices") or 0)] + [int(k) + 1 for k in clip.voices])
    master = speakers.vtubers(conn)
    return {"subs": cues, "subs_source": source, "subs_style": style,
            "voices": {"count": count,
                       "map": {str(k): clip.voices.get(str(k)) or {"name": "", "color": speakers.default_color(k),
                                                                    "text": speakers.readable(speakers.default_color(k))}
                               for k in range(count)},
                       "people": {k: v["color"] for k, v in master.items()},
                       "names": [v["name"] for v in master.values() if v["status"] != "Retirement"],
                       "palette": speakers.PALETTE, "max": speakers.MAX_VOICES},
            "names": names.get(conn, scope), "output_language": cfg.claude.output_language,
            "subs_auto": auto_cues(words, kind, style),
            "subs_auto_by_mode": {m: auto_cues(words, kind, style | {"mode": m}) for m in lines.MODES} if words else {},
            "sub_words": [[round(a, 3), round(b, 3)] for a, b, _ in lines.words_of(words)] if words else [],
            "sub_language": (words or {}).get("language"),
            "sub_layout": {**lines.frame_rules(w, h), "sizes": lines.SIZES, "highlight": lines.HIGHLIGHT,
                           "font": cfg.subtitles.font, "captions": cap.info()}}


def transcript_state(cfg: Config, conn: sqlite3.Connection, moment_pk: int, clip: ClipRef) -> dict[str, Any]:
    """Where the clip's transcript comes from (whisper | youtube | none), and the
    transcribe job: none | queued | running | error."""
    from chatwatcher.subs import transcribe as tr
    whisper = tr.info(clip.folder)
    source = "whisper" if whisper else ("youtube" if (clip.folder / tr.TRANSCRIPT_FILE).is_file()
                                        and (clip.folder / tr.TRANSCRIPT_FILE).stat().st_size else "none")
    out: dict[str, Any] = {"source": source, "whisper": whisper, "state": "none",
                           "can": cfg.transcribe.enabled and tr.available(), "model": cfg.transcribe.model}
    job = conn.execute("SELECT status, error, message FROM jobs WHERE moment_id = ? AND stage = 'transcribe'",
                       (moment_pk,)).fetchone()
    if not (job and job["status"] in ("pending", "running")) and not whisper:
        job = conn.execute("""SELECT j.status, j.error, j.message FROM jobs j JOIN moments m ON m.video_id = j.video_id
                              WHERE m.id = ? AND j.stage = 'transcribe' AND j.moment_id IS NULL""",
                           (moment_pk,)).fetchone() or job       # the whole stream's, still to reach this clip
    if job and job["status"] in ("pending", "running"):
        out["state"] = "queued" if job["status"] == "pending" else "running"
    elif job and job["status"] == "error":
        out |= {"state": "error", "error": (job["error"] or job["message"] or "")[:400]}
    return out


def render_state(conn: sqlite3.Connection, moment_pk: int, clip: ClipRef | None = None,
                 cfg: Config | None = None, kind: str = "long") -> dict[str, Any]:
    """none | queued | running | done | stale (changed since the last render) | error."""
    check_kind(kind)
    if clip is None:
        try:
            clip = clip_ref(conn, moment_pk)
        except EditError:
            return {"state": "none"}
    job = conn.execute("SELECT status, error FROM jobs WHERE moment_id = ? AND stage = ?",
                       (moment_pk, STAGE[kind])).fetchone()
    row = _stored(conn, moment_pk, kind)
    out: dict[str, Any] = {"state": "none"}
    f = clip.out_path(kind)
    rendered = json.loads(row["rendered_json"]) if row and row["rendered_json"] else None
    if f.is_file() and rendered:
        same = (rendered.get("segments") == json.loads(row["segments_json"])
                and rendered.get("effects", []) == _effects(row)
                and _subs_rendered(cfg, row, clip, kind) == (rendered.get("subs", []), rendered.get("subs_style")))
        if kind == "short":
            layout = current_layout(conn, moment_pk, clip, row)
            same = (same and rendered.get("layout") == layout
                    and rendered.get("regions", {}) == _short_regions(conn, moment_pk, _effects(row), layout))
        else:
            same = same and rendered.get("regions", {}) == _used_regions(conn, moment_pk, _effects(row))
            if same and cfg is not None:
                from chatwatcher.brand.service import edit_intro_key
                same = rendered.get("intro") == edit_intro_key(cfg, conn, moment_pk, clip)
        q = "?kind=short&" if kind == "short" else "?"
        if clip.srt_for(kind).is_file():
            out["srt"] = f"/media/edit-srt/{moment_pk}{q}v={int(clip.srt_for(kind).stat().st_mtime)}"
        out |= {"url": f"/media/edit/{moment_pk}{q}v={int(f.stat().st_mtime)}", "size": f.stat().st_size,
                "path": str(f), "length": rendered.get("duration_s"), "at": row["rendered_at"],
                "state": "done" if same else "stale"}
    if job and job["status"] in ("pending", "running"):
        out["state"] = "queued" if job["status"] == "pending" else "running"
        out["progress"] = (row["render_progress"] if row else None) or 0.0
        if job["status"] == "pending":
            busy = conn.execute("SELECT stage FROM jobs WHERE status = 'running' LIMIT 1").fetchone()
            out["behind"] = busy["stage"] if busy else None
    elif job and job["status"] == "error" and out["state"] != "done":
        out |= {"state": "error", "error": (job["error"] or "")[:600]}
    return out


# ── writing ──────────────────────────────────────────────────────────────────

def clean_segments(raw: Any, duration: float) -> list[Segment]:
    """Check segments from the browser (clip time). Raises EditError(kind='invalid')."""
    if not isinstance(raw, list) or not raw:
        raise EditError("The edit needs at least one segment.", "invalid")
    if len(raw) > MAX_SEGMENTS:
        raise EditError(f"At most {MAX_SEGMENTS} segments.", "invalid")
    out: list[Segment] = []
    for i, seg in enumerate(raw, 1):
        try:
            s, e = float(seg[0]), float(seg[1])
            g = float(seg[2]) if len(seg) > 2 else 0.0
        except (TypeError, ValueError, IndexError):
            raise EditError(f"Segment {i} isn't [start, end, gain].", "invalid")
        if not all(math.isfinite(v) for v in (s, e, g)):
            raise EditError(f"Segment {i} has a bad number.", "invalid")
        s, e = max(0.0, s), min(e, duration)
        if e - s < MIN_SEGMENT_S:
            raise EditError(f"Segment {i} is shorter than {MIN_SEGMENT_S:g} s.", "invalid")
        g = round(min(max(g, GAIN_RANGE[0]), GAIN_RANGE[1]), 1)
        out.append([round(s, 3), round(e, 3), g])
    if sum(e - s for s, e, _ in out) < 1:
        raise EditError("The edit must be at least 1 second long.", "invalid")
    return out


def clean_effects(raw: Any, duration: float, sounds: set[str],
                  overlays: set[str] | None = None) -> list[dict[str, Any]]:
    """Check effects from the browser or from Claude (clip time). Raises EditError(kind='invalid').
    overlays: the overlay library's ids (None: any id)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise EditError("Effects must be a list.", "invalid")
    if len(raw) > MAX_EFFECTS:
        raise EditError(f"At most {MAX_EFFECTS} effects.", "invalid")
    out, ids = [], set()
    for i, fx in enumerate(raw, 1):
        kind = fx.get("type", "sfx") if isinstance(fx, dict) else None
        if kind != "sfx" and kind not in motion.TYPES and kind not in (lay.FRAME, "transition", ovl.OVERLAY, txt.TEXT,
                                                                         spt.SPOT, cap.CAPTION):
            raise EditError(f"Effect {i} isn't a sound, zoom, shake, flash, overlay, text, spotlight or caption style.",
                            "invalid")
        try:
            t = float(fx["t"])
        except (KeyError, TypeError, ValueError):
            raise EditError(f"Effect {i} needs a time.", "invalid")
        if not math.isfinite(t) or not 0 <= t <= duration:
            raise EditError(f"Effect {i} is outside the clip.", "invalid")
        fid = re.sub(r"[^A-Za-z0-9_-]", "", str(fx.get("id") or ""))[:24] or f"fx{i}"
        while fid in ids:
            fid += "x"
        ids.add(fid)
        e: dict[str, Any] = {"id": fid, "type": kind, "t": round(t, 3)}
        if kind == "sfx":
            try:
                gain, duck = float(fx.get("gain", 0)), float(fx.get("duck", 0))
            except (TypeError, ValueError):
                raise EditError(f"Effect {i} has a bad volume.", "invalid")
            if not (math.isfinite(gain) and math.isfinite(duck)):
                raise EditError(f"Effect {i} has a bad volume.", "invalid")
            sound = str(fx.get("sound") or "")
            if sound not in sounds:
                raise EditError(f"Effect {i} uses a sound that isn't in your library ({sound or 'none'}).", "invalid")
            e |= {"sound": sound, "gain": round(min(max(gain, GAIN_RANGE[0]), GAIN_RANGE[1]), 1),
                  "duck": round(min(max(duck, DUCK_RANGE[0]), DUCK_RANGE[1]), 1)}
        elif kind == "transition":               # how the cut into the segment starting at t looks
            style = str(fx.get("style") or "")
            if style not in motion.TRANSITIONS:
                raise EditError(f"Effect {i} is an unknown transition ({style or 'none'}).", "invalid")
            try:
                d = float(fx.get("d") or motion.TRANSITIONS[style][0])
            except (TypeError, ValueError):
                raise EditError(f"Effect {i} has a bad length.", "invalid")
            if not math.isfinite(d):
                raise EditError(f"Effect {i} has a bad length.", "invalid")
            e |= {"style": style, "d": round(min(max(d, motion.TRANSITION_D[0]), motion.TRANSITION_D[1]), 3)}
        elif kind == ovl.OVERLAY:                # a green-screen clip over the picture, with its sound
            oid = str(fx.get("overlay") or "")
            if not ovl.valid_id(oid) or (overlays is not None and oid not in overlays):
                raise EditError(f"Effect {i} uses an overlay that isn't in your library ({oid or 'none'}).", "invalid")
            try:
                d, size, gain = float(fx.get("d", 1.0)), float(fx.get("size", 1.0)), float(fx.get("gain", 0))
                dx, dy = float(fx.get("dx") or 0), float(fx.get("dy") or 0)
            except (TypeError, ValueError):
                raise EditError(f"Effect {i} has a bad length, size, position or volume.", "invalid")
            if not all(math.isfinite(v) for v in (d, size, gain, dx, dy)):
                raise EditError(f"Effect {i} has a bad length, size, position or volume.", "invalid")
            at = str(fx.get("at") or "full")
            at = at if at in ovl.PLACES else (regions.normal_name(at) or "full")
            lo, hi = ovl.OFFSET_RANGE
            e |= {"overlay": oid, "d": round(min(max(d, 0.1), ovl.MAX_SECONDS), 3), "at": at,
                  "dx": round(min(max(dx, lo), hi), 4), "dy": round(min(max(dy, lo), hi), 4),
                  "size": round(min(max(size, ovl.SIZE_RANGE[0]), ovl.SIZE_RANGE[1]), 2),
                  "gain": round(min(max(gain, ovl.GAIN_RANGE[0]), ovl.GAIN_RANGE[1]), 1),
                  "mode": fx.get("mode") if fx.get("mode") in ovl.MODES else "both"}
        elif kind == spt.SPOT:                   # a spotlight on something in the clip's frame
            default, shortest, longest = spt.D_LIMITS
            try:
                d = float(fx.get("d", default))
                x, y, w, h = (float(fx[k]) for k in ("x", "y", "w", "h"))
            except (KeyError, TypeError, ValueError):
                raise EditError(f"Effect {i} is a spotlight without a place (x, y, w, h).", "invalid")
            if not all(math.isfinite(v) for v in (d, x, y, w, h)):
                raise EditError(f"Effect {i} has a bad length or place.", "invalid")
            w, h = min(max(w, spt.MIN_SIZE), 1.5), min(max(h, spt.MIN_SIZE), 1.5)
            style = fx.get("style") if fx.get("style") in spt.STYLES else "spotlight"
            e |= {"d": round(min(max(d, shortest), longest), 3), "style": style,
                  "color": fx.get("color") if fx.get("color") in spt.COLORS else spt.DEFAULT_COLOR[style],
                  "x": round(min(max(x, -w / 2), 1 - w / 2), 4), "y": round(min(max(y, -h / 2), 1 - h / 2), 4),
                  "w": round(w, 4), "h": round(h, 4)}
        elif kind == txt.TEXT:                   # a text callout over the picture
            text = txt.clean_text(fx.get("text") or "")
            if not text:
                raise EditError(f"Effect {i} is a text with nothing to say.", "invalid")
            default, shortest, longest = txt.D_LIMITS
            try:
                d, dx, dy = float(fx.get("d", default)), float(fx.get("dx") or 0), float(fx.get("dy") or 0)
                size = None if fx.get("size") is None else float(fx["size"])
            except (TypeError, ValueError):
                raise EditError(f"Effect {i} has a bad length, size or position.", "invalid")
            if not all(math.isfinite(v) for v in (d, dx, dy, size if size is not None else 0)):
                raise EditError(f"Effect {i} has a bad length, size or position.", "invalid")
            style = fx.get("style") if fx.get("style") in txt.STYLES else "pop"
            at = str(fx.get("at") or "middle")
            at = at if at in ovl.PLACES else (regions.normal_name(at) or "middle")
            lo, hi = ovl.OFFSET_RANGE
            e |= {"text": text, "style": style, "d": round(min(max(d, shortest), longest), 3),
                  "color": fx.get("color") if fx.get("color") in txt.COLORS else txt.DEFAULT_COLOR[style],
                  "at": at, "dx": round(min(max(dx, lo), hi), 4), "dy": round(min(max(dy, lo), hi), 4)}
            if size is not None:
                e["size"] = round(min(max(size, txt.SIZE_RANGE[0]), txt.SIZE_RANGE[1]), 3)
        elif kind == cap.CAPTION:                # how the captions starting in [t, t + d) look
            default, shortest, longest = cap.D_LIMITS
            try:
                d = float(fx.get("d", default))
            except (TypeError, ValueError):
                raise EditError(f"Effect {i} has a bad length.", "invalid")
            if not math.isfinite(d):
                raise EditError(f"Effect {i} has a bad length.", "invalid")
            at = str(fx.get("at") or "")
            at = at if at in ("", *cap.PLACES) else (regions.normal_name(at) or "")
            e |= {"d": round(min(max(d, shortest), longest), 3), "fx": fx.get("fx") if fx.get("fx") in cap.FX else "pop",
                  "font": fx.get("font") if fx.get("font") in cap.FONTS else "", "at": at}
        elif kind == lay.FRAME:                  # a Short shows this region full-screen for d seconds
            default, shortest, longest = lay.FRAME_LIMITS
            try:
                d = float(fx.get("d", default))
            except (TypeError, ValueError):
                raise EditError(f"Effect {i} has a bad length.", "invalid")
            if not math.isfinite(d):
                raise EditError(f"Effect {i} has a bad length.", "invalid")
            e |= {"d": round(min(max(d, shortest), longest), 3), "region": regions.normal_name(fx.get("region"))}
        else:
            default, shortest, longest = motion.TYPES[kind]
            try:
                d = float(fx.get("d", default))
                zoom = float(fx.get("zoom", 1.5 if kind == "punch" else 1.25))
                strength = int(fx.get("strength", 2))
            except (TypeError, ValueError):
                raise EditError(f"Effect {i} has a bad setting.", "invalid")
            if not (math.isfinite(d) and math.isfinite(zoom)):
                raise EditError(f"Effect {i} has a bad setting.", "invalid")
            e["d"] = round(min(max(d, shortest), longest), 3)
            if kind in motion.ZOOMS:
                e |= {"region": regions.normal_name(fx.get("region")),
                      "zoom": round(min(max(zoom, motion.ZOOM_RANGE[0]), motion.ZOOM_RANGE[1]), 2)}
            elif kind == "shake":
                e["strength"] = min(max(strength, 1), 3)
            else:
                color = str(fx.get("color") or "white")
                e["color"] = color if color in motion.FLASH else "white"
        e |= {"by": "claude" if fx.get("by") == "claude" else "you", "why": str(fx.get("why") or "")[:300]}
        out.append(e)
    return sorted(out, key=lambda fx: fx["t"])


def _used_regions(conn: sqlite3.Connection, moment_pk: int,
                  effects: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """The boxes of the regions these effects aim at (as they are now)."""
    names = {fx.get("region") for fx in effects if fx.get("type") in motion.ZOOMS and fx.get("region")}
    names |= {fx.get("at") for fx in effects if fx.get("type") in (ovl.OVERLAY, txt.TEXT, cap.CAPTION)
              and fx.get("at") and fx.get("at") not in (*ovl.PLACES, *cap.PLACES)}
    if not names:
        return {}
    have = regions.get(conn, regions.scope_for(conn, moment_pk)[0])
    return {n: have[n] for n in sorted(names) if n in have}


def visuals(effects: list[dict[str, Any]], boxes: dict[str, dict[str, float]]) -> tuple[list[motion.Visual], set[str]]:
    """Picture effects ready for the renderer, and the region names that don't exist
    (those zooms aim at the centre)."""
    out, missing = [], set()
    for fx in effects:
        kind = fx.get("type")
        if kind not in motion.TYPES:
            continue
        v = motion.Visual(kind, fx["t"], fx["d"], strength=fx.get("strength", 2), color=fx.get("color", "white"))
        if kind in motion.ZOOMS:
            v.zoom = fx.get("zoom", 1.5)
            box = boxes.get(fx.get("region") or "")
            if box:
                v.cx, v.cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
            elif fx.get("region"):
                missing.add(fx["region"])
        out.append(v)
    return out, missing


def save(cfg: Config, conn: sqlite3.Connection, moment_pk: int, raw: Any, source: str | None = None,
         effects: Any = None, subs: Any = None, subs_style: Any = None, kind: str = "long",
         layout: Any = None) -> list[Segment]:
    """Store the browser's segments (clip time), and its effects unless effects is None
    (a new Short keeps the long edit's then).
    layout: a Short's layout (None = keep it; a new Short gets a first one)."""
    table = _TABLE[check_kind(kind)]
    clip = clip_ref(conn, moment_pk)
    segs = clean_segments(raw, clip.duration_s)
    stream = [[round(s + clip.zero_s, 3), round(e + clip.zero_s, 3), g] for s, e, g in segs]
    existing = _stored(conn, moment_pk, kind)
    if effects is not None:
        known = {r["id"] for r in conn.execute("SELECT id FROM sounds")}
        ovs = {r["id"] for r in conn.execute("SELECT id FROM overlays")}
        fx = [f | {"t": round(f["t"] + clip.zero_s, 3)} for f in clean_effects(effects, clip.duration_s, known, ovs)]
    else:
        fx = _effects(existing if existing is not None or kind == "long" else _stored(conn, moment_pk, "long"))
    conn.execute(
        f"""INSERT INTO {table} (moment_id, segments_json, source, effects_json) VALUES (?, ?, ?, ?)
           ON CONFLICT (moment_id) DO UPDATE SET segments_json = excluded.segments_json,
               effects_json = excluded.effects_json,
               source = COALESCE(excluded.source, {table}.source), updated_at = datetime('now')""",
        (moment_pk, json.dumps(stream), source if source in ("long", "short", "whole") else None,
         json.dumps(fx, ensure_ascii=False)))
    if kind == "short" and (layout is not None or existing is None or not existing["layout_json"]):
        first = current_layout(conn, moment_pk, clip, existing)
        conn.execute("UPDATE shorts SET layout_json = ? WHERE moment_id = ?",
                     (json.dumps(lay.clean(layout, first) if layout is not None else first, ensure_ascii=False),
                      moment_pk))
    if existing is None and kind == "short" and subs is None and subs_style is None:
        long_row = _stored(conn, moment_pk, "long")           # a new Short keeps your subtitle fixes
        if long_row is not None:
            conn.execute("UPDATE shorts SET subs_json = ?, subs_style_json = ? WHERE moment_id = ?",
                         (long_row["subs_json"], long_row["subs_style_json"], moment_pk))
    if subs is not None or subs_style is not None:
        from chatwatcher.subs.lines import SubsError, clean, clean_style
        try:
            if subs == "auto":                       # follow Whisper's words again
                conn.execute(f"UPDATE {table} SET subs_json = NULL WHERE moment_id = ?", (moment_pk,))
            elif subs is not None:
                cues = [c | {"t0": round(c["t0"] + clip.zero_s, 3), "t1": round(c["t1"] + clip.zero_s, 3)}
                        for c in clean(subs, clip.duration_s)]
                conn.execute(f"UPDATE {table} SET subs_json = ? WHERE moment_id = ?",
                             (json.dumps(cues, ensure_ascii=False), moment_pk))
            if subs_style is not None:
                conn.execute(f"UPDATE {table} SET subs_style_json = ? WHERE moment_id = ?",
                             (json.dumps(clean_style(subs_style, _sub_default(cfg, clip))), moment_pk))
        except SubsError as e:
            raise EditError(str(e), "invalid")
    return segs


def _subs_rendered(cfg: Config | None, row: sqlite3.Row, clip: ClipRef,
                   kind: str = "long") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The subtitles a render would burn in now (stream time), and their style: to tell
    whether edit.mp4 / short.mp4 is out of date."""
    cues, _source, style = current_subs(cfg, row, clip, kind)
    return [c | {"t0": round(c["t0"] + clip.zero_s, 3), "t1": round(c["t1"] + clip.zero_s, 3)} for c in cues], style


def short_visuals(effects: list[dict[str, Any]], boxes: dict[str, dict[str, float]],
                  ps: list[lay.Panel]) -> tuple[list[motion.Visual], set[str]]:
    """visuals() for a Short: zooms aim at where their region lands on the 9:16 frame (its
    centre when the region isn't drawn or isn't on screen)."""
    out, missing = visuals(effects, boxes)
    for fx, v in zip([f for f in effects if f.get("type") in motion.TYPES], out):
        if v.type in motion.ZOOMS:
            at = lay.map_point(v.cx, v.cy, ps) if boxes.get(fx.get("region") or "") else None
            v.cx, v.cy = at or (0.5, 0.5)
    return out, missing


def split_at(segs: list[Segment], times: list[float], fps: float) -> list[Segment]:
    """Segments cut (on whole frames, and not into slivers) at these clip times; the parts
    still play back to back."""
    out: list[Segment] = []
    for s, e, g in segs:
        cuts = sorted({round(s + round((t - s) * fps) / fps, 3) for t in times
                       if s + MIN_SEGMENT_S < t < e - MIN_SEGMENT_S})
        edges = [s, *cuts, e]
        out += [[a, b, g] for a, b in zip(edges, edges[1:])]
    return out


def render_moment(cfg: Config, conn: sqlite3.Connection, moment_pk: int, *, kind: str = "long",
                  on_progress: Callable[[float], None] | None = None) -> dict[str, Any]:
    """Render the saved edit to <clip folder>/edit.mp4 (a Short: short.mp4).
    Returns {duration_s, size_bytes, missing, …}."""
    from chatwatcher.edit.render import SoundCue, place, render
    from chatwatcher.edit.sounds import get_sound, sound_path
    check_kind(kind)
    short = kind == "short"
    table = _TABLE[kind]
    clip = clip_ref(conn, moment_pk)
    row = _stored(conn, moment_pk, kind)
    if row is None:
        raise EditError("Nothing to render: no edit saved for this clip." if not short
                        else "Nothing to render: no Short saved for this clip.", "invalid")
    stream = json.loads(row["segments_json"])
    segs = _to_clip(stream, clip)
    if not segs:
        raise EditError("The edit's segments are outside the downloaded clip. Pick them again.", "invalid")
    stream_fx = _effects(row)
    clip_fx = _effects_to_clip(stream_fx, clip)
    cues, _source, sub_style = current_subs(cfg, row, clip, kind)
    graph, layout, title = None, None, ""
    if short:
        layout = current_layout(conn, moment_pk, clip, row)
        boxes = _short_regions(conn, moment_pk, stream_fx, layout)
        sw, sh = _size(clip)
        ps = lay.panels(layout, boxes, sw, sh)
        title = layout["title"]
        # Frame shots: the picture moves to another region for a while, so the cut is split
        # where they start and end, and each part gets its own picture and zoom targets.
        frames = [f for f in clip_fx if f.get("type") == lay.FRAME and lay.region_box(f.get("region") or "", boxes)]
        segs = split_at(segs, [t for f in frames for t in (f["t"], f["t"] + f["d"])], clip.fps)
        graph, pictures, no_region, ov_lists, ov_missing, text_lists, spot_lists = [], [], set(), [], set(), [], []
        cap_lists = []
        for s, e, _g in segs:
            fr = lay.frame_at((s + e) / 2, frames)
            pk = lay.frame_panels(lay.region_box(fr["region"], boxes), sw, sh) if fr else ps
            graph.append(lay.graph(pk, sw, sh))
            placed, gone = ovl.placed(cfg, conn, clip_fx, boxes, pk)
            ov_lists.append(placed)
            text_lists.append(txt.placed(clip_fx, boxes, pk))
            spot_lists.append(spt.placed(clip_fx, pk))
            cap_lists.append(cap.placed(clip_fx, boxes, pk))
            ov_missing |= gone
            pics, missing = short_visuals(clip_fx, boxes, pk)
            pictures.append(pics)
            no_region |= missing
        size = (lay.OUT_W, lay.OUT_H)
    else:
        boxes = _used_regions(conn, moment_pk, stream_fx)
        pictures, no_region = visuals(clip_fx, boxes)
        pictures = [list(pictures) for _ in segs]          # one list per segment, for the transitions
        placed, ov_missing = ovl.placed(cfg, conn, clip_fx, boxes, None)
        ov_lists = [placed] * len(segs)
        text_lists = [txt.placed(clip_fx, boxes, None)] * len(segs)
        spot_lists = [spt.placed(clip_fx, None)] * len(segs)
        cap_lists = [cap.placed(clip_fx, boxes, None)] * len(segs)
        size = (clip.width, clip.height)
    for k, v in motion.transitions(segs, clip_fx):
        pictures[k].append(v)
    from chatwatcher.clip.media import probe as probe_media
    made_intro = []

    def intro_progress(frac: float) -> None:
        made_intro.append(frac)
        conn.execute(f"UPDATE {table} SET render_progress = ? WHERE moment_id = ?", (round(frac * 0.3, 3), moment_pk))

    # The intro first, when the long edit starts with one (rendered now if it's out of date).
    intro, intro_key, intro_s = None, None, 0.0
    if not short:
        from chatwatcher.brand.service import intro_for_edit
        intro, intro_key = intro_for_edit(cfg, conn, moment_pk, clip, on_progress=intro_progress)
        if intro:
            intro_s = max(1, round(probe_media(intro).duration_s * clip.fps)) / clip.fps
    sound_cues, missing = [], set()
    for fx in clip_fx:
        if fx.get("type", "sfx") != "sfx":
            continue
        snd = get_sound(conn, fx["sound"])
        if not snd or not sound_path(cfg, snd).is_file():
            missing.add(fx["sound"])
            continue
        for at in place(fx["t"], segs, clip.fps):
            sound_cues.append(SoundCue(at + intro_s, sound_path(cfg, snd), fx["gain"], fx["duck"], snd["duration_s"]))
    for fx in clip_fx:                                     # an overlay's own sound
        ov = (ovl.get_overlay(conn, fx.get("overlay") or "")
              if fx.get("type") == ovl.OVERLAY and fx.get("mode") != "picture" else None)
        snd_file = ovl.sound_path(cfg, ov) if ov else None
        if snd_file and snd_file.is_file():
            for at in place(fx["t"], segs, clip.fps):
                sound_cues.append(SoundCue(at + intro_s, snd_file, fx.get("gain", 0) + ov["gain_db"], 0.0,
                                           min(fx["d"], ov["duration_s"])))

    last = [0.0]

    start = 0.3 if made_intro else 0.0     # rendering the intro took the first part of the bar

    def progress(frac: float) -> None:
        frac = start + frac * (1 - start)
        if time.monotonic() - last[0] > 0.5:
            last[0] = time.monotonic()
            conn.execute(f"UPDATE {table} SET render_progress = ? WHERE moment_id = ?", (round(frac, 3), moment_pk))
        if on_progress:
            on_progress(frac)

    conn.execute(f"UPDATE {table} SET render_progress = ? WHERE moment_id = ?", (start, moment_pk))
    try:
        r = render(cfg.edit, clip.path, segs, clip.out_path(kind), fps=clip.fps, has_audio=clip.has_audio,
                   sounds=sound_cues, visuals=pictures, subs=cues if sub_style["on"] else [],
                   sub_style=sub_style, size=size, font=cfg.subtitles.font,
                   intro=intro, intro_s=intro_s, layout=graph, title=title, overlays=ov_lists, texts=text_lists, spots=spot_lists,
                   captions=cap_lists, on_progress=progress)
    finally:
        conn.execute(f"UPDATE {table} SET render_progress = NULL WHERE moment_id = ?", (moment_pk,))
    # The subtitles as a .srt on the edit's timeline (for YouTube's CC), burned in or not.
    from chatwatcher.edit.render import lengths
    from chatwatcher.subs.lines import on_edit, srt
    shown = [(a + intro_s, b + intro_s, *rest) for a, b, *rest in on_edit(cues, segs, lengths(segs, clip.fps))]
    srt_path = clip.srt_for(kind)
    if shown:
        srt_path.write_text(srt(shown), encoding="utf-8")
    else:
        srt_path.unlink(missing_ok=True)
    stream_subs, _ = _subs_rendered(cfg, row, clip, kind)
    info = {"segments": stream, "effects": stream_fx, "regions": boxes, "subs": stream_subs, "subs_style": sub_style,
            "duration_s": round(r.duration_s, 2), "size_bytes": r.size_bytes, "sounds": len(sound_cues),
            "visuals": sum(f.get("type") in (*motion.TYPES, lay.FRAME, "transition", ovl.OVERLAY, txt.TEXT, spt.SPOT, cap.CAPTION) for f in clip_fx if any(s <= f["t"] < e for s, e, _ in segs)), "subtitles": len(shown) if sub_style["on"] else 0, "srt": bool(shown),
            "missing": sorted(missing | ov_missing), "missing_regions": sorted(no_region),
            "intro": intro_key, "intro_s": round(intro_s, 3)}
    if short:
        info |= {"layout": layout, "size": list(size)}
    conn.execute(f"UPDATE {table} SET rendered_json = ?, rendered_at = datetime('now') WHERE moment_id = ?",
                 (json.dumps(info, ensure_ascii=False), moment_pk))
    return info
