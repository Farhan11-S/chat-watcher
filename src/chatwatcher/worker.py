"""The worker: checks channels, then runs queued jobs one at a time, forever.

Safe to stop and restart at any point: jobs interrupted mid-way are re-queued on start,
and every stage skips work that is already done. Only one worker may run per data dir.
"""

from __future__ import annotations

import fcntl
import logging
import signal
import sqlite3
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import IO

from chatwatcher import db, discover, jobs
from chatwatcher.config import Config
from chatwatcher.pipeline import Deps, run_stage, set_video_status, stage_enabled

log = logging.getLogger("chatwatcher.worker")


class WorkerLocked(Exception):
    pass


def setup_logging(cfg: Config, *, verbose: bool = False) -> Path:
    """Console + rotating file at data/logs/worker.log."""
    log_dir = cfg.paths.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "worker.log"
    root = logging.getLogger("chatwatcher")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not root.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%Y-%m-%d %H:%M:%S")
        from chatwatcher.clock import zone
        tz = zone(cfg)
        if tz is not None:   # log in the configured zone, not just the computer's
            fmt.converter = lambda secs: datetime.fromtimestamp(secs, tz).timetuple()
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        filelog = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        filelog.setFormatter(fmt)
        root.addHandler(console)
        root.addHandler(filelog)
    return path


class Worker:
    def __init__(self, cfg: Config, *, deps: Deps | None = None, idle_s: float = 15.0):
        self.cfg = cfg
        self.deps = deps or Deps()
        self.idle_s = idle_s
        self.stop = threading.Event()
        self._poked = threading.Event()
        self._lock_file: IO | None = None

    def poke(self) -> None:
        """Look at the queue now instead of after the idle wait (e.g. you pressed Render)."""
        self._poked.set()

    def _idle(self) -> None:
        end = time.monotonic() + self.idle_s
        while not (self.stop.is_set() or self._poked.is_set()) and time.monotonic() < end:
            self.stop.wait(min(0.5, max(0.0, end - time.monotonic())))
        self._poked.clear()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def acquire_lock(self) -> None:
        self.cfg.paths.data_dir.mkdir(parents=True, exist_ok=True)
        f = open(self.cfg.paths.data_dir / "worker.lock", "w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            raise WorkerLocked("Another worker is already running for this data folder.")
        self._lock_file = f

    def release_lock(self) -> None:
        if self._lock_file:
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None

    def install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            if self.stop.is_set():
                raise KeyboardInterrupt  # second Ctrl+C: stop now; the job is re-queued next start
            log.info("Stopping after the current job (press Ctrl+C again to stop now)…")
            self.stop.set()
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def run(self, *, once: bool = False) -> int:
        """Run until stopped. once=True: check channels, drain due jobs, then return.
        Returns the number of jobs processed."""
        self.acquire_lock()
        conn = db.open_db(self.cfg.paths.db_path)
        processed = 0
        try:
            n = jobs.reset_running(conn)
            if n:
                log.info("Re-queued %d job(s) interrupted by the last shutdown", n)
            if once:
                self.check_channels(conn, force=True)
            while not self.stop.is_set():
                if not once:
                    self.check_channels(conn)
                job = jobs.claim(conn)
                if job is None:
                    if once:
                        break
                    self._idle()
                    continue
                self.process(conn, job)
                processed += 1
        finally:
            conn.close()
            self.release_lock()
        return processed

    # ── steps ────────────────────────────────────────────────────────────────

    def check_channels(self, conn: sqlite3.Connection, *, force: bool = False) -> None:
        rows = (conn.execute("SELECT * FROM channels WHERE enabled = 1").fetchall() if force
                else discover.due_channels(conn, self.cfg.discover.poll_minutes))
        for row in rows:
            if self.stop.is_set():
                return
            r = discover.check_channel(self.cfg, conn, row, ytdlp=self.deps.ytdlp)
            if r.error:
                log.warning("Channel %s: check failed: %s", r.channel, r.error)
            elif r.queued or r.skipped:
                log.info("Channel %s: queued %d new stream(s)%s", r.channel, len(r.queued),
                         f", skipped {len(r.skipped)}" if r.skipped else "")

    def process(self, conn: sqlite3.Connection, job: sqlite3.Row) -> None:
        video = conn.execute("SELECT * FROM videos WHERE id = ?", (job["video_id"],)).fetchone()
        vid, stage, moment_pk = video["video_id"], job["stage"], job["moment_id"]
        label = f"{vid} {stage}" + (f" (moment {moment_pk})" if moment_pk else "")
        log.info("▶ %s (attempt %d) — %s", label, job["attempts"], (video["title"] or "")[:60])
        set_video_status(conn, vid, "processing")
        try:
            out = run_stage(self.cfg, conn, vid, stage, attempt=job["attempts"], moment_id=moment_pk,
                            deps=self.deps, params=jobs.job_params(job),
                            log=lambda m: log.info("  %s: %s", label, m))
        except Exception as e:  # a bug or an unexpected tool failure: record it, keep the worker alive
            log.exception("✗ %s crashed", label)
            jobs.finish(conn, job["id"], ok=False, message=f"{type(e).__name__}: {e}")
            set_video_status(conn, vid, "error", f"{stage}: {e}")
            return

        if out.status == "retry":
            log.info("↻ %s: %s — retrying in %s", label, out.message.splitlines()[0][:200],
                     _fmt_delta(out.retry_in))
            jobs.finish(conn, job["id"], ok=False, message=out.message, retry_in=out.retry_in)
            set_video_status(conn, vid, "processing", f"{stage}: waiting — {out.message.splitlines()[0][:300]}")
            return
        if out.status == "error":
            log.warning("✗ %s: %s", label, out.message.splitlines()[0][:300])
            jobs.finish(conn, job["id"], ok=False, message=out.message)
            set_video_status(conn, vid, "error", f"{stage}: {out.message[:500]}")
            return

        log.info("✓ %s: %s", label, out.message)
        jobs.finish(conn, job["id"], ok=True, message=out.message)
        if moment_pk:
            # A re-cut clip makes its old storyboard wrong: analyse it again. Then, if that
            # clip is on Drive and auto-upload is on, refresh it there (rclone only sends
            # what changed).
            from chatwatcher.analyze.service import analysis_matches
            if stage == "clip" and stage_enabled(self.cfg, "transcribe"):
                jobs.enqueue(conn, video["id"], "transcribe", moment_pk=moment_pk)   # then analyse, below
            elif stage in ("clip", "transcribe") and stage_enabled(self.cfg, "analyze") \
                    and not analysis_matches(conn, moment_pk):
                jobs.enqueue(conn, video["id"], "analyze", moment_pk=moment_pk)
            elif stage != "upload" and stage_enabled(self.cfg, "upload", conn) and conn.execute(
                    "SELECT 1 FROM clips WHERE moment_id = ? AND uploaded_at IS NOT NULL",
                    (moment_pk,)).fetchone():
                jobs.enqueue(conn, video["id"], "upload", moment_pk=moment_pk)
            busy = conn.execute("SELECT 1 FROM jobs WHERE video_id = ? AND status IN ('pending', 'running')",
                                (video["id"],)).fetchone()
            set_video_status(conn, vid, "processing" if busy else "done")
            return
        nxt = None if out.details.get("stop") else jobs.next_stage(stage)

        def skipped(s: str) -> bool:      # transcribe only ever follows a clip download
            return not stage_enabled(self.cfg, s, conn) or (s == "transcribe" and stage != "clip")
        while nxt and skipped(nxt):
            if nxt == "frames" and not stage_enabled(self.cfg, "clip"):
                # No clips and no previews: nothing for Claude to look at. Wait for the user
                # to review the moments and press "Clip kept".
                nxt = None
                break
            nxt = jobs.next_stage(nxt)
        if nxt:
            jobs.enqueue(conn, video["id"], nxt)
            set_video_status(conn, vid, "processing")
        else:
            set_video_status(conn, vid, "done")


def _fmt_delta(d) -> str:
    if d is None:
        return "?"
    s = int(d.total_seconds())
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m"


def run_now(cfg: Config, conn: sqlite3.Connection, video_pk: int, *, deps: Deps | None = None,
            say=print) -> str:
    """Run every stage for one video in the foreground (`cw run`). Returns the final status.
    Raises WorkerLocked if a background worker is running (use `cw add` then)."""
    worker = Worker(cfg, deps=deps)
    worker.acquire_lock()
    try:
        return _run_stages(worker, conn, video_pk, say)
    finally:
        worker.release_lock()


def _run_stages(worker: Worker, conn: sqlite3.Connection, video_pk: int, say) -> str:
    stage: str | None = "ingest"
    while stage:
        jobs.enqueue(conn, video_pk, stage)
        job = conn.execute(
            """UPDATE jobs SET status = 'running', attempts = attempts + 1, started_at = datetime('now')
               WHERE video_id = ? AND stage = ? AND moment_id IS NULL RETURNING *""",
            (video_pk, stage)).fetchone()
        say(f"▶ {stage}")
        worker.process(conn, job)
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
        if row["status"] != "done":
            return "waiting" if row["status"] == "pending" else "error"
        nxt = conn.execute(
            "SELECT stage FROM jobs WHERE video_id = ? AND status = 'pending' AND moment_id IS NULL",
            (video_pk,)).fetchone()
        stage = nxt["stage"] if nxt else None
    return "done"
