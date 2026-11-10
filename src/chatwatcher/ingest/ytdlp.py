"""Thin wrapper around the yt-dlp executable.

We shell out instead of importing yt_dlp so a stuck download can be killed cleanly
and yt-dlp can be upgraded independently.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Any, Callable

from chatwatcher.config import YouTubeConfig
from chatwatcher.tools import find_deno, resolve_exe

LineFn = Callable[[str], None]

_ID = r"[A-Za-z0-9_-]{11}"
_ID_PATTERNS = [
    re.compile(rf"(?:[?&]v=|youtu\.be/|/live/|/shorts/|/embed/)({_ID})(?![A-Za-z0-9_-])"),
    re.compile(rf"^({_ID})$"),
]


def extract_video_id(url_or_id: str) -> str:
    text = url_or_id.strip()
    for pattern in _ID_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(1)
    raise ValueError(f"Could not find a YouTube video id in: {url_or_id!r}")


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


class YtDlpError(Exception):
    """yt-dlp failed. `kind` is a short machine-readable reason; `retryable` hints the queue."""

    def __init__(self, message: str, *, kind: str = "error", retryable: bool = False,
                 output: str = ""):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.output = output


# (pattern, kind, retryable, hint) — first match wins.
_ERROR_HINTS: list[tuple[re.Pattern[str], str, bool, str]] = [
    (re.compile(r"confirm you.?re not a bot|Sign in to confirm", re.I), "bot_check", True,
     "YouTube wants a sign-in (bot check). Set youtube.cookies_file to a fresh cookies.txt "
     "export and install deno."),
    (re.compile(r"members[- ]only|Join this channel|channel's members", re.I), "members_only", False,
     "Members-only video: needs cookies from an account with that membership. Without one, "
     "remove the stream (✕ Remove stream on the Queue or stream page) so it stops showing up."),
    (re.compile(r"Private video", re.I), "private", False, "The video is private."),
    (re.compile(r"live event will begin|Premieres in|is_upcoming|has not started", re.I),
     "not_started", True, "The stream hasn't started yet."),
    (re.compile(r"HTTP Error 429|Too Many Requests", re.I), "rate_limited", True,
     "YouTube is rate-limiting requests; try again later."),
    (re.compile(r"Video unavailable|has been removed|This video is not available", re.I),
     "unavailable", False,
     "The video is unavailable (deleted, region-locked or age-restricted)."),
    (re.compile(r"timed out|Connection reset|Temporary failure|Network is unreachable|"
                r"HTTP Error 5\d\d|Remote end closed", re.I), "network", True,
     "Network problem talking to YouTube."),
]


def classify_error(output: str) -> tuple[str, bool, str]:
    for pattern, kind, retryable, hint in _ERROR_HINTS:
        if pattern.search(output):
            return kind, retryable, hint
    return "error", False, ""


def _error_lines(output: str, limit: int = 6) -> str:
    lines = [ln for ln in output.splitlines() if "ERROR" in ln] or output.splitlines()
    return "\n".join(lines[-limit:])


class YtDlp:
    def __init__(self, cfg: YouTubeConfig):
        exe = resolve_exe(cfg.ytdlp_path)
        if not exe:
            raise YtDlpError(f"yt-dlp not found ({cfg.ytdlp_path!r})", kind="missing")
        self.exe = exe
        self.cfg = cfg

    @property
    def deno(self) -> str | None:
        return find_deno(self.cfg.deno_path)

    # ── command building ─────────────────────────────────────────────────────

    def common_args(self) -> list[str]:
        """Flags for every call: ignore user-level yt-dlp config, add auth."""
        args = ["--ignore-config", "--no-playlist"]
        if self.cfg.cookies_file:
            cookies = Path(self.cfg.cookies_file).expanduser()
            if not cookies.is_file():
                raise YtDlpError(f"Cookies file not found: {cookies}", kind="config")
            args += ["--cookies", str(cookies)]
        if self.cfg.po_token:
            token = self.cfg.po_token if "+" in self.cfg.po_token else f"web+{self.cfg.po_token}"
            args += ["--extractor-args", f"youtube:po_token={token}"]
        if self.deno:
            args += ["--js-runtimes", f"deno:{self.deno}"]
        return args + list(self.cfg.extra_args)

    # ── running ──────────────────────────────────────────────────────────────

    def run(self, args: list[str], *, timeout: float, on_line: LineFn | None = None) -> str:
        """Run yt-dlp, streaming merged stdout/stderr lines to on_line. Returns the output.

        Raises YtDlpError on non-zero exit or timeout.
        """
        cmd = [self.exe, *self.common_args(), *args]
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        timed_out = threading.Event()

        def _kill() -> None:
            timed_out.set()
            proc.kill()

        timer = threading.Timer(timeout, _kill)
        timer.start()
        tail: deque[str] = deque(maxlen=400)
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
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
            raise YtDlpError(f"yt-dlp timed out after {timeout:.0f}s", kind="timeout",
                             retryable=True, output=output)
        if proc.returncode != 0:
            kind, retryable, hint = classify_error(output)
            msg = _error_lines(output) or f"yt-dlp exited with code {proc.returncode}"
            if hint:
                msg = f"{hint}\n{msg}"
            raise YtDlpError(msg, kind=kind, retryable=retryable, output=output)
        return output

    def version(self) -> str:
        out = subprocess.run([self.exe, "--version"], capture_output=True, text=True, timeout=30)
        return out.stdout.strip()

    # ── operations ───────────────────────────────────────────────────────────

    def info(self, video_id: str, *, timeout: float = 180) -> dict[str, Any]:
        """Full metadata (yt-dlp -J), including heatmap and caption track lists."""
        cmd = [self.exe, *self.common_args(), "-J", "--skip-download", watch_url(video_id)]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise YtDlpError(f"yt-dlp metadata timed out after {timeout:.0f}s",
                             kind="timeout", retryable=True) from e
        if out.returncode != 0:
            kind, retryable, hint = classify_error(out.stderr)
            msg = _error_lines(out.stderr) or f"yt-dlp exited with code {out.returncode}"
            raise YtDlpError(f"{hint}\n{msg}" if hint else msg, kind=kind,
                             retryable=retryable, output=out.stderr)
        try:
            return json.loads(out.stdout)
        except json.JSONDecodeError as e:
            raise YtDlpError("yt-dlp returned invalid JSON", output=out.stdout[-2000:]) from e

    def channel_streams(self, url: str, n: int = 15, *, timeout: float = 120) -> dict[str, Any]:
        """Flat listing of a channel's Streams tab: channel info + the newest n streams,
        each with live_status (is_upcoming / is_live / was_live) and duration."""
        cmd = [self.exe, *self.common_args(), "-J", "--flat-playlist", "--playlist-items", f"1:{n}", url]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise YtDlpError("yt-dlp channel listing timed out", kind="timeout", retryable=True) from e
        if out.returncode != 0:
            kind, retryable, hint = classify_error(out.stderr)
            msg = _error_lines(out.stderr) or f"yt-dlp exited with code {out.returncode}"
            raise YtDlpError(f"{hint}\n{msg}" if hint else msg, kind=kind, retryable=retryable,
                             output=out.stderr)
        try:
            return json.loads(out.stdout)
        except json.JSONDecodeError as e:
            raise YtDlpError("yt-dlp returned invalid JSON for the channel") from e

    def download_chat(self, video_id: str, dest: Path, *, on_line: LineFn | None = None,
                      timeout: float = 4 * 3600) -> Path:
        """Download the chat replay (<id>.live_chat.json, one JSON object per line)."""
        dest.mkdir(parents=True, exist_ok=True)
        self.run(
            ["--skip-download", "--write-subs", "--sub-langs", "live_chat", "--newline",
             "-P", str(dest), "-o", "%(id)s.%(ext)s", watch_url(video_id)],
            timeout=timeout, on_line=on_line,
        )
        path = dest / f"{video_id}.live_chat.json"
        if not path.is_file():
            raise YtDlpError("yt-dlp finished but no chat replay file was written "
                             "(chat replay may be disabled for this VOD).", kind="no_chat")
        return path

    def download_captions(self, video_id: str, lang: str, auto: bool, dest: Path, *,
                          timeout: float = 600) -> Path | None:
        """Download one caption track (json3 preferred). Returns the file or None."""
        dest.mkdir(parents=True, exist_ok=True)
        self.run(
            ["--skip-download", "--write-auto-subs" if auto else "--write-subs",
             "--sub-langs", re.escape(lang), "--sub-format", "json3/vtt/best",
             "-P", str(dest), "-o", "%(id)s.%(ext)s", watch_url(video_id)],
            timeout=timeout,
        )
        matches = sorted(p for p in dest.glob(f"{video_id}.{lang}.*") if not p.name.endswith(".part"))
        return matches[0] if matches else None
