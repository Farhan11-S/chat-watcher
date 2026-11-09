"""Names Claude must spell right in subtitles: the talent, her friends, games, inside
jokes. One list per channel (like regions), grown from what Claude finds and from words
you correct, and editable in the editor."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

MAX_TERMS = 80
MAX_LEN = 40


def clean(raw: Any) -> list[str]:
    """Unique (ignoring case), trimmed, in order; at most MAX_TERMS."""
    items = raw if isinstance(raw, list) else re.split(r"[,\n]", str(raw or ""))
    out, seen = [], set()
    for t in items:
        t = " ".join(str(t).replace("*", "").split())[:MAX_LEN].strip(" .,;:!?\"'")
        if len(t) >= 2 and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:MAX_TERMS]


def get(conn: sqlite3.Connection, scope: str) -> list[str]:
    r = conn.execute("SELECT terms_json FROM names WHERE scope = ?", (scope,)).fetchone()
    try:
        return clean(json.loads(r["terms_json"])) if r else []
    except json.JSONDecodeError:
        return []


def put(conn: sqlite3.Connection, scope: str, terms: Any) -> list[str]:
    terms = clean(terms)
    conn.execute("""INSERT INTO names (scope, terms_json) VALUES (?, ?)
                    ON CONFLICT (scope) DO UPDATE SET terms_json = excluded.terms_json, updated_at = datetime('now')""",
                 (scope, json.dumps(terms, ensure_ascii=False)))
    return terms


def add(conn: sqlite3.Connection, scope: str, new: Any) -> tuple[list[str], list[str]]:
    """Add terms the list doesn't have yet. Returns (the list, what was added)."""
    have = get(conn, scope)
    known = {t.lower() for t in have}
    added = [t for t in clean(new) if t.lower() not in known]
    return (put(conn, scope, have + added) if added else have), added
