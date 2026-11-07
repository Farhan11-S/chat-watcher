"""Run one pipeline stage for a video and decide what happens next.

ingest → score → clip → analyze. Each stage is idempotent (finished work is skipped),
so a retry or a restart never redoes downloads or Claude calls.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from chatwatcher import store
from chatwatcher.config import Config

LogFn = Callable[[str], None]


@dataclass
class Deps:
    """Injected collaborators (tests swap in fakes)."""
    ytdlp: Any = None             # YtDlp-like; None = build from config
    claude: Any = None            # ClaudeRunner-like; None = build from config
    rclone: Any = None            # Rclone-like; None = build from config


@dataclass
class StageOutcome:
    status: str                   # done | retry | error | skip
    message: str = ""
    retry_in: timedelta | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _backoff(attempt: int, base_min: float, cap_min: float) -> timedelta:
    return timedelta(minutes=min(cap_min, base_min * 2 ** max(0, attempt - 1)))


_RESET = re.compile(r"\|(\d{10})\b")


def usage_reset_delay(message: str, default: timedelta = timedelta(hours=1)) -> timedelta:
    """Claude's usage-limit message often ends with '|<unix reset time>'."""
    m = _RESET.search(message or "")
    if m:
        wait = int(m.group(1)) - datetime.now(timezone.utc).timestamp()
        if 0 < wait < 8 * 24 * 3600:
            return timedelta(seconds=wait + 120)
    return default


def until_utc_midnight() -> timedelta:
    n = datetime.now(timezone.utc)
    tomorrow = (n + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    return tomorrow - n


def run_stage(cfg: Config, conn: sqlite3.Connection, video_id: str, stage: str, *,
              attempt: int = 1, moment_id: int | None = None, deps: Deps | None = None,
              log: LogFn | None = None) -> StageOutcome:
    """moment_id: redo just that moment's clip or analysis (forced)."""
    deps = deps or Deps()
    say = log or (lambda _m: None)
    if moment_id is not None:
        row = conn.execute("SELECT rank FROM moments WHERE id = ?", (moment_id,)).fetchone()
        if row is None:
            return StageOutcome("error", "moment no longer exists (the video was re-scored)")
        return {"clip": _clip, "analyze": _analyze}[stage](
            cfg, conn, video_id, attempt, deps, say, rank=row["rank"])
    fn = {"ingest": _ingest, "score": _score, "clip": _clip, "analyze": _analyze,
          "upload": _upload}[stage]
    return fn(cfg, conn, video_id, attempt, deps, say)


# ── stages ───────────────────────────────────────────────────────────────────

def _ingest(cfg, conn, video_id, attempt, deps, say) -> StageOutcome:
    from chatwatcher.ingest.service import IngestError, ingest_video
    try:
        r = ingest_video(cfg, conn, video_id, ytdlp=deps.ytdlp, on_step=say)
    except IngestError as e:
        msg = str(e)
        if e.kind in ("not_finished", "processing"):
            # Still live, or YouTube is still preparing the archive. Keep checking for 2 days.
            if attempt < 96:
                return StageOutcome("retry", msg, timedelta(minutes=30))
        elif e.kind == "bot_check":
            if attempt < 3:
                return StageOutcome("retry", msg, timedelta(hours=2))
        elif e.retryable and attempt < 6:
            return StageOutcome("retry", msg, _backoff(attempt, 5, 120))
        return StageOutcome("error", msg)
    chat = r.chat["messages"] if r.chat else 0
    return StageOutcome("done", f"{chat:,} chat messages" + (f"; {len(r.warnings)} warning(s)" if r.warnings else ""))


def _score(cfg, conn, video_id, attempt, deps, say) -> StageOutcome:
    from chatwatcher.scoring.service import ScoreError, score_video
    try:
        r = score_video(cfg, conn, video_id)
    except ScoreError as e:
        return StageOutcome("error", str(e))
    if not r.moments:
        return StageOutcome("done", "no moments found", details={"stop": True})
    return StageOutcome("done", f"{r.stream_type}: {len(r.moments)} moments")


def _clip(cfg, conn, video_id, attempt, deps, say, rank: int | None = None) -> StageOutcome:
    from chatwatcher.clip.service import ClipError, clip_video
    top = None if rank else (cfg.pipeline.clip_top or None)
    try:
        run = clip_video(cfg, conn, video_id, top=top, ytdlp=deps.ytdlp,
                         ranks=[rank] if rank else None, force=bool(rank),
                         on_event=lambda r, m: say(f"clip #{r}: {m}" if r else m))
    except ClipError as e:
        if attempt < 4:
            return StageOutcome("retry", str(e), _backoff(attempt, 15, 120))
        return StageOutcome("error", str(e))
    done, skipped, errors = run.count("done"), run.count("skipped"), run.count("error")
    msg = f"{done} clipped, {skipped} already there" + (f", {errors} failed" if errors else "")
    if errors and not (done or skipped):
        if attempt < 4:
            return StageOutcome("retry", msg + ": " + run.outcomes[0].message[:300], _backoff(attempt, 15, 120))
        return StageOutcome("error", msg + ": " + run.outcomes[0].message[:300])
    return StageOutcome("done", msg)


def _analyze(cfg, conn, video_id, attempt, deps, say, rank: int | None = None) -> StageOutcome:
    from chatwatcher.analyze.service import AnalyzeError, analyze_video
    if not cfg.claude.enabled:
        return StageOutcome("skip", "claude.enabled is false")
    top = None if rank else (cfg.pipeline.analyze_top or None)
    try:
        run = analyze_video(cfg, conn, video_id, top=top, runner=deps.claude,
                            ranks=[rank] if rank else None, force=bool(rank),
                            on_event=lambda r, m: say(f"analyze #{r}: {m}" if r else m))
    except AnalyzeError as e:
        if e.kind in ("auth", "missing") and attempt < 48:
            return StageOutcome("retry", str(e), timedelta(hours=1))   # resumes once you log in
        return StageOutcome("error", str(e))
    done, deferred, errors = run.count("done"), run.count("deferred"), run.count("error")
    msg = f"{done} analysed" + (f", {deferred} deferred" if deferred else "") + (f", {errors} failed" if errors else "")
    if run.calls:
        msg += f" ({run.usage['output']:,} output tokens)"
    if deferred:
        reason = next(o.message for o in run.outcomes if o.status == "deferred")
        wait = until_utc_midnight() if "daily cap" in reason else usage_reset_delay(reason)
        return StageOutcome("retry", f"{msg}; {reason[:200]}", wait)
    if errors and not done and attempt < 3:
        return StageOutcome("retry", msg, timedelta(minutes=30))
    if run.digest_error:
        msg += f"; digest failed: {run.digest_error[:150]}"
    return StageOutcome("done" if (done or not errors) else "error", msg)


def _upload(cfg, conn, video_id, attempt, deps, say) -> StageOutcome:
    from chatwatcher.upload.drive import UploadError, upload_video
    if not cfg.drive.enabled:
        return StageOutcome("skip", "drive.enabled is false")
    try:
        r = upload_video(cfg, conn, video_id, rclone=deps.rclone, on_line=lambda m: say(f"rclone: {m}"))
    except UploadError as e:
        if e.kind == "nothing":
            return StageOutcome("done", str(e))
        if e.kind in ("missing", "no_remote", "auth") and attempt < 48:
            return StageOutcome("retry", str(e), timedelta(hours=1))   # resumes once rclone is set up
        if e.kind in ("network", "rate_limited", "error") and attempt < 6:
            return StageOutcome("retry", str(e), _backoff(attempt, 10, 180))
        return StageOutcome("error", str(e))
    return StageOutcome("done", f"uploaded to {r.dest}" + (f" ({r.stats})" if r.stats else ""))


# ── bookkeeping shared by the worker and `cw run` ─────────────────────────────

def stage_enabled(cfg: Config, stage: str) -> bool:
    if stage == "clip":
        return cfg.pipeline.auto_clip
    if stage == "analyze":
        return cfg.pipeline.auto_analyze and cfg.claude.enabled
    if stage == "upload":
        return cfg.drive.enabled and cfg.drive.auto_upload
    return True


def set_video_status(conn: sqlite3.Connection, video_id: str, status: str, error: str | None = None) -> None:
    store.upsert_video(conn, video_id, status=status, error=error)
