"""A compilation idea (compile/) as one clip the editor can open: its pieces downloaded
with spare footage at both ends and joined into one clip.mp4.

The clip is a moment of kind "compilation", made when you ask for it on the stream page
and downloaded by the ordinary per-moment `clip` job. Its clock is its own: zero_s = 0 and
the moment runs 0–duration, so what the rest of the code calls stream time is, for this
moment, the joined clip's time; Whisper, the editor, the AI edit and the renders need
nothing special. Where each part came from in the stream is kept in the moment's features
({"compilation": {title, kind, reason, segments, parts}}) and in moment.json; the AI edit
reads the parts from there (edit/autoedit).

The pieces can be changed on the clip's page (`check`, `set_pieces`), before the first
download or after: the next download joins the new ones, and because every time in the
joined clip has moved, the edits made on the old one are dropped then (`settle`).
"""

from __future__ import annotations

import json
import math
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Callable

from chatwatcher.clip.download import ClipFile, download_clip
from chatwatcher.clip.media import concat, probe
from chatwatcher.clip.package import (
    chat_lines, extract_frames, slug, transcript_lines, write_lines, write_moment_json,
)
from chatwatcher.compile.prompt import ROLES
from chatwatcher.config import ClipConfig
from chatwatcher.scoring.moments import fmt_ts

KIND = "compilation"
RANK_BASE = 1000         # compilation clips are #1001, #1002, …: never a scored moment's rank
PAD_S = 4.0              # spare seconds each side: the idea's times are whole caption seconds
PARTS_DIR = "parts"
MAX_PIECES = 30
MAX_TOTAL_S = 1800.0     # of pieces: a mistyped hour must not download half the stream


def ranges(segments: list[dict[str, Any]], duration: float = 0, pad: float = PAD_S) -> list[dict[str, Any]]:
    """The idea's pieces as stream ranges to download, in play order: padded, kept inside
    the stream, and a piece that starts inside the padded one before it joined to it (one
    download, no jump)."""
    out: list[dict[str, Any]] = []
    for s in segments:
        a, b = max(0.0, float(s["start"]) - pad), float(s["end"]) + pad
        if duration:
            b = min(b, duration)
        if b - a < 1.0:
            continue
        role, note = s.get("role") or "beat", s.get("note") or ""
        if out and out[-1]["start"] <= a <= out[-1]["end"]:
            last = out[-1]
            last["end"] = max(last["end"], b)
            last["note"] = "; ".join(x for x in (last["note"], note) if x)
            if role != last["role"]:
                last["role"] = f"{last['role']} + {role}"
            continue
        out.append({"start": a, "end": b, "role": role, "note": note})
    return out


def key(title: str, segments: list[dict[str, Any]]) -> str:
    """What makes two ideas the same one: the title and the pieces' times. (Not the row id:
    every look at the stream replaces the idea rows, and SQLite hands the ids out again.)"""
    return json.dumps([title, [[float(s["start"]), float(s["end"])] for s in segments]], ensure_ascii=False)


def moment_key(features_json: str | None) -> str:
    """The idea a compilation moment was made from: kept as "idea", since its own pieces
    may have been changed since."""
    comp = json.loads(features_json or "{}").get("compilation") or {}
    return comp.get("idea") or key(comp.get("title") or "", comp.get("segments") or [])


def find(conn: sqlite3.Connection, video_pk: int, title: str, segments: list[dict[str, Any]]) -> sqlite3.Row | None:
    """The moment already made for this idea, if any."""
    want = key(title, segments)
    return next((m for m in conn.execute(f"SELECT * FROM moments WHERE video_id = ? AND kind = '{KIND}' ORDER BY rank",
                                         (video_pk,)) if moment_key(m["features_json"]) == want), None)


def create(conn: sqlite3.Connection, video_pk: int, idea_id: int) -> int | None:
    """The moment for one idea, made once (asking again returns it). It is marked Keep: you
    asked for it. Returns its id; None when the stream has no such idea (any more)."""
    c = conn.execute("SELECT * FROM compilations WHERE id = ? AND video_id = ?", (idea_id, video_pk)).fetchone()
    if c is None:
        return None
    segments = json.loads(c["segments_json"])
    old = find(conn, video_pk, c["title"], segments)
    if old is not None:
        return old["id"]
    todo = ranges(segments)
    length = sum(r["end"] - r["start"] for r in todo) or 1.0
    rank = max(RANK_BASE, conn.execute(
        f"SELECT COALESCE(MAX(rank), 0) FROM moments WHERE video_id = ? AND kind = '{KIND}'",
        (video_pk,)).fetchone()[0]) + 1
    features = {"reasons": [], "top_messages": [],
                "compilation": {"title": c["title"], "kind": c["kind"], "reason": c["reason"],
                                "rating": c["rating"], "segments": segments, "idea": key(c["title"], segments)}}
    return conn.execute(
        """INSERT INTO moments (video_id, rank, score, start_s, end_s, peak_s, kind, features_json, status)
           VALUES (?, ?, ?, 0, ?, ?, ?, ?, 'approved')""",
        (video_pk, rank, float(c["rating"]), length, length / 2, KIND,
         json.dumps(features, ensure_ascii=False))).lastrowid


def check(rows: list[dict[str, Any]], duration: float = 0) -> list[dict[str, Any]]:
    """Pieces typed on the clip's page, as the idea's segments: whole seconds, inside the
    stream, in the order given. Raises ValueError with what is wrong."""
    out: list[dict[str, Any]] = []
    for i, r in enumerate(rows, 1):
        a, b = max(0.0, float(math.floor(r["start"]))), float(math.ceil(r["end"]))
        if duration and a >= duration:
            raise ValueError(f"Piece {i} starts after the stream ends ({fmt_ts(duration)}).")
        if duration:
            b = min(b, float(math.ceil(duration)))
        if b <= a:
            raise ValueError(f"Piece {i} ends before it starts.")
        if b - a < 2:
            raise ValueError(f"Piece {i} is under 2 seconds long.")
        role = r.get("role") if r.get("role") in ROLES else "beat"
        out.append({"start": a, "end": b, "role": role, "note": " ".join(str(r.get("note") or "").split())[:200]})
    if not out:
        raise ValueError("A compilation needs at least one piece.")
    if len(out) > MAX_PIECES:
        raise ValueError(f"That is {len(out)} pieces; the most is {MAX_PIECES}.")
    total = sum(s["end"] - s["start"] for s in out)
    if total > MAX_TOTAL_S:
        raise ValueError(f"The pieces add up to {fmt_ts(total)}; the most is {fmt_ts(MAX_TOTAL_S)}. "
                         "Check the times for a typing mistake.")
    return out


def set_pieces(conn: sqlite3.Connection, moment_pk: int, segments: list[dict[str, Any]]) -> bool:
    """Replace a compilation moment's pieces (from `check`). False when they are the ones
    it has. The clip on disk is still the old pieces until the next download; "changed"
    says so, and `settle` acts on it."""
    row = conn.execute("SELECT features_json FROM moments WHERE id = ?", (moment_pk,)).fetchone()
    features = json.loads(row["features_json"] or "{}")
    comp = features.setdefault("compilation", {})
    old = comp.get("segments") or []

    def plain(segs: list[dict[str, Any]]) -> list[tuple]:
        return [(float(s["start"]), float(s["end"]), s.get("role") or "beat", s.get("note") or "") for s in segs]
    if plain(old) == plain(segments):
        return False
    comp.setdefault("idea", key(comp.get("title") or "", old))      # still that idea's clip
    comp["segments"], comp["changed"] = segments, True
    conn.execute("UPDATE moments SET features_json = ? WHERE id = ?",
                 (json.dumps(features, ensure_ascii=False), moment_pk))
    return True


def pieces(comp: dict[str, Any]) -> list[dict[str, Any]]:
    """The pieces as the clip's page lists them: each with `at`, the clip second it begins
    at in the joined clip on disk (None: not downloaded, or, after a change that is not
    downloaded yet, not all of it is in that clip)."""
    parts, changed = comp.get("parts") or [], bool(comp.get("changed"))
    out = []
    for s in comp.get("segments") or []:
        if changed:                                # is all of it in the clip that is on disk?
            p = next((p for p in parts if p["start"] - 0.5 <= s["start"] and s["end"] <= p["end"] + 0.5), None)
        else:                                      # the part it was downloaded in
            p = next((p for p in parts if p["start"] < s["end"] and s["start"] < p["end"]), None)
        out.append(dict(s) | {"at": round(p["t0"] + max(0.0, s["start"] - p["start"]), 1) if p else None})
    return out


def dirname(m: dict[str, Any]) -> str:
    return f"c{m['rank'] - RANK_BASE:02d}_{slug((m.get('compilation') or {}).get('title') or 'compilation', 40)}"


def download(yt: Any, info_json: Path, m: dict[str, Any], folder: Path, cfg: ClipConfig, *, duration: float,
             messages: list, transcript: list, video_id: str, title: str, stream_type: str,
             frames: bool = True, say: Callable[[str], None] | None = None,
             ) -> tuple[ClipFile, list[dict], list[dict[str, Any]]]:
    """Download the moment's pieces and join them into folder/clip.mp4, with the same files
    beside it as any clip (frames, chat and transcript excerpts, moment.json), all timed on
    the joined clip. Returns the clip, its frames, and the parts:
    [{t0, t1 (joined clip seconds), start, end (stream seconds), role, note}]."""
    say = say or (lambda _m: None)
    comp = m.get("compilation") or {}
    todo = ranges(comp.get("segments") or [], duration)
    if not todo:
        raise ValueError("This compilation has no pieces left inside the stream.")
    tmp = folder / PARTS_DIR
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        files: list[ClipFile] = []
        for i, r in enumerate(todo, 1):
            say(f"downloading part {i} of {len(todo)} ({r['end'] - r['start']:.0f}s)…")
            files.append(download_clip(yt, info_json, r["start"], r["end"], tmp / f"part{i:02d}", cfg))
        for old in folder.glob("clip.*"):                  # an earlier download
            if old.suffix in (".mp4", ".mkv", ".webm", ".mov"):
                old.unlink()
        if len(files) > 1:
            say("joining the parts…")
            path = concat([f.path for f in files], folder / "clip.mp4")
        else:
            path = files[0].path.replace(folder / f"clip{files[0].path.suffix}")
        pr = probe(path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    clip = ClipFile(path=path, zero_s=0.0, duration_s=pr.duration_s, width=pr.width, height=pr.height,
                    fps=pr.fps, vcodec=pr.vcodec, acodec=pr.acodec, size_bytes=pr.size_bytes,
                    format_id=files[0].format_id)

    parts, at = [], 0.0
    for f, r in zip(files, todo):
        t1 = min(at + f.duration_s, clip.duration_s)
        parts.append({"t0": round(at, 3), "t1": round(t1, 3), "start": f.zero_s,
                      "end": round(f.zero_s + f.duration_s, 3), "role": r["role"], "note": r["note"]})
        at = t1
    parts[-1]["t1"] = round(clip.duration_s, 3)

    peak = (parts[-1]["t0"] + parts[-1]["t1"]) / 2          # the last part is where it pays off
    frame_list = extract_frames(clip, folder / "frames", max(cfg.frames_per_clip, len(parts)), peak) if frames else []
    chat, said = [], []
    for p in parts:
        chat += chat_lines(messages, p["start"], p["t1"] - p["t0"], p["t0"])
        said += transcript_lines(transcript, p["start"], p["t1"] - p["t0"], p["t0"])
    write_lines(folder / "chat_excerpt.txt", chat)
    write_lines(folder / "transcript.txt", said)
    write_moment_json(
        folder / "moment.json", video_id=video_id, title=title, stream_type=stream_type,
        moment=m | {"peak_s": peak, "reasons": [], "top_messages": []}, clip=clip, frames=frame_list,
        extra={"youtube_url": f"https://youtu.be/{video_id}?t={int(parts[0]['start'])}",
               "stream_range": [fmt_ts(min(p["start"] for p in parts)), fmt_ts(max(p["end"] for p in parts))],
               "compilation": {k: comp.get(k) for k in ("title", "kind", "reason", "rating")} | {"parts": parts}})
    return clip, frame_list, parts


def settle(conn: sqlite3.Connection, m: dict[str, Any], clip: ClipFile, parts: list[dict[str, Any]]) -> None:
    """After a download: the moment runs 0–duration of the joined clip and remembers its
    parts. Updates m too (the clips row is written from it). When the pieces were changed
    since the last download, what was made on the old clip goes (`_start_over`)."""
    row = conn.execute("SELECT features_json FROM moments WHERE id = ?", (m["id"],)).fetchone()
    features = json.loads(row["features_json"] or "{}") if row else {}
    comp = features.setdefault("compilation", {})
    comp["parts"] = parts
    if comp.pop("changed", False):
        _start_over(conn, m["id"], clip.path.parent)
    peak = round((parts[-1]["t0"] + parts[-1]["t1"]) / 2, 3)
    conn.execute("UPDATE moments SET start_s = 0, end_s = ?, peak_s = ?, features_json = ? WHERE id = ?",
                 (clip.duration_s, peak, json.dumps(features, ensure_ascii=False), m["id"]))
    m.update(start_s=0.0, end_s=clip.duration_s, peak_s=peak, compilation=features["compilation"])


def _start_over(conn: sqlite3.Connection, moment_pk: int, folder: Path) -> None:
    """The joined clip is other footage now and every time in it has moved: the cuts,
    subtitles, effects and renders made on the old one would land in the wrong places.
    Drop them, and have Claude analyse the new clip (analyze.service.analysis_matches)."""
    from chatwatcher.edit.clipref import EDIT_FILE, SHORT_FILE, SHORT_SRT, SRT_FILE
    for table in ("edits", "shorts", "edit_chats"):
        conn.execute(f"DELETE FROM {table} WHERE moment_id = ?", (moment_pk,))
    conn.execute("UPDATE analyses SET analysis_json = json_remove(analysis_json, '$._meta.range') WHERE moment_id = ?",
                 (moment_pk,))
    for name in (EDIT_FILE, SRT_FILE, SHORT_FILE, SHORT_SRT):
        (folder / name).unlink(missing_ok=True)


def joins(meta: dict[str, Any]) -> list[float]:
    """Clip seconds where one part ends and the next begins (moment.json or a moment's features)."""
    parts = (meta.get("compilation") or {}).get("parts") or []
    return [float(p["t0"]) for p in parts[1:]]
