"""Check that the external tools the pipeline shells out to are installed and usable."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from chatwatcher.config import Config
from chatwatcher.tools import find_deno, resolve_exe

Level = Literal["ok", "warn", "fail"]


@dataclass
class Check:
    name: str
    level: Level
    detail: str


def _version(exe: str, args: list[str]) -> tuple[str | None, str]:
    """Return (resolved path, first line of version output) or (None, reason)."""
    path = resolve_exe(exe)
    if not path:
        return None, "not found"
    try:
        out = subprocess.run([path, *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return path, f"failed to run: {e}"
    text = (out.stdout or out.stderr).strip().splitlines()
    return path, text[0] if text else "(no version output)"


def _tool(name: str, exe: str, args: list[str], required: bool, why: str) -> Check:
    path, detail = _version(exe, args)
    if path and not detail.startswith("failed"):
        return Check(name, "ok", detail)
    return Check(name, "fail" if required else "warn", f"{detail} — {why}")


def _claude_login(cfg: Config) -> Check:
    from chatwatcher.analyze.claude_runner import ClaudeError, ClaudeRunner
    try:
        status = ClaudeRunner(cfg.claude).auth_status()
    except ClaudeError as e:
        return Check("claude login", "warn", str(e))
    if not status:
        return Check("claude login", "warn", "couldn't read `claude auth status`")
    if not status.get("loggedIn"):
        return Check("claude login", "warn",
                     "not logged in — run `claude auth login` once (uses your Claude subscription)")
    method = status.get("authMethod") or "?"
    if "api" in method.lower() and not cfg.claude.allow_api_key:
        return Check("claude login", "warn", f"logged in with {method}; analysis needs a subscription login")
    return Check("claude login", "ok", f"logged in ({method})")


def _drive_remote(cfg: Config) -> Check:
    from chatwatcher.upload.drive import Rclone, RcloneError
    try:
        rc = Rclone(cfg.drive)
        notices = rc.check()
    except RcloneError as e:
        return Check("drive remote", "fail", str(e).splitlines()[0])
    if notices:
        return Check("drive remote", "warn", f"{rc.remote}: reachable, but it {notices[0]}")
    return Check("drive remote", "ok", f"{rc.remote}: reachable → uploads go to {rc.remote}:{cfg.drive.folder}/")


def run_checks(cfg: Config) -> list[Check]:
    yt = cfg.youtube
    checks = [
        _tool("yt-dlp", yt.ytdlp_path, ["--version"], True,
              "needed for chat, metadata and clips (pip install -e . installs it)"),
        _tool("ffmpeg", "ffmpeg", ["-version"], True, "needed to merge clips and grab frames"),
        _tool("deno", find_deno(yt.deno_path) or yt.deno_path or "deno", ["--version"], False,
              "yt-dlp uses it to pass YouTube's JS challenge; downloads may fail without it"),
    ]

    if cfg.claude.enabled:
        claude = _tool("claude", cfg.claude.cli_path, ["--version"], False,
                       "summaries/storyboards will be skipped")
        checks.append(claude)
        if claude.level == "ok":
            checks.append(_claude_login(cfg))
        if os.environ.get("ANTHROPIC_API_KEY"):
            checks.append(Check(
                "claude api key", "warn",
                "ANTHROPIC_API_KEY is set and claude.allow_api_key = true — analysis will bill "
                "the API instead of your subscription.",
            ) if cfg.claude.allow_api_key else Check(
                "claude api key", "ok",
                "ANTHROPIC_API_KEY is set but ignored for analysis (claude.allow_api_key = false)",
            ))

    if cfg.drive.enabled:
        rc = _tool("rclone", cfg.drive.rclone_path, ["version"], True,
                   "drive.enabled is true but rclone is missing")
        checks.append(rc)
        if rc.level == "ok":
            checks.append(_drive_remote(cfg))

    if yt.cookies_file:
        ok = Path(yt.cookies_file).expanduser().is_file()
        checks.append(Check("cookies", "ok" if ok else "fail",
                            yt.cookies_file if ok else f"file not found: {yt.cookies_file}"))
    else:
        checks.append(Check("cookies", "warn",
                            "no cookies file set; members-only VODs and some bot checks will fail"))
    return checks
