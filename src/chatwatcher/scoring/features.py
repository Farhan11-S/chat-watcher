"""Per-bin signals and their robust z-scores.

Each signal is summed over a short reaction window (chat reacts over ~15s), then compared
with the *local* normal level: a rolling median over the surrounding minutes. A streamer
whose chat doubles in the last hour still gets moments from the quiet first hour.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from chatwatcher.ingest.chat_parser import ChatMessage
from chatwatcher.scoring.lexicons import CATEGORIES, Lexicon

ACTIVITY = ("rate", "chatters", "emotes")
FEATURES = ACTIVITY + CATEGORIES + ("money", "replay")

# Rough USD rates, only for weighting Super Chats against each other.
_USD = {
    "$": 1.0, "us$": 1.0, "usd": 1.0, "¥": 0.0067, "jp¥": 0.0067, "jpy": 0.0067, "￥": 0.0067,
    "€": 1.08, "eur": 1.08, "£": 1.27, "gbp": 1.27, "ca$": 0.73, "cad": 0.73, "a$": 0.65,
    "aud": 0.65, "nz$": 0.6, "hk$": 0.128, "nt$": 0.031, "twd": 0.031, "₩": 0.00073,
    "krw": 0.00073, "₱": 0.0175, "php": 0.0175, "mx$": 0.055, "r$": 0.18, "brl": 0.18,
    "₹": 0.012, "inr": 0.012, "rp": 0.000063, "idr": 0.000063, "฿": 0.028, "thb": 0.028,
    "rm": 0.21, "myr": 0.21, "sgd": 0.74, "s$": 0.74, "chf": 1.13, "sek": 0.095,
    "nok": 0.093, "dkk": 0.145, "pln": 0.25, "czk": 0.043, "huf": 0.0027, "₫": 0.00004,
    "vnd": 0.00004, "₪": 0.27, "try": 0.03, "zar": 0.055, "ars": 0.001, "clp": 0.001,
    "cop": 0.00025, "pen": 0.27, "₽": 0.011, "rub": 0.011,
}
_MEMBER_USD = 5.0
_GIFT_USD = 5.0      # per gifted membership
_JEWEL_USD = 1.0     # jewel gifts carry no price


def to_usd(amount: float | None, currency: str | None) -> float:
    if amount is None:
        return 2.0  # unknown currency: count it as a small Super Chat
    rate = _USD.get((currency or "").lower())
    return amount * rate if rate else 2.0


@dataclass
class Timeline:
    bin_s: int
    n_bins: int
    raw: dict[str, np.ndarray]        # windowed raw values per bin
    z: dict[str, np.ndarray]          # robust z-scores per bin
    authors: list[set[str]] = field(default_factory=list, repr=False)

    def t(self, i: int) -> float:
        """Start time of bin i in seconds."""
        return i * self.bin_s


def _window_sum(x: np.ndarray, k: int) -> np.ndarray:
    """Centered moving sum over k bins (k odd)."""
    if k <= 1:
        return x.astype(float)
    return np.convolve(x, np.ones(k), mode="same")


def _rolling(x: np.ndarray, half: int, fn) -> np.ndarray:
    """Centered rolling statistic over 2·half+1 bins (edges mirrored)."""
    padded = np.pad(x, half, mode="reflect" if len(x) > 1 else "edge")
    return fn(sliding_window_view(padded, 2 * half + 1), axis=1)


def robust_z(x: np.ndarray, half: int, *, floor: str = "poisson") -> np.ndarray:
    """z = (x - rolling median) / scale, with scale = max(1.4826·MAD, floor).

    The floor keeps quiet streams from producing huge z-scores out of noise:
    'poisson' uses sqrt(median+1) (right for counts), a number is used as-is.
    """
    med = _rolling(x, half, np.median)
    mad = _rolling(np.abs(x - med), half, np.median) * 1.4826
    if floor == "poisson":
        lo = np.sqrt(med + 1.0)
    else:
        lo = np.full_like(med, float(floor))
    return (x - med) / np.maximum(mad, lo)


def build_timeline(
    messages: list[ChatMessage],
    duration_s: float,
    *,
    bin_s: int = 5,
    react_window_s: int = 15,
    baseline_window_s: int = 600,
    lexicon: Lexicon | None = None,
    heatmap: list[list[float]] | None = None,
) -> Timeline:
    lex = lexicon or Lexicon()
    # The video length bounds the timeline (nothing past the end can be clipped); messages
    # after it fall into the last bin. Without a duration, chat decides.
    span = duration_s if duration_s else (messages[-1].t + 1 if messages else 0.0)
    n = max(1, math.ceil(span / bin_s))

    counts = {f: np.zeros(n) for f in ("rate", "emotes", *CATEGORIES)}
    money = np.zeros(n)
    authors: list[set[str]] = [set() for _ in range(n)]

    for m in messages:
        if m.is_owner or m.is_mod:
            continue
        b = min(int(m.t // bin_s), n - 1)
        if m.kind in ("superchat", "sticker"):
            money[b] += to_usd(m.amount, m.currency)
        elif m.kind == "gift":
            money[b] += (m.gift_count or 1) * _GIFT_USD
        elif m.kind == "jewel_gift":
            money[b] += _JEWEL_USD
        elif m.kind == "member_new":
            money[b] += _MEMBER_USD
        if m.kind not in ("chat", "superchat", "sticker", "member_milestone"):
            continue
        if m.text:
            for cat in lex.categories(m.text):
                counts[cat][b] += 1
            if lex.is_greeting(m.text):
                continue
        counts["rate"][b] += 1
        authors[b].add(m.author_id or m.author)
        if m.emotes:
            counts["emotes"][b] += 1

    k = max(1, round(react_window_s / bin_s))
    k += 1 - k % 2  # odd → centered
    half_k = k // 2
    half_base = max(1, round(baseline_window_s / bin_s / 2))

    raw: dict[str, np.ndarray] = {f: _window_sum(v, k) for f, v in counts.items()}
    raw["chatters"] = np.array([
        len(set().union(*authors[max(0, i - half_k): i + half_k + 1])) for i in range(n)
    ], dtype=float)
    raw["money"] = np.log1p(_window_sum(money, k))
    raw["replay"] = heatmap_bins(heatmap, n, bin_s)

    z = {f: robust_z(raw[f], half_base) for f in ("rate", "chatters", "emotes", *CATEGORIES)}
    z["money"] = robust_z(raw["money"], half_base, floor="0.5")
    z["replay"] = _replay_z(raw["replay"])
    return Timeline(bin_s=bin_s, n_bins=n, raw=raw, z=z, authors=authors)


def heatmap_bins(heatmap: list[list[float]] | None, n: int, bin_s: int,
                 edge_s: float = 60.0) -> np.ndarray:
    """YouTube's "Most replayed" curve resampled to bins. The first/last minute is zeroed:
    replays pile up at the start of any video regardless of content."""
    out = np.zeros(n)
    if not heatmap:
        return out
    total = n * bin_s
    for start, end, value in heatmap:
        a = max(0, int(start // bin_s))
        b = min(n, int(math.ceil(end / bin_s)))
        out[a:b] = value
    edge = int(edge_s // bin_s)
    out[:edge] = 0.0
    if total - edge_s > 0:
        out[max(0, n - edge):] = 0.0
    return out


def _replay_z(h: np.ndarray) -> np.ndarray:
    nz = h[h > 0]
    if len(nz) < 5:
        return np.zeros_like(h)
    med = float(np.median(nz))
    mad = float(np.median(np.abs(nz - med))) * 1.4826
    z = (h - med) / max(mad, 0.05)
    z[h == 0] = 0.0
    return z
