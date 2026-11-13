"""Find compilation material in one stream with exactly one Claude call.

Input: the stream's captions (ingest saved them as transcript.jsonl; fetched now if
YouTube hadn't made them yet at ingest) and its chat, thinned. Output: rows in
`compilations`, replacing what an earlier run found for the stream.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable

from chatwatcher import db, store
from chatwatcher.analyze.claude_runner import ClaudeError, ClaudeRunner
from chatwatcher.compile.parse import parse_result
from chatwatcher.compile.prompt import SCHEMA, SYSTEM_PROMPT, build
from chatwatcher.config import Config
from chatwatcher.ingest.captions import read_transcript
from chatwatcher.ingest.chat_parser import read_messages
from chatwatcher.ingest.ytdlp import extract_video_id
from chatwatcher.paths import vod_files

StepFn = Callable[[str], None]
MIN_TIMEOUT_S = 1200             # one long read of a whole stream; clip analysis needs far less


class CompileError(Exception):
    """kind: not_ingested | no_captions | too_long | disabled | auth | missing |
    usage_limit | bad_output | timeout | error, or a yt-dlp kind when fetching captions."""

    def __init__(self, message: str, *, kind: str = "error", retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


@dataclass
class CompileRun:
    video_id: str
    candidates: list[dict[str, Any]] = field(default_factory=list)
    size: dict[str, int] = field(default_factory=dict)      # prompt.Built.size
    usage: dict[str, int] = field(default_factory=dict)     # tokens the CLI reported
    model: str | None = None

    @property
    def input_tokens(self) -> int:
        """What Claude actually read, per the CLI (0 when it reported nothing)."""
        return sum(self.usage.get(k, 0) for k in ("input", "cache_read", "cache_write"))


def compile_video(cfg: Config, conn: sqlite3.Connection, url_or_id: str, *,
                  runner: ClaudeRunner | None = None, ytdlp: Any = None,
                  on_step: StepFn | None = None) -> CompileRun:
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

    if not files.transcript.is_file():
        _fetch_captions(cfg, conn, video_id, ytdlp, say)
    segments = read_transcript(files.transcript) if files.transcript.is_file() else []
    if not segments:
        raise CompileError(
            "YouTube has no captions for this stream yet, so there is nothing to read. Auto-captions "
            "usually appear a few hours after a stream ends (some streams never get them). Try again later.",
            kind="no_captions")
    messages = read_messages(files.chat) if files.chat.is_file() else []
    info = json.loads(files.info.read_text(encoding="utf-8"))
    duration = float(info.get("duration") or video["duration_s"] or 0)

    built = build(info, segments, messages, output_language=cfg.claude.output_language)
    if built is None:
        raise CompileError("This stream is too long to read in one go, even without chat.", kind="too_long")
    say(f"input: {built.chars:,} characters, about {built.tokens:,} tokens "
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

    run = CompileRun(video_id, parse_result(res.data, duration), built.size, res.usage, res.model)
    if run.input_tokens:
        say(f"Claude read {run.input_tokens:,} tokens and wrote {run.usage.get('output', 0):,}")
    save(conn, video["id"], run)
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


def save(conn: sqlite3.Connection, video_pk: int, run: CompileRun) -> int:
    """Store the run and its candidates; the stream's earlier run goes."""
    with db.tx(conn):
        conn.execute("DELETE FROM compile_runs WHERE video_id = ?", (video_pk,))
        run_id = conn.execute(
            "INSERT INTO compile_runs (video_id, model, input_json, usage_json) VALUES (?, ?, ?, ?)",
            (video_pk, run.model, json.dumps(run.size), json.dumps(run.usage))).lastrowid
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
            "usage": json.loads(r["usage_json"] or "{}"), "candidates": candidates}
