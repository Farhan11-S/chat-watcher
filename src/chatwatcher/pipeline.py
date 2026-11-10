"""Run one pipeline stage for a video and decide what happens next.

ingest → score → clip → analyze. Each stage is idempotent (finished work is skipped),
so a retry or a restart never redoes downloads or Claude calls.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    whisper: Any = None           # subs.transcribe.Engine-like; None = faster-whisper from config


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




def run_stage(cfg: Config, conn: sqlite3.Connection, video_id: str, stage: str, *,
              attempt: int = 1, moment_id: int | None = None, deps: Deps | None = None,
              log: LogFn | None = None, params: dict[str, Any] | None = None) -> StageOutcome:
    """moment_id: redo just that moment's clip or analysis (forced), or upload just its clip.
    params: per-job options from the dashboard (analyze: model, effort, redo; upload: kept_only)."""
    deps = deps or Deps()
    say = log or (lambda _m: None)
    params = params or {}
    if moment_id is not None:
        row = conn.execute("SELECT rank FROM moments WHERE id = ?", (moment_id,)).fetchone()
        if row is None:
            return StageOutcome("error", "moment no longer exists (the video was re-scored)")
        if stage in ("render", "short"):
            return _render(cfg, conn, moment_id, say, kind="long" if stage == "render" else "short")
        if stage == "intro":
            return _intro(cfg, conn, moment_id, say)
        return {"clip": _clip, "transcribe": _transcribe, "analyze": _analyze, "upload": _upload}[stage](
            cfg, conn, video_id, attempt, deps, say, rank=row["rank"], **_opts(stage, params))
    fn = {"ingest": _ingest, "score": _score, "clip": _clip, "transcribe": _transcribe, "frames": _frames,
          "analyze": _analyze, "upload": _upload}[stage]
    return fn(cfg, conn, video_id, attempt, deps, say, **_opts(stage, params))


def _opts(stage: str, params: dict[str, Any]) -> dict[str, Any]:
    """The job options each stage understands; anything else is ignored."""
    keys = {"analyze": ("model", "effort", "redo"), "upload": ("kept_only",),
            "clip": ("kept_only",)}.get(stage, ())
    return {k: params[k] for k in keys if params.get(k) is not None}


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


def _clip(cfg, conn, video_id, attempt, deps, say, rank: int | None = None,
          kept_only: bool = False) -> StageOutcome:
    """Automatic: the best clip_top moments. rank: redo that one. kept_only: every moment
    marked Keep that isn't downloaded yet ("Clip kept" on the stream page)."""
    from chatwatcher.clip.service import ClipError, clip_video
    top = None if (rank or kept_only) else (cfg.pipeline.clip_top or None)
    ranks = [rank] if rank else None
    if kept_only:
        ranks = [r["rank"] for r in conn.execute(
            "SELECT m.rank FROM moments m JOIN videos v ON v.id = m.video_id "
            "WHERE v.video_id = ? AND m.status = 'approved' ORDER BY m.rank", (video_id,))]
        if not ranks:
            return StageOutcome("done", "nothing to clip: no moments are marked Keep", details={"stop": True})
    try:
        run = clip_video(cfg, conn, video_id, top=top, ytdlp=deps.ytdlp,
                         ranks=ranks, force=bool(rank),
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


def _transcribe(cfg, conn, video_id, attempt, deps, say, rank: int | None = None) -> StageOutcome:
    """Whisper on each downloaded clip that has no words yet; rank: (re)do that one clip.
    Between clips it gives way to a render you queued, then carries on."""
    from chatwatcher.subs.transcribe import TranscribeError, Whisper, available, transcribe_clip, WORDS_FILE
    if deps.whisper is None and not available():
        return StageOutcome("skip", "Whisper isn't installed (pip install faster-whisper)")
    rows = conn.execute(
        """SELECT m.rank, c.path, c.folder FROM clips c JOIN moments m ON m.id = c.moment_id
           JOIN videos v ON v.id = m.video_id WHERE v.video_id = ?""" + (" AND m.rank = ?" if rank else "")
        + " ORDER BY m.rank", (video_id, rank) if rank else (video_id,)).fetchall()
    todo = [r for r in rows if Path(r["path"]).is_file()
            and (rank or not (Path(r["folder"] or Path(r["path"]).parent) / WORDS_FILE).is_file())]
    if not todo:
        return StageOutcome("done", "nothing to transcribe" if not rows else "every clip already transcribed")
    try:
        engine = deps.whisper or Whisper(cfg)
    except TranscribeError as e:
        return StageOutcome("error", str(e))
    done, failed, langs = 0, [], set()
    for i, r in enumerate(todo):
        if i and conn.execute("SELECT 1 FROM jobs WHERE stage IN ('render', 'short') AND status = 'pending' "
                              "AND run_after <= datetime('now') LIMIT 1").fetchone():
            return StageOutcome("retry", f"{done} transcribed; paused for a render, the rest follow",
                                timedelta(seconds=1))
        clip = Path(r["path"])
        try:
            d = transcribe_clip(cfg, Path(r["folder"] or clip.parent), clip, engine=engine,
                                say=lambda m, k=r["rank"]: say(f"#{k}: {m}"))
        except TranscribeError as e:
            failed.append(f"#{r['rank']} {e}")
            continue
        done += 1
        langs.add(d["language"])
        say(f"#{r['rank']}: {len(d['lines'])} lines in {d['took_s']:.0f}s ({d['language']})")
    msg = f"{done} transcribed" + (f" ({', '.join(sorted(l for l in langs if l))})" if langs else "")
    if failed:
        msg += f", {len(failed)} failed: " + "; ".join(failed)[:400]
    return StageOutcome("done" if done or not failed else "error", msg)


def _frames(cfg, conn, video_id, attempt, deps, say) -> StageOutcome:
    """Low-res previews of the moments that aren't downloaded, for Claude."""
    from chatwatcher.clip.preview import PreviewError, preview_video
    try:
        run = preview_video(cfg, conn, video_id, ytdlp=deps.ytdlp,
                            on_event=lambda r, m: say(f"#{r}: {m}" if r else m))
    except PreviewError as e:
        if attempt < 4:
            return StageOutcome("retry", str(e), _backoff(attempt, 15, 120))
        return StageOutcome("error", str(e))
    msg = f"{len(run.done)} preview(s)" + (f", {run.skipped} already there" if run.skipped else "")
    if run.errors:
        msg += f", {len(run.errors)} failed: " + "; ".join(f"#{r} {m}" for r, m in run.errors[:3])
        if not run.done and not run.skipped and attempt < 4:
            return StageOutcome("retry", msg, _backoff(attempt, 15, 120))
    return StageOutcome("done", msg)


def _analyze(cfg, conn, video_id, attempt, deps, say, rank: int | None = None,
             model: str | None = None, effort: str | None = None, redo: bool = False) -> StageOutcome:
    from chatwatcher.analyze.service import AnalyzeError, analyze_video
    if not cfg.claude.enabled:
        return StageOutcome("skip", "claude.enabled is false")
    top = None if rank else (cfg.pipeline.analyze_top or None)
    try:
        run = analyze_video(cfg, conn, video_id, top=top, runner=deps.claude,
                            ranks=[rank] if rank else None, force=bool(rank) or redo,
                            digest=not rank,   # one moment redone: keep the digest (and the plan) as is
                            model=model, effort=effort,
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
        from chatwatcher.analyze.service import CAP_SETTING, cap_wait
        wait = (max(cap_wait(cfg, conn), timedelta(minutes=2)) if CAP_SETTING in reason
                else usage_reset_delay(reason))
        return StageOutcome("retry", f"{msg}; {reason[:200]}", wait)
    if errors and not done and attempt < 3:
        return StageOutcome("retry", msg, timedelta(minutes=30))
    if run.digest_error:
        msg += f"; digest failed: {run.digest_error[:150]}"
    return StageOutcome("done" if (done or not errors) else "error", msg)


def _upload(cfg, conn, video_id, attempt, deps, say, rank: int | None = None,
            kept_only: bool = False) -> StageOutcome:
    """The whole stream folder; or one clip (rank); or only the clips marked Keep."""
    from chatwatcher.upload.drive import UploadError, clip_folders, upload_video
    if not cfg.drive.enabled:
        return StageOutcome("skip", "drive.enabled is false")
    folders = None
    if rank or kept_only:
        folders = clip_folders(conn, video_id, ranks=[rank] if rank else None, kept_only=kept_only)
        if not folders:
            return StageOutcome("done", "nothing to upload: " + ("that clip isn't downloaded yet" if rank
                                                              else "no downloaded clips are marked Keep"))
    try:
        r = upload_video(cfg, conn, video_id, rclone=deps.rclone, folders=folders,
                         on_line=lambda m: say(f"rclone: {m}"))
    except UploadError as e:
        if e.kind == "nothing":
            return StageOutcome("done", str(e))
        if e.kind in ("missing", "no_remote", "auth") and attempt < 48:
            return StageOutcome("retry", str(e), timedelta(hours=1))   # resumes once rclone is set up
        if e.kind in ("network", "rate_limited", "error") and attempt < 6:
            return StageOutcome("retry", str(e), _backoff(attempt, 10, 180))
        return StageOutcome("error", str(e))
    what = f"{len(folders)} clip(s)" if folders else "uploaded"
    return StageOutcome("done", f"{what} to {r.dest}" + (f" ({r.stats})" if r.stats else ""))


def _render(cfg, conn, moment_id: int, say, kind: str = "long") -> StageOutcome:
    """Your edit from the dashboard editor → edit.mp4 (a Short: short.mp4) next to clip.mp4."""
    from chatwatcher.clip.media import MediaError
    from chatwatcher.edit.service import EditError, render_moment
    try:
        r = render_moment(cfg, conn, moment_id, kind=kind)
    except (EditError, MediaError) as e:
        return StageOutcome("error", str(e))
    what = f"Short rendered ({r['layout']['type']})" if kind == "short" else "edit rendered"
    msg = f"{what}: {r['duration_s']:.1f}s, {r['size_bytes'] / 1_048_576:.1f} MB"
    if r["sounds"]:
        msg += f", {r['sounds']} sound effect(s)"
    if r.get("visuals"):
        msg += f", {r['visuals']} zoom/shake/flash"
    if r.get("subtitles"):
        msg += f", {r['subtitles']} subtitle line(s)"
    if r["missing"]:
        msg += f"; skipped sounds no longer in the library: {', '.join(r['missing'])}"
    if r.get("missing_regions"):
        msg += f"; zoomed on the centre, no region called {', '.join(r['missing_regions'])}"
    if r.get("intro_s"):
        msg += f", starting with the {r['intro_s']:.1f}s intro"
    return StageOutcome("done", msg)


def _intro(cfg, conn, moment_id: int, say) -> StageOutcome:
    """The brand intro (teasers from the clip → logo) → intro.mp4 next to clip.mp4."""
    from chatwatcher.brand.service import render_intro
    from chatwatcher.clip.media import MediaError
    from chatwatcher.edit.service import EditError
    try:
        r = render_intro(cfg, conn, moment_id)
    except (EditError, MediaError) as e:
        return StageOutcome("error", str(e))
    return StageOutcome("done", f"intro rendered: {r['duration_s']:.1f}s, {len(r['teasers'])} teaser(s), "
                                f"{r['size_bytes'] / 1_048_576:.1f} MB")


# ── bookkeeping shared by the worker and `cw run` ─────────────────────────────

def stage_enabled(cfg: Config, stage: str, conn: sqlite3.Connection | None = None) -> bool:
    """Does this stage run automatically? (conn: read the dashboard's auto-upload switch.)"""
    if stage == "clip":
        return cfg.pipeline.auto_clip
    if stage == "transcribe":
        from chatwatcher.subs.transcribe import available
        return cfg.transcribe.enabled and cfg.transcribe.auto and available()
    if stage == "frames":
        return bool(cfg.pipeline.preview_height) and cfg.pipeline.auto_analyze and cfg.claude.enabled
    if stage == "analyze":
        return cfg.pipeline.auto_analyze and cfg.claude.enabled
    if stage == "upload":
        from chatwatcher import prefs
        return cfg.drive.enabled and prefs.auto_upload(cfg, conn)
    return True


def set_video_status(conn: sqlite3.Connection, video_id: str, status: str, error: str | None = None) -> None:
    store.upsert_video(conn, video_id, status=status, error=error)
