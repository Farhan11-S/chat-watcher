"""Low-res previews: for every moment that isn't downloaded at full quality, fetch a small
video-only range, keep only its still frames, and write the same text files a clip gets
(transcript, chat, moment.json). Claude analyses these, so you can review every moment
with its summary before anything is downloaded at full quality."""

from __future__ import annotations

import json
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from chatwatcher import store
from chatwatcher.clip.download import download_clip, preview_attempts
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

EventFn = Callable[[int, str], None]


class PreviewError(Exception):
    pass


@dataclass
class PreviewRun:
    video_id: str
    done: list[int] = field(default_factory=list)       # ranks
    skipped: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)


def has_material(folder: str | None) -> bool:
    return bool(folder) and (Path(folder) / "moment.json").is_file()


def preview_video(cfg: Config, conn: sqlite3.Connection, url_or_id: str, *,
                  ytdlp: YtDlp | None = None, ranks: list[int] | None = None,
                  on_event: EventFn | None = None) -> PreviewRun:
    event = on_event or (lambda _r, _m: None)
    try:
        video_id = extract_video_id(url_or_id)
    except ValueError as e:
        raise PreviewError(str(e)) from e
    row = store.get_video(conn, video_id)
    files = vod_files(cfg, video_id)
    if row is None or not files.info.is_file():
        raise PreviewError(f"{video_id} hasn't been ingested yet.")
    height = cfg.pipeline.preview_height or 360
    rows = conn.execute(
        """SELECT m.*, c.folder AS clip_folder, p.folder AS preview_folder
           FROM moments m LEFT JOIN clips c ON c.moment_id = m.id LEFT JOIN previews p ON p.moment_id = m.id
           WHERE m.video_id = ? AND m.status != 'rejected' AND m.kind != 'compilation'
           ORDER BY m.rank""", (row["id"],)).fetchall()      # a compilation is not one stretch of the stream
    run = PreviewRun(video_id=video_id)
    todo = []
    for r in rows:
        if ranks and r["rank"] not in ranks:
            continue
        if has_material(r["clip_folder"]) or has_material(r["preview_folder"]):
            run.skipped += 1           # a full-quality clip or a preview is there already
            continue
        todo.append(dict(r) | json.loads(r["features_json"] or "{}"))

    info = json.loads(files.info.read_text(encoding="utf-8"))
    # The stream's output folder must exist (analysis and uploads use it).
    if not (row["output_dir"] and Path(row["output_dir"]).is_dir()):
        from chatwatcher.clock import zone
        out_dir = video_dir(cfg.paths.output_dir, info, video_id, zone(cfg))
        out_dir.mkdir(parents=True, exist_ok=True)
        store.upsert_video(conn, video_id, output_dir=str(out_dir))
    _drop_orphans(conn, files.previews, row["id"])
    if not todo:
        return run

    try:
        yt = ytdlp or YtDlp(cfg.youtube)
        event(0, "Fetching fresh stream URLs…")
        full_info = yt.info(video_id)
    except YtDlpError as e:
        raise PreviewError(str(e)) from e
    files.downloads.mkdir(parents=True, exist_ok=True)
    info_json = files.downloads / "full_info.json"
    info_json.write_text(json.dumps(full_info), encoding="utf-8")
    messages = read_messages(files.chat) if files.chat.is_file() else []
    transcript = read_transcript(files.transcript) if files.transcript.is_file() else []
    stream_type = row["stream_type"] if row["stream_type"] != "auto" else (row["detected_type"] or "talk")
    title = info.get("title") or video_id

    def work(m: dict[str, Any]) -> tuple[Path, Any, list[dict]]:
        folder = files.previews / moment_dirname(m["rank"], m["kind"], m.get("reasons", []), m["start_s"])
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
        event(m["rank"], f"preview {m['end_s'] - m['start_s']:.0f}s at {height}p…")
        clip = download_clip(yt, info_json, m["start_s"], m["end_s"], folder / "preview", cfg.clip,
                             attempts=preview_attempts(height))
        frames = extract_frames(clip, folder / "frames", cfg.clip.frames_per_clip, m["peak_s"])
        write_chat_excerpt(folder / "chat_excerpt.txt", messages, clip)
        write_transcript_excerpt(folder / "transcript.txt", transcript, clip)
        write_moment_json(folder / "moment.json", video_id=video_id, title=title,
                          stream_type=stream_type, moment=m, clip=clip, frames=frames)
        clip.path.unlink(missing_ok=True)          # only the frames are needed
        return folder, clip, frames

    try:
        with ThreadPoolExecutor(max_workers=cfg.clip.max_concurrent) as pool:
            futures = {pool.submit(work, m): m for m in todo}
            for fut in as_completed(futures):
                m = futures[fut]
                try:
                    folder, clip, frames = fut.result()
                except (YtDlpError, MediaError, OSError, ValueError) as e:
                    msg = str(e).strip().splitlines()[-1][:200] if str(e).strip() else type(e).__name__
                    run.errors.append((m["rank"], msg))
                    event(m["rank"], f"preview failed: {msg}")
                    continue
                conn.execute(
                    """INSERT INTO previews (moment_id, folder, zero_s, duration_s, height, frames_json)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT (moment_id) DO UPDATE SET folder = excluded.folder, zero_s = excluded.zero_s,
                           duration_s = excluded.duration_s, height = excluded.height,
                           frames_json = excluded.frames_json, created_at = datetime('now')""",
                    (m["id"], str(folder), clip.zero_s, clip.duration_s, clip.height,
                     json.dumps(frames, ensure_ascii=False)))
                run.done.append(m["rank"])
                event(m["rank"], "preview done")
    finally:
        shutil.rmtree(files.downloads, ignore_errors=True)   # holds signed, expiring URLs
    run.done.sort()
    return run


def _drop_orphans(conn: sqlite3.Connection, root: Path, video_pk: int) -> None:
    """Re-scoring replaces moments; remove preview folders nothing points at any more."""
    if not root.is_dir():
        return
    keep = {Path(r["folder"]).name for r in conn.execute(
        "SELECT p.folder FROM previews p JOIN moments m ON m.id = p.moment_id WHERE m.video_id = ?", (video_pk,))}
    for d in root.iterdir():
        if d.is_dir() and not d.is_symlink() and d.name not in keep:
            shutil.rmtree(d, ignore_errors=True)
