"""rclone wrapper + upload service.

Uses `rclone copy`, never `sync`: files already on Drive are never deleted, so anything
you add there (edits, project files) is safe. Re-running only sends new/changed files.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from chatwatcher import store
from chatwatcher.config import Config, DriveConfig
from chatwatcher.ingest.ytdlp import extract_video_id
from chatwatcher.tools import resolve_exe

LineFn = Callable[[str], None]

_EXCLUDES = [".dl_*/**", ".render_*/**", "thumbs/**", "waveform.json", "*.part", "*.tmp", ".DS_Store"]   # thumbs/: cut-outs, versions


class RcloneError(Exception):
    """kind: missing | no_remote | auth | network | rate_limited | quota | error."""

    def __init__(self, message: str, *, kind: str = "error"):
        super().__init__(message)
        self.kind = kind


_HINTS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"didn't find section in config|not found in config|couldn't find root|"
                r"config file .* not found|unknown remote", re.I), "no_remote",
     "rclone has no remote named '{remote}'. Run `rclone config` to add your Google Drive "
     "(see the README), or set drive.remote to the name you used."),
    (re.compile(r"invalid_grant|token expired|oauth2|unauthori[sz]ed|\b401\b|empty token|"
                r"couldn't fetch token|failed to get token", re.I), "auth",
     "Google Drive login expired. Run `rclone config reconnect {remote}:` and sign in again."),
    (re.compile(r"storageQuotaExceeded|quota.*exceeded.*storage|The user's Drive storage quota", re.I),
     "quota", "Your Google Drive is full."),
    (re.compile(r"rateLimitExceeded|userRateLimitExceeded|\b429\b|too many requests", re.I),
     "rate_limited", "Google Drive is rate-limiting; will retry later."),
    (re.compile(r"dial tcp|no such host|connection reset|i/o timeout|timeout|network is unreachable|"
                r"TLS handshake", re.I), "network", "Network problem reaching Google Drive."),
]


def classify(output: str, remote: str) -> tuple[str, str]:
    for pattern, kind, hint in _HINTS:
        if pattern.search(output):
            return kind, hint.format(remote=remote)
    return "error", ""


def _remote_name(cfg: DriveConfig) -> str:
    return cfg.remote.rstrip(":")


class Rclone:
    def __init__(self, cfg: DriveConfig):
        exe = resolve_exe(cfg.rclone_path)
        if not exe:
            raise RcloneError(f"rclone not found ({cfg.rclone_path!r}). See the README to install it.",
                              kind="missing")
        self.exe = exe
        self.cfg = cfg
        self.remote = _remote_name(cfg)

    def _run(self, args: list[str], *, timeout: float = 60) -> str:
        try:
            out = subprocess.run([self.exe, *args], capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise RcloneError("rclone timed out", kind="network") from e
        if out.returncode != 0:
            kind, hint = classify(out.stderr + out.stdout, self.remote)
            tail = "\n".join(out.stderr.strip().splitlines()[-4:])
            raise RcloneError(f"{hint}\n{tail}".strip() if hint else tail or "rclone failed", kind=kind)
        return out.stdout

    def remotes(self) -> list[str]:
        return [r.rstrip(":") for r in self._run(["listremotes"]).split()]

    def check(self) -> list[str]:
        """Raise unless the remote exists and we can list it (i.e. the login works).
        Returns rclone's notices worth showing (e.g. the shared client id retirement)."""
        if self.remote not in self.remotes():
            _, hint = classify("didn't find section in config", self.remote)
            raise RcloneError(hint, kind="no_remote")
        out = subprocess.run([self.exe, "lsjson", "--max-depth", "1", "--dirs-only", f"{self.remote}:"],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            kind, hint = classify(out.stderr, self.remote)
            raise RcloneError(hint or out.stderr.strip().splitlines()[-1], kind=kind)
        notices = []
        if re.search(r"shared Google Drive client_id", out.stderr, re.I):
            notices.append("uses rclone's shared Google client id, which Google is retiring — create your own "
                           "(https://rclone.org/drive/#making-your-own-client-id) to keep uploads working")
        return notices

    def about(self) -> dict[str, int]:
        """Account storage: total, used, trashed, other (Gmail, Photos…), free — in bytes."""
        try:
            data = json.loads(self._run(["about", f"{self.remote}:", "--json"], timeout=60) or "{}")
        except json.JSONDecodeError as e:
            raise RcloneError("rclone about returned something unexpected") from e
        return {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}

    def list_files(self, path: str) -> list[dict]:
        """Every file under remote:path (Path relative to it, Size). Empty if it doesn't exist."""
        try:
            out = self._run(["lsjson", "-R", "--files-only", "--fast-list", "--no-modtime", "--no-mimetype",
                             f"{self.remote}:{path}"], timeout=300)
        except RcloneError as e:
            if re.search(r"directory not found|not found", str(e), re.I) and e.kind == "error":
                return []
            raise
        try:
            return json.loads(out or "[]")
        except json.JSONDecodeError as e:
            raise RcloneError("rclone lsjson returned something unexpected") from e

    def login_fingerprint(self) -> str | None:
        """A short hash of the remote's refresh token. It changes when you sign in again
        (`rclone config reconnect`), so we can tell when the current sign-in started."""
        try:
            remote = json.loads(self._run(["config", "dump"], timeout=30)).get(self.remote) or {}
            token = json.loads(remote.get("token") or "{}").get("refresh_token")
        except (RcloneError, json.JSONDecodeError, AttributeError):
            return None
        return hashlib.sha256(token.encode()).hexdigest()[:16] if token else None

    def copy(self, local_dir: Path, dest_path: str, *, on_line: LineFn | None = None,
             only: list[str] | None = None, timeout: float = 4 * 3600) -> str:
        """rclone copy local_dir → remote:dest_path. Returns rclone's last stats line.

        only: copy just these sub-folders of local_dir (plus the files directly in it)."""
        cmd = [self.exe, "copy", str(local_dir), f"{self.remote}:{dest_path}",
               "--transfers", str(self.cfg.transfers), "--stats", "15s", "--stats-one-line",
               "--stats-log-level", "NOTICE"]
        if only is None:
            for pattern in _EXCLUDES:
                cmd += ["--exclude", pattern]
        else:
            # rclone wants one ordered --filter list (first match wins) instead of mixing
            # --include and --exclude.
            rules = [f"- {p}" for p in _EXCLUDES] + ["+ /*"]
            rules += [f"+ /{_glob_escape(name)}/**" for name in only] + ["- **"]
            for rule in rules:
                cmd += ["--filter", rule]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", bufsize=1)
        timed_out = threading.Event()
        timer = threading.Timer(timeout, lambda: (timed_out.set(), proc.kill()))
        timer.start()
        tail: deque[str] = deque(maxlen=200)
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip()
                if line:
                    tail.append(line)
                    if on_line:
                        on_line(line)
            proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        output = "\n".join(tail)
        if timed_out.is_set():
            raise RcloneError("Upload timed out", kind="network")
        if proc.returncode != 0:
            kind, hint = classify(output, self.remote)
            raise RcloneError(f"{hint}\n{output[-600:]}".strip() if hint else output[-600:], kind=kind)
        stats = [ln for ln in tail if "Transferred" in ln or "%" in ln]
        return stats[-1] if stats else ""

    def folder_link(self, dest_path: str) -> str:
        """https link to the uploaded folder on Drive (falls back to remote:path).

        `lsjson --stat` on a folder returns it as the root, without an ID, so list the
        parent folder and pick the entry by name instead."""
        parent, _, name = dest_path.rstrip("/").rpartition("/")
        try:
            entries = json.loads(self._run(["lsjson", "--dirs-only", "--no-modtime", "--no-mimetype",
                                            f"{self.remote}:{parent}"], timeout=60) or "[]")
        except (RcloneError, json.JSONDecodeError):
            entries = []
        folder_id = next((e.get("ID") for e in entries if e.get("Name") == name and e.get("ID")), None)
        if folder_id:
            return f"https://drive.google.com/drive/folders/{folder_id}"
        return f"{self.remote}:{dest_path}"


# ── service ──────────────────────────────────────────────────────────────────

class UploadError(Exception):
    def __init__(self, message: str, *, kind: str = "error"):
        super().__init__(message)
        self.kind = kind


def _glob_escape(name: str) -> str:
    """Folder names can contain glob characters (e.g. [ ] from stream titles)."""
    return re.sub(r"([\\*?\[\]{}])", r"\\\1", name)


def clip_folders(conn: sqlite3.Connection, video_id: str, *, ranks: list[int] | None = None,
                 kept_only: bool = False) -> list[str]:
    """Names of the downloaded clip folders to upload: some ranks, or the ones marked Keep."""
    rows = conn.execute(
        """SELECT m.rank, m.status, c.folder FROM moments m JOIN clips c ON c.moment_id = m.id
           JOIN videos v ON v.id = m.video_id WHERE v.video_id = ? ORDER BY m.rank""",
        (video_id,)).fetchall()
    return [Path(r["folder"]).name for r in rows
            if r["folder"] and Path(r["folder"]).is_dir()
            and (not ranks or r["rank"] in ranks) and (not kept_only or r["status"] == "approved")]


@dataclass
class UploadResult:
    video_id: str
    local_dir: Path
    dest: str
    url: str
    stats: str


def dest_path_for(cfg: Config, out_dir: Path) -> str:
    """Mirror output/<channel>/<stream>/ under drive.folder."""
    try:
        rel = out_dir.resolve().relative_to(cfg.paths.output_dir.resolve()).as_posix()
    except ValueError:
        rel = out_dir.name
    return f"{cfg.drive.folder.strip('/')}/{rel}" if cfg.drive.folder.strip("/") else rel


def upload_video(cfg: Config, conn: sqlite3.Connection, url_or_id: str, *,
                 rclone: Rclone | None = None, on_line: LineFn | None = None,
                 folders: list[str] | None = None) -> UploadResult:
    """Copy the stream's output folder to Drive. folders: only these clip folders (and the
    stream-level files next to them)."""
    try:
        video_id = extract_video_id(url_or_id)
    except ValueError as e:
        raise UploadError(str(e)) from e
    v = store.get_video(conn, video_id)
    if v is None:
        raise UploadError(f"Unknown video {video_id}")
    out_dir = Path(v["output_dir"]) if v["output_dir"] else None
    if out_dir is None or not out_dir.is_dir() or not any(out_dir.iterdir()):
        raise UploadError("Nothing to upload yet (no clips for this stream).", kind="nothing")
    try:
        rc = rclone or Rclone(cfg.drive)
        dest = dest_path_for(cfg, out_dir)
        stats = rc.copy(out_dir, dest, on_line=on_line, only=folders)
        url = rc.folder_link(dest)
    except RcloneError as e:
        raise UploadError(str(e), kind=e.kind) from e
    store.upsert_video(conn, video_id, drive_url=url)
    for c in conn.execute("SELECT c.id, c.folder FROM clips c JOIN moments m ON m.id = c.moment_id "
                          "WHERE m.video_id = ?", (v["id"],)).fetchall():
        name = Path(c["folder"]).name if c["folder"] else None
        if name and (out_dir / name).is_dir() and (folders is None or name in folders):
            conn.execute("UPDATE clips SET uploaded_at = datetime('now') WHERE id = ?", (c["id"],))
    return UploadResult(video_id=video_id, local_dir=out_dir, dest=f"{rc.remote}:{dest}", url=url, stats=stats)
