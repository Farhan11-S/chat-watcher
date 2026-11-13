"""Claude's answer → checked compilation candidates (times in seconds).

Nothing here trusts the answer: unknown kinds become "other", ratings are clamped, pieces
with unreadable or impossible times are dropped, and a candidate left with fewer than two
pieces is dropped (it would not be a compilation).
"""

from __future__ import annotations

import re
from typing import Any

from chatwatcher.compile.prompt import KINDS, MAX_CANDIDATES, ROLES

MIN_PIECE_S = 2.0
MAX_PIECE_S = 300.0
_TIME = r"(?:(\d+):)?(\d{1,2}):(\d{2})(?:\.\d+)?"
_SPAN = re.compile(rf"^\s*{_TIME}\s*[-–—]\s*{_TIME}\s*$")


def _secs(h: str | None, m: str, s: str) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s)


def parse_segment(line: Any, duration: float = 0) -> dict[str, Any] | None:
    """"0:14:05-0:14:20 | setup | note" (or an object with start/end) → {start, end, role, note}."""
    if isinstance(line, dict):
        span, role, note = f"{line.get('start', '')}-{line.get('end', '')}", line.get("role", ""), line.get("note", "")
    else:
        span, _, rest = (p.strip() for p in str(line).partition("|"))
        role, sep, note = (p.strip() for p in rest.partition("|"))
        if role.lower() not in ROLES:    # "times | note": no role given
            role, note = "", rest
    m = _SPAN.match(span)
    if not m:
        return None
    start, end = _secs(*m.groups()[:3]), _secs(*m.groups()[3:])
    if duration:
        start, end = min(start, duration), min(end, duration)
    if end - start < MIN_PIECE_S:
        return None
    end = min(end, start + MAX_PIECE_S)
    role = str(role).strip().lower()
    return {"start": float(start), "end": float(end), "role": role if role in ROLES else "beat",
            "note": " ".join(str(note).split())[:200]}


def parse_result(data: Any, duration: float = 0) -> list[dict[str, Any]]:
    """The valid candidates, best rating first (Claude's order among equals)."""
    items = data.get("compilations") if isinstance(data, dict) else None
    out: list[dict[str, Any]] = []
    for c in items if isinstance(items, list) else []:
        if not isinstance(c, dict):
            continue
        title = " ".join(str(c.get("title") or "").split())[:200]
        segments = [s for s in (parse_segment(x, duration) for x in c.get("segments") or []) if s]
        segments = [s for i, s in enumerate(segments)          # the same piece listed twice
                    if not any(o["start"] == s["start"] and o["end"] == s["end"] for o in segments[:i])]
        if not title or len(segments) < 2:
            continue
        try:
            rating = min(10, max(1, int(round(float(c.get("rating"))))))
        except (TypeError, ValueError):
            rating = 1
        kind = str(c.get("kind") or "").strip().lower()
        kind = kind if kind in KINDS else "other"
        if kind != "callback":
            for s in segments:
                s["role"] = "beat"
        out.append({"title": title, "kind": kind, "rating": rating, "segments": segments,
                    "reason": " ".join(str(c.get("reason") or "").split())[:600]})
    out.sort(key=lambda c: -c["rating"])
    return out[:MAX_CANDIDATES]
