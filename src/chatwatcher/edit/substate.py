"""A clip's subtitles and transcript as the editor sees them: the cues (saved, or built
fresh from Whisper's words), their style, the voices, and where the transcript comes from."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from chatwatcher.config import Config
from chatwatcher.edit import captions as cap
from chatwatcher.edit import clipref as cr
from chatwatcher.edit import layout as lay
from chatwatcher.edit import regions
from chatwatcher.edit.clipref import ClipRef


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


def sub_default(cfg: Config | None, clip: ClipRef) -> dict[str, Any]:
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
                              sub_default(cfg, clip))
    if row and row["subs_json"] is not None:
        cues = [c | {"t0": round(c["t0"] - clip.zero_s, 3), "t1": round(c["t1"] - clip.zero_s, 3)}
                for c in json.loads(row["subs_json"])]
        cues = [c for c in cues if c["t1"] > 0 and c["t0"] < clip.duration_s]
        return speakers.paint(cues, clip.voices), "saved", style
    words = cr.words(clip)
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
    cues, source, style = current_subs(cfg, row if row is not None else cr.stored(conn, moment_pk, kind), clip, kind)
    words = cr.words(clip)
    w, h = (lay.OUT_W, lay.OUT_H) if kind == "short" else cr.size(clip)
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


def subs_rendered(cfg: Config | None, row: sqlite3.Row, clip: ClipRef,
                   kind: str = "long") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The subtitles a render would burn in now (stream time), and their style: to tell
    whether edit.mp4 / short.mp4 is out of date."""
    cues, _source, style = current_subs(cfg, row, clip, kind)
    return [c | {"t0": round(c["t0"] + clip.zero_s, 3), "t1": round(c["t1"] + clip.zero_s, 3)} for c in cues], style
