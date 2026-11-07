"""Synthetic chat for scoring tests."""

from __future__ import annotations

import random

from chatwatcher.ingest.chat_parser import ChatMessage

_FILLER = ["なるほど", "それな", "へー", "そうなんだ", "いいね", "ok", "たしかに", "うんうん"]


def background(duration: float, per_5s: float = 3, seed: int = 1) -> list[ChatMessage]:
    """Steady, reaction-free chat from a pool of 200 viewers."""
    rnd = random.Random(seed)
    out: list[ChatMessage] = []
    t, i = 0.0, 0
    while t < duration:
        t += rnd.expovariate(per_5s / 5)
        i += 1
        a = rnd.randrange(200)
        out.append(ChatMessage(t=round(t, 2), id=f"bg{i}", author=f"v{a}", author_id=f"UC{a}",
                               text=rnd.choice(_FILLER)))
    return out


def burst(at: float, text: str, n: int = 40, span: float = 12, prefix: str = "b",
          **kw) -> list[ChatMessage]:
    """n messages with the same text from n different viewers within `span` seconds."""
    return [ChatMessage(t=round(at + span * k / n, 2), id=f"{prefix}{at}-{k}",
                        author=f"{prefix}{k}", author_id=f"UC{prefix}{k}", text=text, **kw)
            for k in range(n)]


def chat(*parts: list[ChatMessage]) -> list[ChatMessage]:
    return sorted((m for p in parts for m in p), key=lambda m: m.t)
