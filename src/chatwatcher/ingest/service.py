"""Ingest one VOD: metadata → chat replay → captions → heatmap, saved under data/vods/<id>/.

Safe to re-run: metadata and heatmap are refreshed every time (the heatmap appears
once a VOD has enough views), while the slow chat download and captions are reused
unless force=True.
"""

from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from chatwatcher import store
from chatwatcher.config import Config
from chatwatcher.ingest import captions as cap
from chatwatcher.ingest.chat_parser import chat_stats, parse_chat_file, read_messages, write_messages
from chatwatcher.ingest.ytdlp import YtDlp, YtDlpError, extract_video_id
from chatwatcher.paths import VodFiles, vod_files

StepFn = Callable[[str], None]

# Metadata keys worth keeping; the full -J output is megabytes of format/caption URLs.
_INFO_KEYS = (
    "id", "title", "description", "channel", "channel_id", "uploader_id", "channel_url",
    "duration", "live_status", "was_live", "release_timestamp", "timestamp", "upload_date",
    "language", "tags", "categories", "chapters", "heatmap", "view_count", "like_count",
    "availability", "webpage_url",
)


class IngestError(Exception):
    def __init__(self, message: str, *, kind: str = "error", retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


@dataclass
class IngestResult:
    video_id: str
    title: str
    channel: str | None
    live_status: str | None
    duration_s: float | None
    files: VodFiles
    chat: dict[str, Any] | None = None          # chat_stats(), None = no chat replay
    chat_reused: bool = False
    caption_lang: str | None = None
    caption_auto: bool = False
    caption_segments: int = 0
    heatmap_points: int = 0
    warnings: list[str] = field(default_factory=list)


def trim_info(info: dict[str, Any]) -> dict[str, Any]:
    out = {k: info.get(k) for k in _INFO_KEYS if info.get(k) is not None}
    subs = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    out["has_chat_replay"] = "live_chat" in subs
    out["caption_tracks"] = {
        "manual": sorted(k for k in subs if k != "live_chat"),
        "auto_orig": sorted(k for k in auto if k.endswith("-orig")),
        "auto_count": len(auto),
    }
    return out


def published_at(info: dict[str, Any]) -> str | None:
    ts = info.get("release_timestamp") or info.get("timestamp")
    if ts:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    d = info.get("upload_date")
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if d and len(d) == 8 else None


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _gzip_file(src: Path, dest: Path) -> None:
    tmp = dest.with_name(dest.name + ".tmp")
    with src.open("rb") as fin, gzip.open(tmp, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, length=1 << 20)
    tmp.replace(dest)
    src.unlink()


def _check_finished(live_status: str | None) -> None:
    if live_status in ("is_live", "is_upcoming"):
        raise IngestError("The stream hasn't finished yet; ingest it after it ends.",
                          kind="not_finished", retryable=True)
    if live_status == "post_live":
        raise IngestError("The stream just ended and YouTube is still processing the archive "
                          "(chat replay isn't available yet). Try again later.",
                          kind="processing", retryable=True)


def ingest_video(
    cfg: Config,
    conn: sqlite3.Connection,
    url_or_id: str,
    *,
    force: bool = False,
    want_captions: bool = True,
    ytdlp: YtDlp | None = None,
    on_step: StepFn | None = None,
    on_progress: StepFn | None = None,
) -> IngestResult:
    step = on_step or (lambda _msg: None)
    try:
        video_id = extract_video_id(url_or_id)
    except ValueError as e:
        raise IngestError(str(e), kind="bad_input") from e

    try:
        yt = ytdlp or YtDlp(cfg.youtube)
        step("Fetching video info…")
        info = yt.info(video_id)
    except YtDlpError as e:
        store.upsert_video(conn, video_id, status="error", error=str(e))
        raise IngestError(str(e), kind=e.kind, retryable=e.retryable) from e

    live_status = info.get("live_status")
    try:
        _check_finished(live_status)
    except IngestError as e:
        store.upsert_video(conn, video_id, live_status=live_status, title=info.get("title"),
                           status="error", error=str(e))
        raise

    files = vod_files(cfg, video_id)
    files.root.mkdir(parents=True, exist_ok=True)
    trimmed = trim_info(info)
    _write_json(files.info, trimmed)

    channel_pk = None
    if info.get("channel_id"):
        channel_pk = store.upsert_channel(conn, info["channel_id"], handle=info.get("uploader_id"),
                                          title=info.get("channel"))
    store.upsert_video(
        conn, video_id,
        channel_id=channel_pk, title=info.get("title"), duration_s=info.get("duration"),
        published_at=published_at(info), live_status=live_status, data_dir=str(files.root),
        status="processing", error=None,
    )

    result = IngestResult(
        video_id=video_id, title=info.get("title") or video_id, channel=info.get("channel"),
        live_status=live_status, duration_s=info.get("duration"), files=files,
    )
    if live_status not in ("was_live", None):
        result.warnings.append(f"Not a stream archive (live_status={live_status}); "
                               "scoring works best on streams with chat replay.")

    try:
        _ingest_chat(yt, video_id, files, trimmed["has_chat_replay"], force, result, step,
                     on_progress)
        if want_captions:
            _ingest_captions(cfg, yt, video_id, info, files, force, result, step)
        result.heatmap_points = _save_heatmap(info, files)
    except YtDlpError as e:
        store.upsert_video(conn, video_id, status="error", error=str(e))
        raise IngestError(str(e), kind=e.kind, retryable=e.retryable) from e
    except Exception as e:
        store.upsert_video(conn, video_id, status="error", error=f"{type(e).__name__}: {e}")
        raise

    shutil.rmtree(files.downloads, ignore_errors=True)
    _write_json(files.manifest, {
        "video_id": video_id,
        "ingested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ytdlp_version": _safe_version(yt),
        "live_status": live_status,
        "chat": result.chat,
        "captions": {"lang": result.caption_lang, "auto": result.caption_auto,
                     "segments": result.caption_segments} if result.caption_lang else None,
        "heatmap_points": result.heatmap_points,
        "warnings": result.warnings,
    })
    store.upsert_video(
        conn, video_id,
        status="new", error=None,
        message_count=result.chat["messages"] if result.chat else None,
        caption_lang=result.caption_lang,
        has_heatmap=int(result.heatmap_points > 0),
        ingested_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    )
    return result


def _safe_version(yt: YtDlp) -> str | None:
    try:
        return yt.version()
    except Exception:
        return None


def _ingest_chat(yt: YtDlp, video_id: str, files: VodFiles, available: bool, force: bool,
                 result: IngestResult, step: StepFn, on_progress: StepFn | None) -> None:
    if not available:
        result.warnings.append("No chat replay on this video; moments will rely on the "
                               "replay heatmap only.")
        return
    if files.chat.is_file() and not force:
        step("Reusing chat downloaded earlier (use --force to re-download).")
        result.chat = chat_stats(read_messages(files.chat))
        result.chat_reused = True
        return

    if files.chat_raw.is_file() and not force:
        step("Re-parsing saved raw chat…")
    else:
        step("Downloading chat replay (long streams take a few minutes)…")
        raw = yt.download_chat(video_id, files.downloads, on_line=on_progress)
        _gzip_file(raw, files.chat_raw)
        step("Parsing chat…")

    messages = parse_chat_file(files.chat_raw)
    write_messages(files.chat, messages)
    result.chat = chat_stats(messages)
    if not messages:
        result.warnings.append("Chat replay downloaded but contained no messages.")


def _ingest_captions(cfg: Config, yt: YtDlp, video_id: str, info: dict[str, Any],
                     files: VodFiles, force: bool, result: IngestResult, step: StepFn) -> None:
    track = cap.choose_track(info, cfg.youtube.caption_langs)
    if not track:
        result.warnings.append("No captions available; clip cut points will use chat only.")
        return
    lang, auto = track
    result.caption_lang, result.caption_auto = lang, auto

    existing = sorted(files.root.glob(f"captions.{lang}.*"))
    if existing and files.transcript.is_file() and not force:
        result.caption_segments = len(cap.read_transcript(files.transcript))
        return

    step(f"Downloading captions ({lang}{', auto' if auto else ''})…")
    try:
        path = yt.download_captions(video_id, lang, auto, files.downloads)
    except YtDlpError as e:
        if e.kind in ("bot_check", "rate_limited"):
            raise
        result.warnings.append(f"Caption download failed: {e}")
        result.caption_lang = None
        return
    if path is None:
        result.warnings.append(f"Caption track {lang} was listed but not downloaded.")
        result.caption_lang = None
        return

    kept = files.root / f"captions.{lang}{path.suffix}"
    for old in files.root.glob("captions.*"):  # also drops a previously chosen track
        old.unlink()
    path.replace(kept)
    segments = cap.parse_caption_file(kept)
    cap.write_transcript(files.transcript, segments)
    result.caption_segments = len(segments)


def _save_heatmap(info: dict[str, Any], files: VodFiles) -> int:
    points = [
        [p["start_time"], p["end_time"], p["value"]]
        for p in (info.get("heatmap") or [])
        if {"start_time", "end_time", "value"} <= p.keys()
    ]
    if points:
        _write_json(files.heatmap, points)
    elif files.heatmap.exists():
        files.heatmap.unlink()
    return len(points)
