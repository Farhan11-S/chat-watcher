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
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from chatwatcher.config import Config
from chatwatcher.edit import motion, regions
from chatwatcher.edit.audio import Waveform, analyse, snap

EDIT_FILE = "edit.mp4"
SRT_FILE = "edit.srt"
WAVEFORM_FILE = "waveform.json"
MAX_SEGMENTS = 40
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

    @property
    def edit_path(self) -> Path:
        return self.folder / EDIT_FILE

    @property
    def srt_path(self) -> Path:
        return self.folder / SRT_FILE


def clip_ref(conn: sqlite3.Connection, moment_pk: int) -> ClipRef:
    c = conn.execute("SELECT * FROM clips WHERE moment_id = ?", (moment_pk,)).fetchone()
    if c is None:
        raise EditError("This moment isn't downloaded yet.", "not_clipped")
    path = Path(c["path"])
    if not path.is_file():
        raise EditError("The clip video was deleted to free space. Download it again to edit it.", "not_clipped")
    return ClipRef(moment_pk, path, Path(c["folder"] or path.parent), float(c["zero_s"] or 0),
                   float(c["duration_s"] or 0), float(c["fps"] or 30), bool(c["acodec"]),
                   int(c["width"] or 0), int(c["height"] or 0))


# ── reading ──────────────────────────────────────────────────────────────────

def _latest_analysis(conn: sqlite3.Connection, moment_pk: int) -> dict[str, Any]:
    row = conn.execute("SELECT analysis_json FROM analyses WHERE moment_id = ? ORDER BY id DESC LIMIT 1",
                       (moment_pk,)).fetchone()
    try:
        return json.loads(row["analysis_json"]) if row else {}
    except json.JSONDecodeError:
        return {}


def proposals(a: dict[str, Any], clip: ClipRef, wf: Waveform, snap_s: float) -> dict[str, list[Segment] | None]:
    """Claude's long and short cuts in clip time, each edge moved to the nearest pause."""
    shift = float((a.get("_meta") or {}).get("zero_s", clip.zero_s)) - clip.zero_s
    cut = a.get("cut") or {}
    end = clip.duration_s

    def one(i_key: str, o_key: str) -> list[Segment] | None:
        i, o = cut.get(i_key), cut.get(o_key)
        if i is None or o is None:
            return None
        s = min(max(snap(float(i) + shift, wf.pauses, snap_s), 0.0), end)
        e = min(max(snap(float(o) + shift, wf.pauses, snap_s), 0.0), end)
        return [[round(s, 3), round(e, 3), 0.0]] if e - s >= 1 else None

    return {"long": one("long_in", "long_out"), "short": one("short_in", "short_out"),
            "whole": [[0.0, round(end, 3), 0.0]]}


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


def _stored(conn: sqlite3.Connection, moment_pk: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM edits WHERE moment_id = ?", (moment_pk,)).fetchone()


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


def load(cfg: Config, conn: sqlite3.Connection, moment_pk: int) -> dict[str, Any]:
    """Everything the editor needs, in clip time."""
    clip = clip_ref(conn, moment_pk)
    wf = analyse(clip.path, clip.folder / WAVEFORM_FILE)
    a = _latest_analysis(conn, moment_pk)
    props = proposals(a, clip, wf, cfg.edit.snap_s)
    row = _stored(conn, moment_pk)
    segments = _to_clip(json.loads(row["segments_json"]), clip) if row else []
    source = row["source"] if row and segments else None
    if not segments:
        source = "long" if props["long"] else "whole"
        segments = props[source]
    shift = float((a.get("_meta") or {}).get("zero_s", clip.zero_s)) - clip.zero_s
    beats = [{"start": round(float(b.get("start", 0)) + shift, 2), "end": round(float(b.get("end", 0)) + shift, 2),
              "beat": b.get("beat", ""), "edit": b.get("edit", ""), "overlay": b.get("text_overlay", "")}
             for b in a.get("storyboard") or []]
    from chatwatcher.edit.sounds import list_sounds
    sounds = [{"id": x["id"], "name": x["name"], "description": x["description"], "length": x["duration_s"],
               "gain": x["gain_db"]} for x in list_sounds(conn)]
    scope, scope_label = regions.scope_for(conn, moment_pk)
    return {
        "duration": clip.duration_s, "fps": clip.fps, **wf.to_json(),
        "segments": segments, "source": source, "saved": bool(row),
        "effects": _effects_to_clip(_effects(row), clip), "sounds": sounds,
        "regions": regions.get(conn, scope), "region_scope": scope_label,
        "visual": {"types": motion.TYPES, "zoom_range": motion.ZOOM_RANGE, "shake": motion.SHAKE_AMP,
                   "flash": motion.FLASH, "margin": motion.MARGIN, "waves": [motion.WAVES_X, motion.WAVES_Y]},
        "presets": props, "beats": beats, "lines": transcript_lines(clip.folder),
        "notes": (a.get("cut") or {}).get("notes", ""), "hook": a.get("hook", ""),
        "snap_s": cfg.edit.snap_s, "render": render_state(conn, moment_pk, clip, cfg=cfg),
        "transcript": transcript_state(cfg, conn, moment_pk, clip),
        **subtitles(cfg, conn, moment_pk, clip),
    }


def _words(clip: ClipRef) -> dict[str, Any] | None:
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
    return {"on": sc.on and not karaoke, "size": sc.size, "position": sc.position}


def current_subs(cfg: Config | None, row: sqlite3.Row | None, clip: ClipRef) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    """(cues in clip time, where they come from: saved | auto | none, style). Until you edit
    them, cues are built fresh from Whisper's words, so transcribing again updates them."""
    from chatwatcher.subs import lines
    style = lines.clean_style(json.loads(row["subs_style_json"]) if row and row["subs_style_json"] else {},
                              _sub_default(cfg, clip))
    if row and row["subs_json"] is not None:
        cues = [c | {"t0": round(c["t0"] - clip.zero_s, 3), "t1": round(c["t1"] - clip.zero_s, 3)}
                for c in json.loads(row["subs_json"])]
        return [c for c in cues if c["t1"] > 0 and c["t0"] < clip.duration_s], "saved", style
    words = _words(clip)
    return (lines.build(words), "auto", style) if words else ([], "none", style)


def subtitles(cfg: Config, conn: sqlite3.Connection, moment_pk: int, clip: ClipRef) -> dict[str, Any]:
    """The editor's subtitle data: the cues, what Whisper's words would give (to start over
    from), the word times (edges snap to them) and the language."""
    from chatwatcher.subs import lines
    cues, source, style = current_subs(cfg, _stored(conn, moment_pk), clip)
    words = _words(clip)
    from chatwatcher.subs import names
    return {"subs": cues, "subs_source": source, "subs_style": style,
            "names": names.get(conn, regions.scope_for(conn, moment_pk)[0]), "output_language": cfg.claude.output_language,
            "subs_auto": lines.build(words) if words else [],
            "sub_words": [[round(a, 3), round(b, 3)] for a, b, _ in lines.words_of(words)] if words else [],
            "sub_language": (words or {}).get("language"),
            "sub_layout": {"max_line": lines.MAX_LINE, "sizes": lines.SIZES, "highlight": lines.HIGHLIGHT,
                           "font": cfg.subtitles.font}}


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
                 cfg: Config | None = None) -> dict[str, Any]:
    """none | queued | running | done | stale (changed since the last render) | error."""
    if clip is None:
        try:
            clip = clip_ref(conn, moment_pk)
        except EditError:
            return {"state": "none"}
    job = conn.execute("SELECT status, error FROM jobs WHERE moment_id = ? AND stage = 'render'",
                       (moment_pk,)).fetchone()
    row = _stored(conn, moment_pk)
    out: dict[str, Any] = {"state": "none"}
    f = clip.edit_path
    rendered = json.loads(row["rendered_json"]) if row and row["rendered_json"] else None
    if f.is_file() and rendered:
        same = (rendered.get("segments") == json.loads(row["segments_json"])
                and rendered.get("effects", []) == _effects(row)
                and rendered.get("regions", {}) == _used_regions(conn, moment_pk, _effects(row))
                and _subs_rendered(cfg, row, clip) == (rendered.get("subs", []), rendered.get("subs_style")))
        if clip.srt_path.is_file():
            out["srt"] = f"/media/edit-srt/{moment_pk}?v={int(clip.srt_path.stat().st_mtime)}"
        out |= {"url": f"/media/edit/{moment_pk}?v={int(f.stat().st_mtime)}", "size": f.stat().st_size,
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


def clean_effects(raw: Any, duration: float, sounds: set[str]) -> list[dict[str, Any]]:
    """Check effects from the browser or from Claude (clip time). Raises EditError(kind='invalid')."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise EditError("Effects must be a list.", "invalid")
    if len(raw) > MAX_EFFECTS:
        raise EditError(f"At most {MAX_EFFECTS} effects.", "invalid")
    out, ids = [], set()
    for i, fx in enumerate(raw, 1):
        kind = fx.get("type", "sfx") if isinstance(fx, dict) else None
        if kind != "sfx" and kind not in motion.TYPES:
            raise EditError(f"Effect {i} isn't a sound, zoom, shake or flash.", "invalid")
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
         effects: Any = None, subs: Any = None, subs_style: Any = None) -> list[Segment]:
    """Store the browser's segments (clip time), and its effects unless effects is None.
    The first save also records Claude's proposals, so your final cut can later be
    compared with what Claude suggested."""
    clip = clip_ref(conn, moment_pk)
    segs = clean_segments(raw, clip.duration_s)
    stream = [[round(s + clip.zero_s, 3), round(e + clip.zero_s, 3), g] for s, e, g in segs]
    if effects is not None:
        known = {r["id"] for r in conn.execute("SELECT id FROM sounds")}
        fx = [f | {"t": round(f["t"] + clip.zero_s, 3)} for f in clean_effects(effects, clip.duration_s, known)]
    else:
        fx = _effects(_stored(conn, moment_pk))
    proposed = None
    if _stored(conn, moment_pk) is None:
        wf = analyse(clip.path, clip.folder / WAVEFORM_FILE)
        a = _latest_analysis(conn, moment_pk)
        props = proposals(a, clip, wf, cfg.edit.snap_s)
        proposed = json.dumps({"zero_s": clip.zero_s, "model": a.get("model") or (a.get("_meta") or {}).get("model"),
                               "cut": a.get("cut"), "snapped": props})
    conn.execute(
        """INSERT INTO edits (moment_id, segments_json, source, proposed_json, effects_json) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (moment_id) DO UPDATE SET segments_json = excluded.segments_json,
               effects_json = excluded.effects_json,
               source = COALESCE(excluded.source, edits.source), updated_at = datetime('now')""",
        (moment_pk, json.dumps(stream), source if source in ("long", "short", "whole") else None, proposed,
         json.dumps(fx, ensure_ascii=False)))
    if subs is not None or subs_style is not None:
        from chatwatcher.subs.lines import SubsError, clean, clean_style
        try:
            if subs == "auto":                       # follow Whisper's words again
                conn.execute("UPDATE edits SET subs_json = NULL WHERE moment_id = ?", (moment_pk,))
            elif subs is not None:
                cues = [c | {"t0": round(c["t0"] + clip.zero_s, 3), "t1": round(c["t1"] + clip.zero_s, 3)}
                        for c in clean(subs, clip.duration_s)]
                conn.execute("UPDATE edits SET subs_json = ? WHERE moment_id = ?",
                             (json.dumps(cues, ensure_ascii=False), moment_pk))
            if subs_style is not None:
                conn.execute("UPDATE edits SET subs_style_json = ? WHERE moment_id = ?",
                             (json.dumps(clean_style(subs_style, _sub_default(cfg, clip))), moment_pk))
        except SubsError as e:
            raise EditError(str(e), "invalid")
    return segs


def _subs_rendered(cfg: Config | None, row: sqlite3.Row, clip: ClipRef) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The subtitles a render would burn in now (stream time), and their style: to tell
    whether edit.mp4 is out of date."""
    cues, _source, style = current_subs(cfg, row, clip)
    return [c | {"t0": round(c["t0"] + clip.zero_s, 3), "t1": round(c["t1"] + clip.zero_s, 3)} for c in cues], style


def render_moment(cfg: Config, conn: sqlite3.Connection, moment_pk: int, *,
                  on_progress: Callable[[float], None] | None = None) -> dict[str, Any]:
    """Render the saved edit to <clip folder>/edit.mp4. Returns {duration_s, size_bytes, missing}."""
    from chatwatcher.edit.render import SoundCue, place, render
    from chatwatcher.edit.sounds import get_sound, sound_path
    clip = clip_ref(conn, moment_pk)
    row = _stored(conn, moment_pk)
    if row is None:
        raise EditError("Nothing to render: no edit saved for this clip.", "invalid")
    stream = json.loads(row["segments_json"])
    segs = _to_clip(stream, clip)
    if not segs:
        raise EditError("The edit's segments are outside the downloaded clip. Pick them again.", "invalid")
    stream_fx = _effects(row)
    clip_fx = _effects_to_clip(stream_fx, clip)
    cues, _source, sub_style = current_subs(cfg, row, clip)
    boxes = _used_regions(conn, moment_pk, stream_fx)
    pictures, no_region = visuals(clip_fx, boxes)
    sound_cues, missing = [], set()
    for fx in clip_fx:
        if fx.get("type", "sfx") != "sfx":
            continue
        snd = get_sound(conn, fx["sound"])
        if not snd or not sound_path(cfg, snd).is_file():
            missing.add(fx["sound"])
            continue
        for at in place(fx["t"], segs, clip.fps):
            sound_cues.append(SoundCue(at, sound_path(cfg, snd), fx["gain"], fx["duck"], snd["duration_s"]))

    last = [0.0]

    def progress(frac: float) -> None:
        if time.monotonic() - last[0] > 0.5:
            last[0] = time.monotonic()
            conn.execute("UPDATE edits SET render_progress = ? WHERE moment_id = ?", (round(frac, 3), moment_pk))
        if on_progress:
            on_progress(frac)

    conn.execute("UPDATE edits SET render_progress = 0 WHERE moment_id = ?", (moment_pk,))
    try:
        r = render(cfg.edit, clip.path, segs, clip.edit_path, fps=clip.fps, has_audio=clip.has_audio,
                   sounds=sound_cues, visuals=pictures, subs=cues if sub_style["on"] else [],
                   sub_style=sub_style, size=(clip.width, clip.height), font=cfg.subtitles.font,
                   on_progress=progress)
    finally:
        conn.execute("UPDATE edits SET render_progress = NULL WHERE moment_id = ?", (moment_pk,))
    # The subtitles as a .srt on the edit's timeline (for YouTube's CC), burned in or not.
    from chatwatcher.edit.render import lengths
    from chatwatcher.subs.lines import on_edit, srt
    shown = on_edit(cues, segs, lengths(segs, clip.fps))
    if shown:
        clip.srt_path.write_text(srt(shown), encoding="utf-8")
    else:
        clip.srt_path.unlink(missing_ok=True)
    stream_subs, _ = _subs_rendered(cfg, row, clip)
    info = {"segments": stream, "effects": stream_fx, "regions": boxes, "subs": stream_subs, "subs_style": sub_style,
            "duration_s": round(r.duration_s, 2), "size_bytes": r.size_bytes, "sounds": len(sound_cues),
            "visuals": len(pictures), "subtitles": len(shown) if sub_style["on"] else 0, "srt": bool(shown),
            "missing": sorted(missing), "missing_regions": sorted(no_region)}
    conn.execute("UPDATE edits SET rendered_json = ?, rendered_at = datetime('now') WHERE moment_id = ?",
                 (json.dumps(info, ensure_ascii=False), moment_pk))
    return info
