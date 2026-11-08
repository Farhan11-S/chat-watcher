"""Run Claude headlessly through the `claude` CLI, i.e. on the user's Claude subscription.

Never `--bare`: bare mode only accepts ANTHROPIC_API_KEY. Unless allowed in config, the
API key variables are removed from the environment so the CLI can't bill the API.
For clip analysis Claude gets one tool, Read, confined to the clip folder (to look at
frames). Questions from the dashboard get no tools, or WebSearch only if the user ticks it.
Never a shell, WebFetch, user settings, MCP servers or slash commands.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chatwatcher.config import ClaudeConfig
from chatwatcher.tools import resolve_exe

_AUTH = re.compile(r"authenticat|not logged in|log ?in|oauth|invalid api key|credentials", re.I)
_LIMIT = re.compile(r"usage limit|limit reached|rate limit|too many requests|5-hour|weekly limit|"
                    r"resets? (?:at|in)|overloaded|\b429\b|\b529\b", re.I)


class ClaudeError(Exception):
    """kind: missing | auth | usage_limit | timeout | bad_output | error."""

    def __init__(self, message: str, *, kind: str = "error"):
        super().__init__(message)
        self.kind = kind

    @property
    def retryable(self) -> bool:
        return self.kind in ("usage_limit", "timeout", "bad_output", "error")


@dataclass
class ClaudeResult:
    data: dict[str, Any]
    model: str | None
    duration_ms: int | None
    raw: dict[str, Any]

    @property
    def usage(self) -> dict[str, int]:
        """Token counts reported by the CLI (what this call took from the plan's limits)."""
        u = self.raw.get("usage") or {}
        return {
            "input": int(u.get("input_tokens") or 0),
            "cache_read": int(u.get("cache_read_input_tokens") or 0),
            "cache_write": int(u.get("cache_creation_input_tokens") or 0),
            "output": int(u.get("output_tokens") or 0),
        }


def _env(cfg: ClaudeConfig) -> dict[str, str]:
    env = dict(os.environ)
    if not cfg.allow_api_key:
        for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            env.pop(k, None)
    env.pop("CLAUDE_CODE_SIMPLE", None)  # that's bare mode, which only accepts an API key
    return env


def classify(message: str) -> str:
    if _LIMIT.search(message):
        return "usage_limit"
    if _AUTH.search(message):
        return "auth"
    return "error"


class ClaudeRunner:
    def __init__(self, cfg: ClaudeConfig):
        exe = resolve_exe(cfg.cli_path)
        if not exe:
            raise ClaudeError(f"claude CLI not found ({cfg.cli_path!r}). Install Claude Code.",
                              kind="missing")
        self.exe = exe
        self.cfg = cfg

    def command(self, schema: dict[str, Any] | None, *, system: str, allow_read: bool = False,
                read_dir: Path | None = None, model: str | None = None, effort: str | None = None,
                tools: list[str] | None = None) -> list[str]:
        """schema=None: plain-text answer. tools: extra built-in tools (e.g. WebSearch)."""
        allowed = list(tools or []) + (["Read"] if allow_read else [])
        cmd = [self.exe, "-p", "--output-format", "json"]
        if schema is not None:
            cmd += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
        cmd += [
            "--model", model or self.cfg.model,
            "--effort", effort or self.cfg.effort,
            "--system-prompt", system,
            "--tools", ",".join(allowed),
            "--restricted", "--strict-mcp-config", "--disable-slash-commands",
            "--no-session-persistence", "--permission-mode", "dontAsk",
        ]
        if allowed:
            cmd += ["--allowedTools", ",".join(allowed)]
        if allow_read and read_dir:
            cmd += ["--add-dir", str(read_dir)]
        return cmd

    def auth_status(self) -> dict[str, Any]:
        """`claude auth status` (costs no usage). Empty dict if it can't be read."""
        try:
            out = subprocess.run([self.exe, "auth", "status"], capture_output=True, text=True,
                                 timeout=30, env=_env(self.cfg))
            return json.loads(out.stdout or "{}")
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return {}

    def _invoke(self, cmd: list[str], prompt: str, cwd: Path) -> dict[str, Any]:
        """Run the CLI and return its JSON result, or raise a classified ClaudeError."""
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", cwd=cwd, env=_env(self.cfg),
                                  timeout=self.cfg.timeout_s)
        except subprocess.TimeoutExpired as e:
            raise ClaudeError(f"claude timed out after {self.cfg.timeout_s}s", kind="timeout") from e

        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            msg = (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()[-500:]
            raise ClaudeError(f"claude failed: {msg}", kind=classify(msg))

        if out.get("is_error") or proc.returncode != 0:
            msg = str(out.get("result") or proc.stderr or "unknown error").strip()
            kind = classify(msg)
            if kind == "auth":
                msg += " — run `claude auth login` once in a terminal."
            raise ClaudeError(f"claude: {msg[:500]}", kind=kind)
        return out

    def _result(self, data: dict[str, Any], out: dict[str, Any], model: str | None) -> ClaudeResult:
        models = list((out.get("modelUsage") or {}).keys())
        return ClaudeResult(data=data, model=models[0] if models else (model or self.cfg.model),
                            duration_ms=out.get("duration_ms"), raw=out)

    def run(self, prompt: str, schema: dict[str, Any], *, system: str, cwd: Path,
            allow_read: bool = False, model: str | None = None,
            effort: str | None = None) -> ClaudeResult:
        """One structured answer matching `schema`. model/effort override the config."""
        cmd = self.command(schema, system=system, allow_read=allow_read,
                           read_dir=cwd if allow_read else None, model=model, effort=effort)
        out = self._invoke(cmd, prompt, cwd)
        data = out.get("structured_output")
        if data is None:
            data = _json_from_text(out.get("result") or "")
        if not isinstance(data, dict):
            raise ClaudeError("claude returned no structured output", kind="bad_output")
        missing = [k for k in schema.get("required", []) if k not in data]
        if missing:
            raise ClaudeError(f"claude output is missing {missing}", kind="bad_output")
        return self._result(data, out, model)

    def ask(self, prompt: str, *, system: str, cwd: Path, model: str | None = None,
            effort: str | None = None, web_search: bool = False) -> ClaudeResult:
        """A plain-text answer (data = {"text": ...}). No tools, unless web_search."""
        cmd = self.command(None, system=system, model=model, effort=effort,
                           tools=["WebSearch"] if web_search else None)
        out = self._invoke(cmd, prompt, cwd)
        text = str(out.get("result") or "").strip()
        if not text:
            raise ClaudeError("claude returned an empty answer", kind="bad_output")
        return self._result({"text": text}, out, model)


def _json_from_text(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                return None
    return None
