"""Claude's answer → checked compilation candidates, the stream's context and the moments
chat missed (times in seconds).

Nothing here trusts the answer: unknown kinds become "other", ratings are clamped, pieces
with unreadable or impossible times are dropped, and a candidate left with fewer than two
pieces is dropped (it would not be a compilation).
"""

from __future__ import annotations

import re
from typing import Any

from chatwatcher.compile.prompt import CATEGORIES, KINDS, MAX_CANDIDATES, ROLES

MIN_PIECE_S = 2.0
MAX_PIECE_S = 300.0
_TIME = r"(?:(\d+):)?(\d{1,2}):(\d{2})(?:\.\d+)?"
_SPAN = re.compile(rf"^\s*{_TIME}\s*[-–—]\s*{_TIME}\s*$")
_ONE = re.compile(rf"^\s*{_TIME}\s*$")
MISSED_MIN_S, MISSED_MAX_S = 15.0, 240.0


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


def _text(v: Any, n: int) -> str:
    return " ".join(str(v or "").split())[:n]


def parse_span(text: Any, duration: float = 0) -> tuple[float, float] | None:
    """"0:14:05-0:14:20" → (845.0, 860.0), inside the stream; None when unreadable."""
    m = _SPAN.match(str(text or ""))
    if not m:
        return None
    a, b = _secs(*m.groups()[:3]), _secs(*m.groups()[3:])
    if duration:
        a, b = min(a, duration), min(b, duration)
    return (float(a), float(b)) if b > a else None


def parse_context(data: Any, duration: float = 0) -> dict[str, Any]:
    """{summary, chapters: [{start, end, text}], jokes: [str], names: [str]}; whatever is
    missing or unreadable is left empty."""
    d = data if isinstance(data, dict) else {}

    def lines(key: str, cap: int, n: int = 300) -> list[str]:
        v = d.get(key)
        return [t for t in (_text(x, n) for x in (v if isinstance(v, list) else [])) if t][:cap]
    chapters = []
    for line in lines("chapters", 40, 400):
        span, _, what = (p.strip() for p in line.partition("|"))
        got = parse_span(span, duration)
        if got and what:
            chapters.append({"start": got[0], "end": got[1], "text": what})
    chapters.sort(key=lambda c: c["start"])
    return {"summary": _text(d.get("summary"), 1500), "chapters": chapters,
            "jokes": lines("running_jokes", 12), "names": lines("names", 25, 200)}


def parse_missed(data: Any, duration: float = 0, limit: int = 20) -> list[dict[str, Any]]:
    """The moments chat missed: [{start, end, peak, title, category, why, rating}], best
    first. Ranges are kept between MISSED_MIN_S and MISSED_MAX_S; a rating under 5, no
    title or an unreadable range drops the moment."""
    items = data.get("missed") if isinstance(data, dict) else None
    out: list[dict[str, Any]] = []
    for c in items if isinstance(items, list) else []:
        if not isinstance(c, dict):
            continue
        span = parse_span(c.get("span"), duration)
        title = _text(c.get("title"), 200)
        try:
            rating = min(10, max(1, int(round(float(c.get("rating"))))))
        except (TypeError, ValueError):
            rating = 0
        if not span or not title or rating < 5:
            continue
        a, b = span
        if b - a < MISSED_MIN_S:                     # too short to follow: widen around it
            pad = (MISSED_MIN_S - (b - a)) / 2
            a, b = max(0.0, a - pad), b + pad
            if duration:
                b = min(b, duration)
        b = min(b, a + MISSED_MAX_S)
        m = _ONE.match(str(c.get("peak") or ""))
        peak = float(_secs(*m.groups())) if m else None
        if peak is None or not a <= peak <= b:
            peak = a + (b - a) * 0.6
        category = str(c.get("category") or "").strip().lower()
        out.append({"start": a, "end": b, "peak": peak, "title": title, "rating": rating,
                    "category": category if category in CATEGORIES else "other",
                    "why": _text(c.get("why"), 400)})
    out.sort(key=lambda c: -c["rating"])
    return out[:limit]
