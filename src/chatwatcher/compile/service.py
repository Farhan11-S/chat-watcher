"""Read one whole stream with exactly one Claude call ("✨ Read the whole stream").

Input: what was said — Whisper's transcript of the stream's audio when you ticked "Listen
first" (compile.speech, made by this same job before the call), otherwise the captions
(ingest saved them as transcript.jsonl; fetched now if YouTube hadn't made them yet) —
its chat, thinned, and the moments already found. Output, replacing what an earlier read
found: compilation ideas (`compilations`), the stream's context (`compile_runs.context_json`,
given to every clip analysis afterwards: context_text) and moments chat missed, which
become moments of kind "missed" (ranks 501, 502, …) that you review like any other.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable

from chatwatcher import db, store
from chatwatcher.analyze.claude_runner import ClaudeError, ClaudeRunner
from chatwatcher.compile.parse import parse_context, parse_missed, parse_result
from chatwatcher.compile.prompt import SCHEMA, SYSTEM_PROMPT, build
from chatwatcher.config import Config
from chatwatcher.ingest.chat_parser import read_messages
from chatwatcher.ingest.ytdlp import extract_video_id
from chatwatcher.paths import vod_files

StepFn = Callable[[str], None]
MIN_TIMEOUT_S = 1200             # one long read of a whole stream; clip analysis needs far less
MISSED = "missed"                # moment kind of what the read found that chat didn't react to
MISSED_RANK_BASE = 500           # missed moments are #501, #502, …; compilations #1001, …


class CompileError(Exception):
    """kind: not_ingested | no_captions | too_long | disabled | auth | missing |
    usage_limit | bad_output | timeout | error, a yt-dlp kind when fetching captions, or a
    compile.speech kind (no_audio | download) when listening."""

    def __init__(self, message: str, *, kind: str = "error", retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


@dataclass
class CompileRun:
    video_id: str
    candidates: list[dict[str, Any]] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)   # parse.parse_context
    missed: list[dict[str, Any]] = field(default_factory=list)   # parse.parse_missed + moment_id / same_as
    source: str = "captions"                                # what was read: captions | whisper
    size: dict[str, int] = field(default_factory=dict)      # prompt.Built.size
    usage: dict[str, int] = field(default_factory=dict)     # tokens the CLI reported
    model: str | None = None

    @property
    def input_tokens(self) -> int:
        """What Claude actually read, per the CLI (0 when it reported nothing)."""
        return sum(self.usage.get(k, 0) for k in ("input", "cache_read", "cache_write"))


def compile_video(cfg: Config, conn: sqlite3.Connection, url_or_id: str, *,
                  runner: ClaudeRunner | None = None, ytdlp: Any = None,
                  on_step: StepFn | None = None, listen: bool = False, whisper: Any = None,
                  should_pause: Callable[[], bool] = lambda: False) -> CompileRun | None:
    """listen: transcribe the stream's audio first (unless done before). Returns None when
    the transcription stopped to let other work go first: call again to carry on."""
    say = on_step or (lambda _m: None)
    if not cfg.claude.enabled:
        raise CompileError("claude.enabled is false in the config.", kind="disabled")
    try:
        video_id = extract_video_id(url_or_id)
    except ValueError as e:
        raise CompileError(str(e)) from e
    video = store.get_video(conn, video_id)
    files = vod_files(cfg, video_id)
    if video is None or not files.info.is_file():
        raise CompileError("This stream hasn't been fetched yet.", kind="not_ingested")

    info = json.loads(files.info.read_text(encoding="utf-8"))
    duration = float(info.get("duration") or video["duration_s"] or 0)
    if listen and not files.speech.is_file():
        from chatwatcher.compile.speech import SpeechError, transcribe_stream
        try:
            if not transcribe_stream(cfg, files, video_id, duration, ytdlp=ytdlp, engine=whisper,
                                     should_pause=should_pause, say=say):
                return None
        except SpeechError as e:
            raise CompileError(str(e), kind=e.kind, retryable=e.retryable) from e
    if not files.speech.is_file() and not files.transcript.is_file():
        _fetch_captions(cfg, conn, video_id, ytdlp, say)
    from chatwatcher.compile.speech import refresh_excerpts, stream_transcript
    segments, source = stream_transcript(files)
    if listen and source == "whisper":
        n = refresh_excerpts(conn, video["id"], segments)
        if n:
            say(f"{n} clip/preview transcript(s) now use Whisper's text")
    if not segments:
        raise CompileError(
            "YouTube has no captions for this stream yet, so there is nothing to read. Auto-captions "
            "usually appear a few hours after a stream ends (some streams never get them). Try again later.",
            kind="no_captions")
    messages = read_messages(files.chat) if files.chat.is_file() else []

    built = build(info, segments, messages, output_language=cfg.claude.output_language,
                  known=known_moments(conn, video["id"]), source=source, max_missed=cfg.compile.max_missed)
    if built is None:
        raise CompileError("This stream is too long to read in one go, even without chat.", kind="too_long")
    say(f"input ({'Whisper transcript' if source == 'whisper' else 'captions'}): {built.chars:,} characters, about {built.tokens:,} tokens "
        f"({built.speech_lines:,} transcript lines, {built.chat_lines:,} chat lines"
        + (f", detail level {built.level}" if built.level else "") + ")")

    try:
        runner = runner or ClaudeRunner(cfg.claude.model_copy(
            update={"timeout_s": max(cfg.claude.timeout_s, MIN_TIMEOUT_S)}))
    except ClaudeError as e:
        raise CompileError(str(e), kind=e.kind) from e
    status = runner.auth_status()
    if status and status.get("loggedIn") is False:
        raise CompileError("The claude CLI isn't logged in. Run `claude auth login` once in a "
                           "terminal (it uses your Claude subscription).", kind="auth")
    say("asking Claude (one call)…")
    try:                             # no retry here: one stream, one call
        res = runner.run(built.prompt, SCHEMA, system=SYSTEM_PROMPT, cwd=files.root)
    except ClaudeError as e:
        raise CompileError(str(e), kind=e.kind) from e

    run = CompileRun(video_id, parse_result(res.data, duration), parse_context(res.data, duration),
                     parse_missed(res.data, duration, cfg.compile.max_missed), source, built.size,
                     res.usage, res.model)
    if run.input_tokens:
        say(f"Claude read {run.input_tokens:,} tokens and wrote {run.usage.get('output', 0):,}")
    save(conn, video["id"], run, cfg)
    return run


def _fetch_captions(cfg: Config, conn: sqlite3.Connection, video_id: str, ytdlp: Any, say: StepFn) -> None:
    from chatwatcher.ingest.service import refresh_captions
    from chatwatcher.ingest.ytdlp import YtDlpError
    say("no captions saved; asking YouTube again…")
    try:
        n = refresh_captions(cfg, conn, video_id, ytdlp=ytdlp, on_step=say)
    except YtDlpError as e:
        raise CompileError(f"Couldn't fetch captions: {e}", kind=e.kind, retryable=e.retryable) from e
    if n:
        say(f"captions fetched: {n:,} lines")


def known_moments(conn: sqlite3.Connection, video_pk: int) -> list[dict[str, Any]]:
    """The stream's moments (not compilation clips), for the prompt: {start_s, end_s, title}
    — Claude's title when analysed, else what made it a moment."""
    out = []
    for m in conn.execute(
            """SELECT m.start_s, m.end_s, m.kind, m.features_json,
                      (SELECT a.analysis_json FROM analyses a WHERE a.moment_id = m.id ORDER BY a.id DESC LIMIT 1) AS a
               FROM moments m WHERE m.video_id = ? AND m.kind != 'compilation' ORDER BY m.start_s""", (video_pk,)):
        f = json.loads(m["features_json"] or "{}")
        a = json.loads(m["a"]) if m["a"] else {}
        title = ((a.get("titles") or [{}])[0] or {}).get("text") or (f.get(MISSED) or {}).get("title") \
            or ", ".join(r["signal"] for r in (f.get("reasons") or [])[:2]) or m["kind"]
        out.append({"start_s": m["start_s"], "end_s": m["end_s"], "title": title})
    return out


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    """How much of the shorter range the other one covers, 0-1."""
    inter = min(a[1], b[1]) - max(a[0], b[0])
    return max(0.0, inter) / max(1e-6, min(a[1] - a[0], b[1] - b[0]))


def add_missed(conn: sqlite3.Connection, video_pk: int, missed: list[dict[str, Any]]) -> None:
    """Each moment chat missed becomes a moment (kind "missed", status new), unless it is
    mostly a moment the stream already has; then it gets "same_as": that rank. Sets
    "moment_id" on the ones added."""
    have = [(m["start_s"], m["end_s"], m["rank"]) for m in conn.execute(
        "SELECT start_s, end_s, rank FROM moments WHERE video_id = ? AND kind != 'compilation'", (video_pk,))]
    rank = max(MISSED_RANK_BASE, conn.execute(
        "SELECT COALESCE(MAX(rank), 0) FROM moments WHERE video_id = ? AND kind = ?",
        (video_pk, MISSED)).fetchone()[0])
    for c in missed:
        same = next((r for a, b, r in have if _overlap((a, b), (c["start"], c["end"])) > 0.5), None)
        if same is not None:
            c["same_as"] = same
            continue
        rank += 1
        features = {"reasons": [{"signal": "heard", "z": float(c["rating"])}], "top_messages": [],
                    MISSED: {k: c[k] for k in ("title", "category", "why", "rating")}}
        c["moment_id"] = conn.execute(
            """INSERT INTO moments (video_id, rank, score, start_s, end_s, peak_s, kind, features_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (video_pk, rank, float(c["rating"]) / 4, c["start"], c["end"], c["peak"], MISSED,
             json.dumps(features, ensure_ascii=False))).lastrowid
        have.append((c["start"], c["end"], rank))


def save(conn: sqlite3.Connection, video_pk: int, run: CompileRun, cfg: Config | None = None) -> int:
    """Store the run, its candidates, its context and the moments chat missed; the stream's
    earlier run goes (moments made from it stay: you may have reviewed them)."""
    with db.tx(conn):
        add_missed(conn, video_pk, run.missed)
        conn.execute("DELETE FROM compile_runs WHERE video_id = ?", (video_pk,))
        run_id = conn.execute(
            """INSERT INTO compile_runs (video_id, model, input_json, usage_json, context_json, missed_json, source)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (video_pk, run.model, json.dumps(run.size), json.dumps(run.usage),
             json.dumps(run.context, ensure_ascii=False), json.dumps(run.missed, ensure_ascii=False),
             run.source)).lastrowid
        conn.executemany(
            """INSERT INTO compilations (run_id, video_id, position, title, kind, reason, rating, segments_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [(run_id, video_pk, i, c["title"], c["kind"], c["reason"], c["rating"],
              json.dumps(c["segments"], ensure_ascii=False)) for i, c in enumerate(run.candidates, 1)])
    return run_id


def load(conn: sqlite3.Connection, video_pk: int) -> dict[str, Any] | None:
    """The stream's latest run for the dashboard: {model, created_at, input, usage,
    candidates: [{title, kind, reason, rating, segments, length_s}]}; None = never run."""
    r = conn.execute("SELECT * FROM compile_runs WHERE video_id = ? ORDER BY id DESC LIMIT 1",
                     (video_pk,)).fetchone()
    if r is None:
        return None
    candidates = []
    for c in conn.execute("SELECT * FROM compilations WHERE run_id = ? ORDER BY position", (r["id"],)):
        segments = json.loads(c["segments_json"])
        candidates.append({"id": c["id"], "title": c["title"], "kind": c["kind"], "reason": c["reason"],
                           "rating": c["rating"], "segments": segments,
                           "length_s": sum(s["end"] - s["start"] for s in segments)})
    return {"model": r["model"], "created_at": r["created_at"], "input": json.loads(r["input_json"]),
            "usage": json.loads(r["usage_json"] or "{}"), "candidates": candidates,
            "context": json.loads(r["context_json"] or "{}"), "missed": json.loads(r["missed_json"] or "[]"),
            "source": r["source"] or "captions"}


def context(conn: sqlite3.Connection, video_pk: int) -> dict[str, Any]:
    """The stream's context from its latest read ({} when never read)."""
    r = conn.execute("SELECT context_json FROM compile_runs WHERE video_id = ? ORDER BY id DESC LIMIT 1",
                     (video_pk,)).fetchone()
    return json.loads(r["context_json"] or "{}") if r else {}


def context_text(ctx: dict[str, Any], start_s: float | None = None, end_s: float | None = None) -> str:
    """The context as a few lines for a clip's analysis: the summary, the chapters around
    the clip (start_s/end_s in stream time; left out when not given, e.g. a compilation
    clip, whose clock is its own), the running jokes and the names. "" when there is none."""
    if not ctx or not (ctx.get("summary") or ctx.get("chapters")):
        return ""
    from chatwatcher.compile.prompt import stamp
    parts = [f"The whole stream: {ctx['summary']}"] if ctx.get("summary") else []
    if start_s is not None and end_s is not None:
        near = [c for c in ctx.get("chapters") or [] if c["end"] >= start_s - 300 and c["start"] <= end_s + 120]
        if near:
            parts.append("Around this clip:\n" + "\n".join(
                f"- {stamp(c['start'])}-{stamp(c['end'])} {c['text']}" + (" ← this clip" if c["start"] <= start_s < c["end"] else "")
                for c in near))
    if ctx.get("jokes"):
        parts.append("Running jokes: " + "; ".join(ctx["jokes"]))
    if ctx.get("names"):
        parts.append("Names (right spellings): " + "; ".join(ctx["names"]))
    return "\n".join(parts)
