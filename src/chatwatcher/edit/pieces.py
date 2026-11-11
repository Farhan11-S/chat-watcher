"""A clip as numbered pieces for Claude to cut with: every stretch of talking and every
quiet stretch between, with exact times from Whisper's words.

Claude is poor at exact timestamps but good at choosing, so the AI edit shows it pieces
("p7 [26.1–41.6] quiet 15.5 s · 2 hits · chat ×3.1") and it answers with piece ids. The
times come from here, so cuts land between words.

A talk piece is a whole sentence (a thought), so a cut never stops someone mid-sentence:
it ends at a sentence end followed by a pause, or at a long pause. Pauses inside it stay
inside (the AI edit shortens the long ones). Pieces tile the clip with no holes: quiet
pieces fill the gaps between sentences; gaps shorter than MIN_QUIET are split between the
talk pieces on either side."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MIN_QUIET = 0.6          # a gap between sentences at least this long is its own quiet piece
SENTENCE_GAP = 0.3       # a sentence end followed by this much quiet ends a talk piece
HARD_GAP = 1.6           # a pause this long ends a talk piece even mid-sentence
MAX_TALK = 12.0          # a talk piece is split at a sentence end (or a comma) past this
LONGEST_TALK = 20.0      # …and anywhere past this (someone who never stops)
_SENTENCE_END = re.compile(r"[.?!。？！]$")
_COMMA = re.compile(r"[,、，;:]$")
_CHAT = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\]")


@dataclass
class Piece:
    id: str
    kind: str                   # talk | quiet
    t0: float                   # piece edges (the pieces tile the clip)
    t1: float
    w0: float = 0.0             # talk: first word start / last word end
    w1: float = 0.0
    text: str = ""
    hits: list[tuple[float, float]] = field(default_factory=list)   # (time, dB above talking)
    chat: float = 0.0           # chat messages per second here, over the clip's average
    voice: float = 0.0          # quiet: seconds of voice with no words (a laugh, a scream, mumbling)
    pauses: list[tuple[float, float]] = field(default_factory=list)   # talk: gaps between its words ≥ MIN_QUIET

    @property
    def length(self) -> float:
        return self.t1 - self.t0

    def line(self) -> str:
        span = f"{self.id} [{self.t0:.1f}–{self.t1:.1f}]"
        extra = []
        if self.hits:
            extra.append(f"{len(self.hits)} loud hit{'s' * (len(self.hits) > 1)} "
                         f"(max +{max(d for _, d in self.hits):.0f} dB at {max(self.hits, key=lambda h: h[1])[0]:.1f})")
        if self.kind == "quiet" and self.voice >= 0.3:
            extra.append(f"voice without words {self.voice:.1f}s (laugh/scream?)")
        if self.chat >= 1.5:
            extra.append(f"chat ×{self.chat:.1f}")
        tail = (" · " + " · ".join(extra)) if extra else ""
        if self.kind == "talk":
            return f'{span} "{self.text}"{tail}'
        return f"{span} quiet {self.length:.1f}s{tail}"


def _words(data: dict[str, Any] | None) -> list[tuple[float, float, str]]:
    out = []
    for line in (data or {}).get("lines") or []:
        for w in line.get("words") or []:
            if str(w[2]).strip():
                out.append((float(w[0]), float(w[1]), str(w[2])))
    return sorted(out)


def _talk_groups(words: list[tuple[float, float, str]]) -> list[list[tuple[float, float, str]]]:
    """Words → sentences: a new one after a sentence end and a pause, after a long pause, or
    when one runs too long."""
    groups: list[list[tuple[float, float, str]]] = []
    cur: list[tuple[float, float, str]] = []
    for w in words:
        if cur:
            gap, last = w[0] - cur[-1][1], cur[-1][2].strip()
            length = cur[-1][1] - cur[0][0]
            ended = bool(_SENTENCE_END.search(last))
            if (gap >= HARD_GAP or (ended and gap >= SENTENCE_GAP) or length >= LONGEST_TALK
                    or (length >= MAX_TALK and (ended or _COMMA.search(last)))):
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def chat_times(chat_text: str) -> list[float]:
    out = []
    for raw in chat_text.splitlines():
        m = _CHAT.match(raw)
        if m:
            out.append(int(m[1]) * 60 + float(m[2]))
    return out


def build(words_data: dict[str, Any] | None, duration: float, hits: list[tuple[float, float]] | None = None,
          chat: list[float] | None = None) -> list[Piece]:
    """words_data's "speech" (subs.align) marks the voice in quiet pieces."""
    """The clip [0, duration] as talk and quiet pieces, in time order."""
    raw: list[Piece] = []
    for g in _talk_groups([w for w in _words(words_data) if w[0] < duration]):
        w0, w1 = max(g[0][0], 0.0), min(g[-1][1], duration)
        gaps = [(a[1], b[0]) for a, b in zip(g, g[1:]) if b[0] - a[1] >= MIN_QUIET]
        raw.append(Piece("", "talk", w0, w1, w0, w1, "".join(w[2] for w in g).strip(), pauses=gaps))
    pieces: list[Piece] = []
    at = 0.0
    for p in raw:
        if p.w0 - at >= MIN_QUIET:
            pieces.append(Piece("", "quiet", at, p.w0))
            p.t0 = p.w0
        elif pieces:                       # small gap: split it between the neighbours
            mid = (at + p.w0) / 2
            pieces[-1].t1 = mid
            p.t0 = mid
        else:
            p.t0 = 0.0
        pieces.append(p)
        at = p.w1
    if duration - at >= MIN_QUIET or not pieces:
        pieces.append(Piece("", "quiet", at, duration))
    else:
        pieces[-1].t1 = duration
    rate = len(chat or []) / duration if duration else 0.0
    for i, p in enumerate(pieces, 1):
        p.id = f"p{i}"
        p.hits = [h for h in hits or [] if p.t0 <= h[0] < p.t1]
        if p.kind == "quiet":
            p.voice = round(sum(max(0.0, min(b, p.t1) - max(a, p.t0))
                                for a, b in (words_data or {}).get("speech") or []), 1)
        if rate and p.length > 0:
            n = sum(p.t0 <= c < p.t1 for c in chat or [])
            p.chat = round(n / p.length / rate, 1)
    return pieces
