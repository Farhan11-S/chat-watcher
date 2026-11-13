"""Thin a whole stream's chat to a line or two per time window.

A stream has tens of thousands of messages; Claude only needs to see what chat was
reacting to. Per window the messages most people typed win (said by 5 people beats said
once), emote-only messages are dropped, and a message already shown for the window
before is not shown again.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

MAX_LEN = 60                     # characters kept of one message
_EMOTE = re.compile(r":[\w\-]+:")            # custom emote shortcuts, e.g. ":_kusa:"
_STRETCH = re.compile(r"(.)\1{2,}")          # loooool → lool
_NOT_WORD = re.compile(r"[\W_]+")


@dataclass(slots=True)
class ChatLine:
    t: int                       # window start, seconds from stream start
    texts: list[str]             # what to show, most repeated first ("bless you ×6")


def clean(text: str) -> str:
    """The message without emotes; "" when nothing readable is left (emote or emoji only)."""
    text = re.sub(r"\s+", " ", _EMOTE.sub(" ", text or "")).strip()
    if not any(ch.isalnum() for ch in text):
        return ""
    return text[:MAX_LEN].rstrip()


def key(text: str) -> str:
    """Messages that say the same thing share a key: case, punctuation and stretched
    letters don't count ("LOOOOL!!" = "lool")."""
    return _NOT_WORD.sub(" ", _STRETCH.sub(r"\1\1", text.casefold())).strip()


def thin_chat(messages: Iterable[Any], *, window_s: int = 10, per_window: int = 2) -> list[ChatLine]:
    """messages: ChatMessage-like (t, text, kind), any order. Returns the windows that have
    something to show, in stream order."""
    windows: dict[int, tuple[Counter, dict[str, str]]] = {}
    for m in messages:
        if m.kind not in ("chat", "superchat") or m.t < 0:
            continue
        text = clean(m.text)
        k = key(text) if text else ""
        if not k:
            continue
        counts, first = windows.setdefault(int(m.t // window_s), (Counter(), {}))
        counts[k] += 1
        first.setdefault(k, text)            # shown as the first person typed it

    out: list[ChatLine] = []
    shown_before: set[str] = set()
    last_w = None
    for w in sorted(windows):
        counts, first = windows[w]
        if last_w != w - 1:
            shown_before = set()
        # Most repeated first; among equals the longer message says more.
        ranked = sorted(counts, key=lambda k: (-counts[k], -min(len(k), 30), k))
        picked = [k for k in ranked if k not in shown_before][:per_window]
        shown_before, last_w = set(picked), w
        if picked:
            out.append(ChatLine(w * window_s, [first[k] + (f" ×{counts[k]}" if counts[k] >= 3 else "")
                                               for k in picked]))
    return out
