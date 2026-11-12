"""Reading Claude's answer to an AI edit: piece ids ("p2-p5", "p7:0.6", "p9!"), the
"a | b | c" lists, and the limits on how much it may place. Plan is what the cut
(autoedit.assemble) gives back."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

STRENGTHS = {"light": 1, "normal": 2, "heavy": 3}
CENTRE = "centre"
MAX_OVERLAYS = 3
MAX_TEXTS = 3
MAX_SPOTS = 3
MAX_CAPTIONS = 12


class AutoEditError(Exception):
    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind


@dataclass
class Plan:
    segments: list[list[float]]
    cut_s: float = 0.0                       # how much was removed
    pieces_kept: int = 0
    pieces_total: int = 0
    cold_open: bool = False
    warnings: list[str] = field(default_factory=list)


# ── reading Claude's answer ─────────────────────────────────────────────────

_ID = re.compile(r"^\s*p(\d+)\s*(?:[-–]\s*p?(\d+))?\s*(?::\s*(\d+(?:\.\d+)?)|(!))?\s*$", re.I)


def parse_keep(items: list[str], n: int) -> dict[int, tuple[str, float | None]]:
    """{piece index (0-based): (mode, seconds)}; mode: all | beat | action."""
    out: dict[int, tuple[str, float | None]] = {}
    for item in items:
        m = _ID.match(str(item))
        if not m:
            continue
        a, b = int(m[1]), int(m[2] or m[1])
        for k in range(min(a, b), max(a, b) + 1):
            if 1 <= k <= n:
                out[k - 1] = ("beat", float(m[3])) if m[3] else (("action", None) if m[4] else ("all", None))
    return out


def parse_range(text: str, n: int) -> list[int]:
    m = _ID.match(str(text or ""))
    if not m:
        return []
    a, b = int(m[1]), int(m[2] or m[1])
    return [k - 1 for k in range(min(a, b), max(a, b) + 1) if 1 <= k <= n]


def parts(item: Any) -> list[str]:
    return [x.strip() for x in str(item).split("|")]
