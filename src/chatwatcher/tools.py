"""Locate external executables."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def resolve_exe(name: str) -> str | None:
    """Find an executable: explicit paths as-is, then this venv's bin/, then PATH.

    The venv check matters because yt-dlp is a Python dependency: it lives next to
    our interpreter even when the venv isn't activated (e.g. under cron/systemd).
    """
    if not name:
        return None
    if "/" in name:
        p = Path(name).expanduser()
        return str(p) if p.is_file() else None
    local = Path(sys.executable).parent / name
    if local.is_file():
        return str(local)
    return shutil.which(name)


def find_deno(configured: str = "") -> str | None:
    """Deno for yt-dlp's YouTube challenge solver: configured path, venv/PATH, then the
    official installer's default location ($DENO_INSTALL or ~/.deno/bin), so it works
    even when ~/.deno/bin isn't on PATH."""
    if configured:
        return resolve_exe(configured)
    found = resolve_exe("deno")
    if found:
        return found
    install = Path(os.environ.get("DENO_INSTALL") or Path.home() / ".deno")
    candidate = install / "bin" / "deno"
    return str(candidate) if candidate.is_file() else None
