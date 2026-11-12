"""Directing the ✨ AI edit: a conversation with Claude about one clip's edit.

Each edit of a clip (long and short apart) has a Claude session (the claude CLI keeps it,
in the clip folder's project). The ✨ AI edit button starts a new one: Claude gets the
whole material (pieces, chat, frames, library) and makes the edit. Then you write to it,
"open on the scream", "fewer zooms on the game", "why did you cut p12?", and it answers
in the same session, so it remembers the clip and what it did. Every message tells it the
edit as it is now, because you may have changed it by hand since. It answers with the
whole edit again and apply = true, which replaces the edit (Ctrl+Z in the editor brings
yours back), or apply = false when it only answers.

A session that can't be resumed (the clip was re-cut into another folder, or the CLI's
history was cleared) is started again with the full material and the recent messages."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from chatwatcher.analyze.claude_runner import ClaudeRunner
from chatwatcher.config import Config
from chatwatcher.edit import autoedit as ae
from chatwatcher.edit import service

MAX_MESSAGE = 2000
RECAP = 6                 # messages repeated to a session that had to start again

FOLLOW_UP = """\
The director says:
<director>
{message}
</director>

The edit as it is now (the director may have changed it by hand since your last answer):
{state}

If the director asks for a change, answer with apply = true and the whole edit again: \
change what they ask for and keep the rest as it is now. If they only ask a question, or \
nothing needs to change, answer with apply = false and put your answer in note. Answer only \
with the structured output."""

FIRST = """\

The director's direction for this edit (follow it; it wins over the defaults above):
<director>
{message}
</director>"""


class DirectError(ae.AutoEditError):
    pass


@dataclass
class Turn:
    result: ae.Result | None          # the new edit, or None when Claude only answered
    reply: str
    messages: list[dict[str, Any]]
    fresh: bool                       # a new session was started


def thread(conn: sqlite3.Connection, moment_pk: int, kind: str) -> tuple[str | None, list[dict[str, Any]]]:
    """The current session of this edit and its messages, oldest first."""
    last = conn.execute("SELECT session_id FROM edit_chats WHERE moment_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
                        (moment_pk, kind)).fetchone()
    if last is None:
        return None, []
    rows = conn.execute("SELECT role, text, applied, model, created_at FROM edit_chats "
                        "WHERE moment_id = ? AND kind = ? AND session_id = ? ORDER BY id",
                        (moment_pk, kind, last["session_id"])).fetchall()
    return last["session_id"], [dict(r) | {"applied": bool(r["applied"])} for r in rows]


def _ids(nums: list[int]) -> str:
    """[2, 3, 4, 7] → "p2-p4, p7" (in the order given)."""
    out: list[str] = []
    start = prev = None
    for n in nums + [None]:
        if start is not None and n == prev + 1:
            prev = n
            continue
        if start is not None:
            out.append(f"p{start}" if start == prev else f"p{start}-p{prev}")
        start = prev = n
    return ", ".join(out)


def describe(m: ae.Material, segments: list[list[float]], effects: list[dict[str, Any]],
             title: str = "") -> str:
    """The edit in the AI edit's own words: kept pieces per segment, in play order, and the
    effects (whose they are)."""
    lines = []
    for s, e, *g in segments:
        inside = [k + 1 for k, p in enumerate(m.ps) if s - 0.05 <= (p.t0 + p.t1) / 2 <= e + 0.05]
        gain = float(g[0]) if g else 0.0
        part = _ids(inside) if inside else "(part of a piece)"
        lines.append(f"- {s:.1f}–{e:.1f} s: {part}" + (f" at {gain:+.0f} dB" if gain else ""))
    fx = [ae.placed_line(f) + (" (yours)" if f.get("by") == "claude" else " (placed by the director)") for f in effects]
    out = "Kept, in play order:\n" + ("\n".join(lines) or "(nothing)")
    out += "\nEffects:\n" + ("\n".join(fx) or "(none)")
    if title:
        out += f"\nShort's title: {title}"
    lib = [f"- {o['id']}: {o['name']} · {o['duration_s']:.1f} s · {o['description'] or ''}" for o in m.overlays]
    out += "\nOverlay library now (for overlays):\n" + ("\n".join(lib) or "(empty)")
    return out


def _say(conn: sqlite3.Connection, moment_pk: int, kind: str, session: str, role: str, text: str,
         applied: bool = False, model: str | None = None, usage: dict[str, int] | None = None) -> None:
    conn.execute("INSERT INTO edit_chats (moment_id, kind, session_id, role, text, applied, model, usage_json) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (moment_pk, kind, session, role, text, int(applied), model,
                  json.dumps(usage) if usage else None))


def _summary(r: ae.Result) -> str:
    c, p = r.counts, r.plan
    bits = [f"cut {p.cut_s:.0f} s" if p.cut_s >= 1 else "",
            f"{c['camera']} shot{'s' * (c['camera'] != 1)}" if c.get("camera") else "",
            f"{c['sounds']} sound{'s' * (c['sounds'] != 1)}" if c.get("sounds") else "",
            f"{c['hits']} shake/flash" if c.get("hits") else "",
            f"{c['overlays']} overlay{'s' * (c['overlays'] != 1)}" if c.get("overlays") else "",
            f"{c['texts']} text{'s' * (c['texts'] != 1)}" if c.get("texts") else "",
            f"{c['spots']} spotlight{'s' * (c['spots'] != 1)}" if c.get("spots") else "",
            f"{c['captions']} caption style{'s' * (c['captions'] != 1)}" if c.get("captions") else "",
            "a cold open" if p.cold_open else ""]
    return ", ".join(b for b in bits if b)


def direct(cfg: Config, conn: sqlite3.Connection, moment_pk: int, message: str = "", *, kind: str = "short",
           new: bool = False, intensity: str = "normal", runner: ClaudeRunner | None = None,
           captions_on: bool = True) -> Turn:
    """Send the director's message (empty = just make the edit) to the edit's session; a
    new session when there's none yet or new=True. Saves the edit Claude makes."""
    message = str(message or "").strip()[:MAX_MESSAGE]
    m = ae.material(cfg, conn, moment_pk, kind, intensity)
    session, history = thread(conn, moment_pk, kind)
    fresh = new or session is None
    res = None
    if not fresh:
        if not message:
            raise DirectError("Write what you want changed.", "invalid")
        row = service.stored(conn, moment_pk, kind)
        segs = service.to_clip(json.loads(row["segments_json"]), m.clip) if row else service.whole(m.clip)
        effects = service.effects_to_clip(service.effects(m.base), m.clip)
        title = (service.current_layout(conn, moment_pk, m.clip, row).get("title") or "") if kind == "short" else ""
        prompt = FOLLOW_UP.format(message=message, state=describe(m, segs, effects, title))
        try:
            res = ae.ask(cfg, m, prompt, runner=runner, images=False, chat=True, session=session, resume=True)
        except ae.AutoEditError as e:
            if e.kind != "no_session":
                raise
            fresh = True                    # lost: start again, with what was said so far
    if fresh:
        recap = ""
        if not new and history:
            said = [f"{'Director' if h['role'] == 'user' else 'You'}: {h['text']}" for h in history[-RECAP:]]
            recap = "\n\nEarlier in this conversation (the session was lost):\n" + "\n".join(said)
        session = str(uuid.uuid4())
        prompt = m.prompt() + (FIRST.format(message=message) if message else "") + recap
        res = ae.ask(cfg, m, prompt, runner=runner, chat=True, session=session)
        res.data["apply"] = True            # a first answer always makes the edit
    assert res is not None and session is not None
    _say(conn, moment_pk, kind, session, "user", message or "✨ Make the edit")
    d = res.data
    result = None
    reply = " ".join(str(d.get("note") or "").split())
    if d.get("apply"):
        result = ae.apply(cfg, conn, moment_pk, m, d, model=res.model, usage=res.usage, captions_on=captions_on,
                          restyle=fresh)
        done = _summary(result)
        reply = (reply + (f" ({done})" if done else "")).strip() or "Done."
        if result.plan.warnings:
            reply += " " + " ".join(result.plan.warnings)
    if fresh and not new and history:
        reply = "(The earlier session couldn't be resumed, so this is a new one with the full clip.) " + reply
    _say(conn, moment_pk, kind, session, "assistant", reply or "(no answer)", applied=result is not None,
         model=res.model, usage=res.usage)
    return Turn(result, reply, thread(conn, moment_pk, kind)[1], fresh)

