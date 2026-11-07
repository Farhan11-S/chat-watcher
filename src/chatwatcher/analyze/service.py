"""Analyze clipped moments with Claude: analysis.json + storyboard.md per clip, plus a
stream digest. One Claude call at a time, with a daily cap, pausing on usage limits."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from chatwatcher import store
from chatwatcher.analyze.claude_runner import ClaudeError, ClaudeResult, ClaudeRunner
from chatwatcher.analyze.prompts import (
    DIGEST_SCHEMA, MOMENT_SCHEMA, SYSTEM_PROMPT, digest_prompt, moment_prompt,
)
from chatwatcher.analyze.storyboard import render_digest, render_moment
from chatwatcher.config import Config
from chatwatcher.ingest.ytdlp import extract_video_id
from chatwatcher.paths import vod_files

EventFn = Callable[[int, str], None]


class AnalyzeError(Exception):
    def __init__(self, message: str, *, kind: str = "error"):
        super().__init__(message)
        self.kind = kind


@dataclass
class AnalyzeOutcome:
    rank: int
    status: str            # done | skipped | deferred | error
    message: str = ""
    title: str = ""
    worthiness: int | None = None
    category: str | None = None
    folder: Path | None = None


@dataclass
class AnalyzeRun:
    video_id: str
    outcomes: list[AnalyzeOutcome] = field(default_factory=list)
    digest_path: Path | None = None
    digest_error: str | None = None
    calls: int = 0
    usage: dict[str, int] = field(default_factory=lambda: {"input": 0, "cache_read": 0,
                                                            "cache_write": 0, "output": 0})

    def add_usage(self, res: ClaudeResult) -> None:
        self.calls += 1
        for k, v in res.usage.items():
            self.usage[k] = self.usage.get(k, 0) + v

    def count(self, status: str) -> int:
        return sum(o.status == status for o in self.outcomes)


def _rows(conn: sqlite3.Connection, video_pk: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT m.id, m.rank, m.kind, c.folder,
               (SELECT a.analysis_json FROM analyses a WHERE a.moment_id = m.id
                ORDER BY a.id DESC LIMIT 1) AS analysis_json,
               (SELECT a.model FROM analyses a WHERE a.moment_id = m.id
                ORDER BY a.id DESC LIMIT 1) AS model
        FROM moments m LEFT JOIN clips c ON c.moment_id = m.id
        WHERE m.video_id = ? AND m.status != 'rejected'
        ORDER BY m.rank
        """,
        (video_pk,),
    ).fetchall()


def analyses_today(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM analyses WHERE moment_id IS NOT NULL AND date(created_at) = date('now')"
    ).fetchone()[0]


def _call(runner: ClaudeRunner, *args, attempts: int = 2, **kwargs) -> ClaudeResult:
    """Retry once on flaky failures; usage limits and auth problems are raised at once."""
    last: ClaudeError | None = None
    for _ in range(attempts):
        try:
            return runner.run(*args, **kwargs)
        except ClaudeError as e:
            if e.kind in ("usage_limit", "auth", "missing"):
                raise
            last = e
    assert last is not None
    raise last


def sanitize(a: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    """Clamp times to the clip and make sure the thumbnail points at a real frame."""
    dur = float(meta.get("clip", {}).get("duration_s") or 0)

    def clamp(v: Any) -> Any:
        return None if v is None else round(min(max(float(v), 0.0), dur), 1) if dur else v

    cut = a.get("cut") or {}
    for k in ("long_in", "long_out", "short_in", "short_out"):
        if k in cut:
            cut[k] = clamp(cut[k])
    if cut.get("long_in") is not None and cut.get("long_out") is not None and cut["long_out"] <= cut["long_in"]:
        cut["long_in"], cut["long_out"] = 0.0, dur
    if (cut.get("short_in") is None) != (cut.get("short_out") is None):
        cut["short_in"] = cut["short_out"] = None
    for b in a.get("storyboard") or []:
        b["start"], b["end"] = clamp(b.get("start", 0)), clamp(b.get("end", 0))
    for c in a.get("captions") or []:
        c["at"] = clamp(c.get("at", 0))
    frames = [f["file"] for f in meta.get("frames", [])]
    thumb = a.setdefault("thumbnail", {})
    if frames and thumb.get("frame_file") not in frames:
        peak = next((f["file"] for f in meta["frames"] if f.get("is_peak")), frames[len(frames) // 2])
        thumb["frame_file"] = peak
    return a


def analyze_video(
    cfg: Config,
    conn: sqlite3.Connection,
    url_or_id: str,
    *,
    ranks: list[int] | None = None,
    top: int | None = None,
    force: bool = False,
    digest: bool = True,
    runner: ClaudeRunner | None = None,
    on_event: EventFn | None = None,
) -> AnalyzeRun:
    event = on_event or (lambda _r, _m: None)
    if not cfg.claude.enabled:
        raise AnalyzeError("claude.enabled is false in the config.", kind="disabled")
    try:
        video_id = extract_video_id(url_or_id)
    except ValueError as e:
        raise AnalyzeError(str(e)) from e
    video = store.get_video(conn, video_id)
    files = vod_files(cfg, video_id)
    if video is None or not files.info.is_file():
        raise AnalyzeError(f"{video_id} hasn't been ingested yet. Run `cw ingest {video_id}` first.")
    rows = _rows(conn, video["id"])
    if not rows:
        raise AnalyzeError(f"No moments for {video_id}. Run `cw score` and `cw clip` first.")
    selected = [r for r in rows if not ranks or r["rank"] in ranks]
    if top:
        selected = selected[:top]

    try:
        runner = runner or ClaudeRunner(cfg.claude)
    except ClaudeError as e:
        raise AnalyzeError(str(e), kind=e.kind) from e
    status = runner.auth_status()
    if status and status.get("loggedIn") is False:
        raise AnalyzeError("The claude CLI isn't logged in. Run `claude auth login` once in a "
                           "terminal (it uses your Claude subscription).", kind="auth")

    run = AnalyzeRun(video_id=video_id)
    cap = cfg.claude.daily_moment_cap
    used_today = analyses_today(conn)
    stop_reason: str | None = None
    new_analyses = 0

    for r in selected:
        folder = Path(r["folder"]) if r["folder"] else None
        if folder is None or not (folder / "moment.json").is_file():
            run.outcomes.append(AnalyzeOutcome(r["rank"], "skipped", "not clipped yet (run `cw clip`)"))
            continue
        if r["analysis_json"] and (folder / "storyboard.md").is_file() and not force:
            a = json.loads(r["analysis_json"])
            run.outcomes.append(_outcome(r["rank"], "skipped", a, folder, "already analysed"))
            continue
        if stop_reason:
            run.outcomes.append(AnalyzeOutcome(r["rank"], "deferred", stop_reason, folder=folder))
            continue
        if cap and used_today >= cap:
            stop_reason = f"daily cap of {cap} analyses reached (claude.daily_moment_cap)"
            run.outcomes.append(AnalyzeOutcome(r["rank"], "deferred", stop_reason, folder=folder))
            continue

        event(r["rank"], "asking Claude…")
        meta = json.loads((folder / "moment.json").read_text(encoding="utf-8"))
        prompt = moment_prompt(folder, output_language=cfg.claude.output_language,
                               title_languages=cfg.claude.title_languages)
        try:
            res = _call(runner, prompt, MOMENT_SCHEMA, system=SYSTEM_PROMPT, cwd=folder,
                        allow_read=True)
        except ClaudeError as e:
            if e.kind in ("auth", "missing"):
                raise AnalyzeError(str(e), kind=e.kind) from e
            if e.kind == "usage_limit":
                stop_reason = f"Claude usage limit reached — run again later ({e})"
                run.outcomes.append(AnalyzeOutcome(r["rank"], "deferred", stop_reason, folder=folder))
                event(r["rank"], "usage limit reached; stopping")
                continue
            run.outcomes.append(AnalyzeOutcome(r["rank"], "error", str(e), folder=folder))
            event(r["rank"], f"failed: {e}")
            continue

        run.add_usage(res)
        a = sanitize(res.data, meta)
        a["_meta"] = {"model": res.model, "duration_ms": res.duration_ms, "usage": res.usage}
        meta["model"] = res.model
        (folder / "analysis.json").write_text(json.dumps(a, ensure_ascii=False, indent=1), encoding="utf-8")
        (folder / "storyboard.md").write_text(render_moment(a, meta), encoding="utf-8")
        conn.execute("INSERT INTO analyses (video_id, moment_id, model, analysis_json) VALUES (?, ?, ?, ?)",
                     (video["id"], r["id"], res.model, json.dumps(a, ensure_ascii=False)))
        used_today += 1
        new_analyses += 1
        run.outcomes.append(_outcome(r["rank"], "done", a, folder))
        event(r["rank"], "done")

    if digest and not stop_reason:
        _digest(cfg, conn, video, run, runner, force=force or new_analyses > 0, event=event)
    return run


def _outcome(rank: int, status: str, a: dict[str, Any], folder: Path, msg: str = "") -> AnalyzeOutcome:
    titles = a.get("titles") or []
    return AnalyzeOutcome(rank, status, msg, title=titles[0]["text"] if titles else "",
                          worthiness=a.get("clip_worthiness"), category=a.get("category"),
                          folder=folder)


def _digest(cfg: Config, conn: sqlite3.Connection, video: sqlite3.Row, run: AnalyzeRun,
            runner: ClaudeRunner, *, force: bool, event: EventFn) -> None:
    out_dir = Path(video["output_dir"]) if video["output_dir"] else None
    rows = _rows(conn, video["id"])
    analysed = []
    for r in rows:
        if r["analysis_json"] and r["folder"] and (Path(r["folder"]) / "moment.json").is_file():
            meta = json.loads((Path(r["folder"]) / "moment.json").read_text(encoding="utf-8"))
            analysed.append({"rank": r["rank"], "analysis": json.loads(r["analysis_json"]),
                             "stream_range": "–".join(meta["stream_range"]),
                             "folder": Path(r["folder"]).name})
    if out_dir is None or len(analysed) < 2:
        return
    md = out_dir / "stream_digest.md"
    if md.is_file() and not force:
        run.digest_path = md
        return
    info = json.loads(vod_files(cfg, video["video_id"]).info.read_text(encoding="utf-8"))
    stream = {"video_id": video["video_id"], "title": info.get("title"), "channel": info.get("channel"),
              "stream_type": video["stream_type"] if video["stream_type"] != "auto" else video["detected_type"]}
    event(0, "writing stream digest…")
    try:
        res = _call(runner, digest_prompt(stream, analysed, output_language=cfg.claude.output_language,
                                          title_languages=cfg.claude.title_languages),
                    DIGEST_SCHEMA, system=SYSTEM_PROMPT, cwd=out_dir, allow_read=False)
    except ClaudeError as e:
        run.digest_error = str(e)
        return
    run.add_usage(res)
    valid = {a["rank"] for a in analysed}
    d = res.data
    for k in ("highlights_order", "best_shorts", "skip"):
        d[k] = [r for r in d.get(k) or [] if r in valid]
    (out_dir / "stream_digest.json").write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    md.write_text(render_digest(d, stream, {a["rank"]: a for a in analysed}), encoding="utf-8")
    conn.execute("INSERT INTO analyses (video_id, moment_id, model, analysis_json) VALUES (?, NULL, ?, ?)",
                 (video["id"], res.model, json.dumps(d, ensure_ascii=False)))
    run.digest_path = md
