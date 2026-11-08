"""Clip every scored moment of a VOD into output/<channel>/<date>_<id>_<title>/<NN>_<label>/."""

from __future__ import annotations

import json
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from chatwatcher import store
from chatwatcher.clip.download import ClipFile, download_clip
from chatwatcher.clip.media import MediaError
from chatwatcher.clip.package import (
    extract_frames, moment_dirname, video_dir, write_chat_excerpt, write_moment_json,
    write_transcript_excerpt,
)
from chatwatcher.config import Config
from chatwatcher.ingest.captions import read_transcript
from chatwatcher.ingest.chat_parser import read_messages
from chatwatcher.ingest.ytdlp import YtDlp, YtDlpError, extract_video_id
from chatwatcher.paths import vod_files

EventFn = Callable[[int, str], None]   # (rank, message)


class ClipError(Exception):
    pass


@dataclass
class ClipOutcome:
    rank: int
    moment_id: int
    status: str                      # done | skipped | error
    message: str = ""
    clip: ClipFile | None = None
    folder: Path | None = None
    frames: list[dict] = field(default_factory=list)


@dataclass
class ClipRun:
    video_id: str
    output_dir: Path
    outcomes: list[ClipOutcome] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(o.status == status for o in self.outcomes)


def _moments(conn: sqlite3.Connection, video_pk: int, ranks: list[int] | None,
             top: int | None) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM moments WHERE video_id = ? AND status != 'rejected' ORDER BY rank",
        (video_pk,),
    ).fetchall()
    out = []
    for r in rows:
        if ranks and r["rank"] not in ranks:
            continue
        out.append(dict(r) | json.loads(r["features_json"] or "{}"))
    return out[:top] if top else out


def clip_video(
    cfg: Config,
    conn: sqlite3.Connection,
    url_or_id: str,
    *,
    ranks: list[int] | None = None,
    top: int | None = None,
    force: bool = False,
    frames: bool = True,
    ytdlp: YtDlp | None = None,
    on_event: EventFn | None = None,
) -> ClipRun:
    event = on_event or (lambda _r, _m: None)
    try:
        video_id = extract_video_id(url_or_id)
    except ValueError as e:
        raise ClipError(str(e)) from e
    row = store.get_video(conn, video_id)
    files = vod_files(cfg, video_id)
    if row is None or not files.info.is_file():
        raise ClipError(f"{video_id} hasn't been ingested yet. Run `cw ingest {video_id}` first.")
    moments = _moments(conn, row["id"], ranks, top)
    if not moments:
        raise ClipError(f"No moments to clip for {video_id}. Run `cw score {video_id}` first.")

    info = json.loads(files.info.read_text(encoding="utf-8"))
    # Keep a stream's folder once it exists, even if its title or the time zone changes.
    if row["output_dir"] and Path(row["output_dir"]).is_dir():
        out_dir = Path(row["output_dir"])
    else:
        from chatwatcher.clock import zone
        out_dir = video_dir(cfg.paths.output_dir, info, video_id, zone(cfg))
    out_dir.mkdir(parents=True, exist_ok=True)
    store.upsert_video(conn, video_id, output_dir=str(out_dir))
    run = ClipRun(video_id=video_id, output_dir=out_dir)

    existing = {r["moment_id"]: r for r in conn.execute(
        "SELECT * FROM clips WHERE moment_id IN (%s)" % ",".join("?" * len(moments)),
        [m["id"] for m in moments])}
    todo = []
    for m in moments:
        ex = existing.get(m["id"])
        if ex and Path(ex["path"]).is_file() and not force:
            run.outcomes.append(ClipOutcome(m["rank"], m["id"], "skipped", "already clipped",
                                            folder=Path(ex["folder"]) if ex["folder"] else None))
        else:
            todo.append(m)
    if not todo:
        return run

    try:
        yt = ytdlp or YtDlp(cfg.youtube)
        event(0, "Fetching fresh stream URLs…")
        full_info = yt.info(video_id)
    except YtDlpError as e:
        raise ClipError(str(e)) from e
    files.downloads.mkdir(parents=True, exist_ok=True)
    info_json = files.downloads / "full_info.json"
    info_json.write_text(json.dumps(full_info), encoding="utf-8")

    messages = read_messages(files.chat) if files.chat.is_file() else []
    transcript = read_transcript(files.transcript) if files.transcript.is_file() else []
    stream_type = row["stream_type"] if row["stream_type"] != "auto" else (row["detected_type"] or "talk")
    title = info.get("title") or video_id

    def work(m: dict[str, Any]) -> ClipOutcome:
        folder = out_dir / moment_dirname(m["rank"], m["kind"], m.get("reasons", []), m["start_s"])
        old = existing.get(m["id"])
        if old and old["folder"] and Path(old["folder"]) != folder and Path(old["folder"]).is_dir() \
                and not folder.exists():
            shutil.move(old["folder"], folder)   # cut points moved: rename, keep anything else in it
        folder.mkdir(parents=True, exist_ok=True)
        event(m["rank"], f"downloading {m['end_s'] - m['start_s']:.0f}s…")
        clip = download_clip(yt, info_json, m["start_s"], m["end_s"], folder / "clip", cfg.clip)
        frame_list = (extract_frames(clip, folder / "frames", cfg.clip.frames_per_clip, m["peak_s"])
                      if frames else [])
        write_chat_excerpt(folder / "chat_excerpt.txt", messages, clip)
        write_transcript_excerpt(folder / "transcript.txt", transcript, clip)
        write_moment_json(folder / "moment.json", video_id=video_id, title=title,
                          stream_type=stream_type, moment=m, clip=clip, frames=frame_list)
        return ClipOutcome(m["rank"], m["id"], "done", clip=clip, folder=folder,
                           frames=frame_list)

    try:
        with ThreadPoolExecutor(max_workers=cfg.clip.max_concurrent) as pool:
            futures = {pool.submit(work, m): m for m in todo}
            for fut in as_completed(futures):
                m = futures[fut]
                try:
                    outcome = fut.result()
                except (YtDlpError, MediaError, OSError, ValueError) as e:
                    outcome = ClipOutcome(m["rank"], m["id"], "error", str(e).strip())
                    event(m["rank"], f"failed: {str(e).strip().splitlines()[0][:150]}")
                else:
                    _save_clip(conn, m, outcome)
                    from chatwatcher.analyze.service import carry_analysis
                    reused = carry_analysis(conn, m["id"], outcome.folder)
                    event(m["rank"], "done" + (" (kept Claude's analysis from the preview)" if reused else ""))
                run.outcomes.append(outcome)
    finally:
        shutil.rmtree(files.downloads, ignore_errors=True)  # holds signed, expiring URLs
    run.outcomes.sort(key=lambda o: o.rank)
    return run


def _save_clip(conn: sqlite3.Connection, m: dict[str, Any], o: ClipOutcome) -> None:
    c = o.clip
    assert c is not None and o.folder is not None
    conn.execute(
        """
        INSERT INTO clips (moment_id, path, container, width, height, fps, vcodec, acodec,
                           size_bytes, folder, zero_s, duration_s, req_start_s, req_end_s,
                           format_id, frames_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (moment_id) DO UPDATE SET
            path = excluded.path, container = excluded.container, width = excluded.width,
            height = excluded.height, fps = excluded.fps, vcodec = excluded.vcodec,
            acodec = excluded.acodec, size_bytes = excluded.size_bytes, folder = excluded.folder,
            zero_s = excluded.zero_s, duration_s = excluded.duration_s,
            req_start_s = excluded.req_start_s, req_end_s = excluded.req_end_s,
            format_id = excluded.format_id, frames_json = excluded.frames_json,
            created_at = datetime('now')
        """,
        (m["id"], str(c.path), c.container, c.width, c.height, c.fps, c.vcodec, c.acodec,
         c.size_bytes, str(o.folder), c.zero_s, c.duration_s, m["start_s"], m["end_s"],
         c.format_id, json.dumps(o.frames, ensure_ascii=False)),
    )
