"""How much space chat-watcher uses: on this computer (a walk of output/ and data/) and on
Google Drive (`rclone about` plus one recursive listing of the Drive folder)."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chatwatcher.config import Config


# Video files in a clip folder: the download, your rendered edit, Short and intro. "Delete clip
# videos" removes these; everything else (summaries, frames, thumbnail) stays.
CLIP_FILES = ("clip.mp4", "clip.mkv", "edit.mp4", "short.mp4", "intro.mp4")


def dir_size(path: Path) -> tuple[int, int]:
    """(bytes, bytes in clip files) under path. Symlinks are not followed."""
    total = clips = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                size = os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
            total += size
            if name in CLIP_FILES:
                clips += size
    return total, clips


@dataclass
class Bucket:
    """One channel or one stream folder."""
    key: str                    # path relative to output/ ("channel" or "channel/stream")
    label: str
    local: int = 0
    clips: int = 0
    drive: int | None = None    # None = unknown (Drive not checked)
    video_id: str | None = None
    channel: str | None = None
    drive_url: str | None = None   # set once the stream (or a clip of it) was uploaded
    youtube_id: str | None = None  # from the folder name, for streams no longer in the database


@dataclass
class LocalUsage:
    disk_total: int
    disk_free: int
    output_bytes: int
    clip_bytes: int
    data_bytes: int
    data_on_same_disk: bool
    channels: list[Bucket] = field(default_factory=list)
    streams: list[Bucket] = field(default_factory=list)
    titles: dict[str, str] = field(default_factory=dict)   # video id → title, incl. deleted streams

    @property
    def ours(self) -> int:
        return self.output_bytes + (self.data_bytes if self.data_on_same_disk else 0)

    @property
    def other(self) -> int:
        return max(self.disk_total - self.disk_free - self.ours, 0)


def _existing_parent(p: Path) -> Path:
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def local_usage(cfg: Config, conn: sqlite3.Connection) -> LocalUsage:
    out_root, data_root = cfg.paths.output_dir, cfg.paths.data_dir
    disk = shutil.disk_usage(_existing_parent(out_root))
    same = _existing_parent(out_root).stat().st_dev == _existing_parent(data_root).stat().st_dev
    data_bytes = dir_size(data_root)[0] if data_root.is_dir() else 0

    # Name streams and channels from the database where we can; otherwise use folder names.
    known: dict[str, dict[str, Any]] = {}
    for r in conn.execute("""SELECT v.video_id, v.title, v.output_dir, v.drive_url, c.title AS channel
                             FROM videos v LEFT JOIN channels c ON c.id = v.channel_id
                             WHERE v.output_dir IS NOT NULL"""):
        try:
            rel = Path(r["output_dir"]).resolve().relative_to(out_root.resolve()).as_posix()
        except ValueError:
            continue
        known[rel] = dict(r)

    channels: list[Bucket] = []
    streams: list[Bucket] = []
    if out_root.is_dir():
        for ch_dir in sorted(p for p in out_root.iterdir() if p.is_dir()):
            ch = Bucket(key=ch_dir.name, label=ch_dir.name.replace("_", " "))
            for st_dir in sorted(p for p in ch_dir.iterdir() if p.is_dir()):
                size, clips = dir_size(st_dir)
                rel = f"{ch_dir.name}/{st_dir.name}"
                info = known.get(rel, {})
                if info.get("channel"):
                    ch.label = info["channel"]
                streams.append(Bucket(key=rel, label=info.get("title") or st_dir.name, local=size,
                                      clips=clips, video_id=info.get("video_id"), channel=ch.key,
                                      drive_url=info.get("drive_url")))
                ch.local += size
                ch.clips += clips
            # loose files directly in the channel folder
            ch.local += sum(f.stat().st_size for f in ch_dir.iterdir() if f.is_file())
            channels.append(ch)
    out_bytes = sum(c.local for c in channels)
    if out_root.is_dir():
        out_bytes += sum(f.stat().st_size for f in out_root.iterdir() if f.is_file())
    titles = {r["video_id"]: r["title"] for r in conn.execute(
        "SELECT video_id, title FROM discovered WHERE title IS NOT NULL "
        "UNION ALL SELECT video_id, title FROM videos WHERE title IS NOT NULL")}
    return LocalUsage(disk_total=disk.total, disk_free=disk.free, output_bytes=out_bytes,
                      clip_bytes=sum(c.clips for c in channels), data_bytes=data_bytes,
                      data_on_same_disk=same, channels=channels, streams=streams, titles=titles)


_FOLDER = re.compile(r"^(\d{4}-\d{2}-\d{2}|undated)_([A-Za-z0-9_-]{11})_(.*)$")


def folder_label(name: str, titles: dict[str, str]) -> tuple[str, str | None]:
    """Stream folder '<date>_<video id>_<title>' → (readable name, video id)."""
    m = _FOLDER.match(name)
    if not m:
        return name.replace("_", " "), None
    day, vid, rest = m.groups()
    return f"{titles.get(vid) or rest.replace('_', ' ')} ({day})", vid


@dataclass
class DriveUsage:
    checked_at: float
    total: int | None = None        # None = no quota (unlimited) or not reported
    used: int | None = None         # everything in this account's Drive (incl. trash)
    trashed: int | None = None
    other: int | None = None        # Gmail, Photos and the like
    free: int | None = None
    ours: int = 0                   # files under drive.folder
    files: int = 0
    by_path: dict[str, int] = field(default_factory=dict)   # "channel" and "channel/stream" → bytes
    error: str | None = None

    @property
    def your_other(self) -> int:
        return max((self.used or 0) - self.ours, 0)


def drive_usage(cfg: Config, rclone: Any = None) -> DriveUsage:
    from chatwatcher.upload.drive import Rclone, RcloneError
    res = DriveUsage(checked_at=time.time())
    try:
        rc = rclone or Rclone(cfg.drive)
        about = rc.about()
        res.total, res.used, res.trashed = about.get("total"), about.get("used"), about.get("trashed")
        res.other, res.free = about.get("other"), about.get("free")
        folder = cfg.drive.folder.strip("/")
        for e in rc.list_files(folder):
            size = max(int(e.get("Size") or 0), 0)
            parts = str(e.get("Path") or "").split("/")
            res.ours += size
            res.files += 1
            if len(parts) >= 2:
                res.by_path[parts[0]] = res.by_path.get(parts[0], 0) + size
            if len(parts) >= 3:
                key = f"{parts[0]}/{parts[1]}"
                res.by_path[key] = res.by_path.get(key, 0) + size
    except RcloneError as e:
        res.error = str(e)
    return res


def merge(local: LocalUsage, drive: DriveUsage | None) -> None:
    """Fill in each channel's and stream's Drive size, and add folders that are only on Drive."""
    if drive is None or drive.error:
        return
    have = {b.key for b in local.channels} | {b.key for b in local.streams}
    for b in [*local.channels, *local.streams]:
        b.drive = drive.by_path.get(b.key, 0)
    for key, size in drive.by_path.items():
        if key in have:
            continue
        if "/" in key:
            ch, folder = key.split("/", 1)
            label, vid = folder_label(folder, local.titles)
            local.streams.append(Bucket(key=key, label=label, drive=size, channel=ch, youtube_id=vid))
        else:
            local.channels.append(Bucket(key=key, label=key.replace("_", " "), drive=size))


def fmt_bytes(n: int | float | None) -> str:
    """1,284 B · 12.9 MB · 4.2 GB · 1.3 TB (decimal units, like disk makers and Google)."""
    if n is None:
        return "–"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1000 or unit == "PB":
            if unit == "B":
                return f"{int(n):,} B"
            return f"{n:.1f} {unit}" if abs(n) < 100 else f"{n:.0f} {unit}"
        n /= 1000
    return f"{n:.0f} PB"


# ── freeing space ────────────────────────────────────────────────────────────



@dataclass
class DeleteResult:
    freed: int = 0
    streams: int = 0
    skipped: list[str] = field(default_factory=list)   # why some weren't touched


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _stream_dir(cfg: Config, key: str) -> Path | None:
    """'<channel folder>/<stream folder>' → that folder under output/, or None if the key
    isn't exactly one stream folder there (no '..', no absolute paths, no deeper paths)."""
    parts = key.split("/")
    if len(parts) != 2 or any(p in ("", ".", "..") or "\\" in p for p in parts):
        return None
    path = cfg.paths.output_dir / parts[0] / parts[1]
    if not path.is_dir() or path.is_symlink() or not _inside(cfg.paths.output_dir, path):
        return None
    return path


def delete_streams(cfg: Config, conn: sqlite3.Connection, keys: list[str], *,
                   videos_only: bool) -> DeleteResult:
    """Free space on this computer. Google Drive is never touched.

    videos_only: delete just the clip video files. Summaries, frames, chat and
    Q&A stay, and the clips can be downloaded again ("Clip kept" / "Download it again").
    Otherwise: delete the stream's folder, its working data (chat, captions) and its
    dashboard entry, and remember it as seen so the channel watcher doesn't fetch it again.
    """
    res = DeleteResult()
    videos = {}
    for r in conn.execute("SELECT * FROM videos WHERE output_dir IS NOT NULL"):
        try:
            videos[Path(r["output_dir"]).resolve()] = r
        except OSError:
            continue
    for key in dict.fromkeys(keys):
        folder = _stream_dir(cfg, key)
        if folder is None:
            res.skipped.append(f"{key}: not a stream folder")
            continue
        v = videos.get(folder.resolve())
        if v is not None and conn.execute("SELECT 1 FROM jobs WHERE video_id = ? AND status = 'running'",
                                          (v["id"],)).fetchone():
            res.skipped.append(f"{v['title'] or key}: the worker is busy with it right now; try again later")
            continue
        if videos_only:
            freed = 0
            for clip_dir in folder.iterdir():
                if not clip_dir.is_dir() or clip_dir.is_symlink():
                    continue
                for name in CLIP_FILES:
                    f = clip_dir / name
                    if f.is_file() and not f.is_symlink():
                        freed += f.stat().st_size
                        f.unlink()
            res.freed += freed
            res.streams += 1
            continue

        if v is not None:
            res.freed += remove_video(cfg, conn, v, "deleted from the Storage page")
        else:
            res.freed += _remove_dir(cfg, folder)
        res.streams += 1
    return res


def _remove_dir(cfg: Config, folder: Path) -> int:
    """Delete a stream's output folder (and its channel folder once empty). Returns bytes freed."""
    if not folder.is_dir() or folder.is_symlink() or not _inside(cfg.paths.output_dir, folder):
        return 0
    freed = dir_size(folder)[0]
    shutil.rmtree(folder)
    channel_dir = folder.parent
    if channel_dir.resolve() != cfg.paths.output_dir.resolve() and not any(channel_dir.iterdir()):
        channel_dir.rmdir()
    return freed


def remove_video(cfg: Config, conn: sqlite3.Connection, v: sqlite3.Row, reason: str) -> int:
    """Remove a stream from this computer and the dashboard: its folder, its working data
    (chat, captions), its moments, clips and queue jobs. It's remembered as seen, so the
    channel watcher doesn't fetch it again. Google Drive isn't touched. Returns bytes freed."""
    freed = _remove_dir(cfg, Path(v["output_dir"])) if v["output_dir"] else 0
    work = cfg.paths.data_dir / "vods" / v["video_id"]
    if work.is_dir() and not work.is_symlink() and _inside(cfg.paths.data_dir, work):
        freed += dir_size(work)[0]
        shutil.rmtree(work)
    # Seen-but-removed: the watcher skips video ids it has a `discovered` row for.
    conn.execute(
        """INSERT INTO discovered (video_id, channel_id, title, duration_s, action, reason)
           VALUES (?, ?, ?, ?, 'skipped', ?)
           ON CONFLICT (video_id) DO UPDATE SET action = 'skipped', reason = excluded.reason""",
        (v["video_id"], v["channel_id"], v["title"], v["duration_s"], reason))
    conn.execute("DELETE FROM videos WHERE id = ?", (v["id"],))   # moments, clips, analyses, jobs, Q&A go too
    return freed
