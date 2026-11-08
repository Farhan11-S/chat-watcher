"""Combined score → peaks → moments with clip in/out points."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from chatwatcher.ingest.captions import Segment
from chatwatcher.ingest.chat_parser import ChatMessage
from chatwatcher.scoring.features import Timeline
from chatwatcher.scoring.lexicons import Lexicon, canonical


@dataclass
class Moment:
    rank: int
    score: float
    peak_s: float
    react_start_s: float       # where chat starts reacting
    react_end_s: float         # where the reaction has died down
    start_s: float             # suggested clip in-point
    end_s: float               # suggested clip out-point
    kind: str = "reaction"     # reaction | song
    reasons: list[dict[str, float | str]] = field(default_factory=list)
    top_messages: list[tuple[str, int]] = field(default_factory=list)

    @property
    def length_s(self) -> float:
        return self.end_s - self.start_s

    def features_json(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("rank", "score", "peak_s", "start_s", "end_s", "kind"):
            d.pop(k)
        return d


@dataclass
class ScoreParams:
    min_score: float = 1.0
    top_n: int = 10
    merge_gap_s: float = 45
    pre_roll_s: float = 25
    post_roll_s: float = 5
    min_clip_s: float = 20
    max_clip_s: float = 180
    feature_cap: float = 64.0
    edge_ignore_s: float = 60
    # karaoke
    song_len_s: float = 300          # typical song incl. intro; window ends at the applause
    song_max_s: float = 420
    song_applause_z: float = 4.0     # applause burst strength that marks a song end
    song_gap_s: float = 90           # two song ends are at least this far apart


def combined_score(tl: Timeline, weights: dict[str, float], cap: float) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Weighted mean of √z over positive z (capped). Returns (score, per-signal contribution).

    Busy chats routinely produce z of 15–30; the square root keeps "bigger reaction
    ranks higher" without letting one signal drown out the others.
    """
    total_w = sum(w for f, w in weights.items() if f in tl.z and w > 0) or 1.0
    contrib = {
        f: np.sqrt(np.clip(tl.z[f], 0.0, cap)) * w / total_w
        for f, w in weights.items() if f in tl.z and w > 0
    }
    score = np.sum(list(contrib.values()), axis=0) if contrib else np.zeros(tl.n_bins)
    return score, contrib


def find_peaks(score: np.ndarray, min_score: float, gap_bins: int, ignore_bins: int = 0) -> list[int]:
    """Local maxima ≥ min_score, strongest first, at least gap_bins apart."""
    n = len(score)
    lo, hi = ignore_bins, n - ignore_bins
    cands = [
        i for i in range(max(lo, 0), min(hi, n))
        if score[i] >= min_score and score[i] == score[max(0, i - 2): i + 3].max()
    ]
    cands.sort(key=lambda i: -score[i])
    picked: list[int] = []
    for i in cands:
        if all(abs(i - j) >= gap_bins for j in picked):
            picked.append(i)
    return picked


def adaptive_threshold(score: np.ndarray, min_score: float, pct: float = 97.0,
                       floor_frac: float = 0.6) -> float:
    """min_score, lowered to the stream's own top-3% level on calm streams (never below
    60% of min_score), so quiet chats still yield candidates."""
    if len(score) == 0:
        return min_score
    return max(min_score * floor_frac, min(min_score, float(np.percentile(score, pct))))


def reaction_extent(score: np.ndarray, peak: int, min_score: float, max_bins: int) -> tuple[int, int]:
    """Bins around the peak where the score stays high (≥40% of the peak)."""
    thr = max(min_score * 0.5, score[peak] * 0.4)
    a = peak
    while a > 0 and peak - a < max_bins and score[a - 1] >= thr:
        a -= 1
    b = peak
    while b < len(score) - 1 and b - peak < max_bins and score[b + 1] >= thr:
        b += 1
    return a, b


# ── transcript → sentences, for natural cut points ────────────────────────────

_SENT_END = re.compile(r"[。！？!?.…♪]$|\[(?:音楽|music|拍手|applause)\]$", re.I)
# Scripts written without spaces between words (kana, CJK ideographs, full-width forms).
_NO_SPACE = re.compile(r"[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")


def _join(a: str, b: str) -> str:
    """Join two caption lines: no space inside Japanese/Chinese text (lines break
    mid-word there), one space otherwise (English and Indonesian break between words)."""
    if not a or not b or a[-1].isspace() or b[0].isspace():
        return a + b
    if _NO_SPACE.match(a[-1]) or _NO_SPACE.match(b[0]):
        return a + b
    return f"{a} {b}"


def sentences(segments: list[Segment], gap_s: float = 1.2, max_len_s: float = 25) -> list[Segment]:
    """Merge caption lines (which often break mid-word) into sentence-ish spans."""
    out: list[Segment] = []
    cur: Segment | None = None
    for s in segments:
        if cur and (s.start - cur.end > gap_s or s.end - cur.start > max_len_s):
            out.append(cur)
            cur = None
        if cur is None:
            cur = Segment(s.start, s.end, s.text)
        else:
            cur.end, cur.text = s.end, _join(cur.text, s.text)
        if _SENT_END.search(s.text):
            out.append(cur)
            cur = None
    if cur:
        out.append(cur)
    return out


def snap(t: float, sents: list[Segment], *, back_s: float, fwd_s: float, to: str) -> float:
    """Move t to the start (to='start') or end (to='end') of the sentence it falls in,
    if that is within back_s/fwd_s."""
    for s in sents:
        if s.start <= t < s.end:
            if to == "start" and t - s.start <= back_s:
                return s.start
            if to == "end" and s.end - t <= fwd_s:
                return s.end
            return t
    return t


def clip_window(peak_s: float, react_a: float, react_b: float, p: ScoreParams,
                sents: list[Segment], duration: float) -> tuple[float, float]:
    start = react_a - p.pre_roll_s
    end = react_b + p.post_roll_s
    if end - start > p.max_clip_s:  # the event is before the reaction: keep more lead-in
        start = max(start, peak_s - 0.75 * p.max_clip_s)
        end = start + p.max_clip_s
    if end - start < p.min_clip_s:
        missing = p.min_clip_s - (end - start)
        start -= missing * 0.7
        end += missing * 0.3
    if sents:
        start = snap(start, sents, back_s=10, fwd_s=0, to="start")
        end = snap(end, sents, back_s=0, fwd_s=8, to="end")
    return max(0.0, round(start, 1)), min(duration, round(end, 1))


def greeting_share(messages: list[ChatMessage], a: float, b: float) -> float:
    texts = [m.text for m in messages if a <= m.t <= b and m.text]
    if not texts:
        return 0.0
    return sum(Lexicon.is_greeting(t) for t in texts) / len(texts)


def top_messages(messages: list[ChatMessage], a: float, b: float, n: int = 6) -> list[tuple[str, int]]:
    c = Counter(
        canonical(m.text) for m in messages
        if a <= m.t <= b and m.text and not (m.is_owner or m.is_mod)
    )
    c.pop("", None)
    return c.most_common(n)


def reasons_at(tl: Timeline, contrib: dict[str, np.ndarray], i: int, k: int = 4) -> list[dict[str, Any]]:
    """The signals that contributed most at bin i."""
    rows = sorted(((f, float(c[i])) for f, c in contrib.items()), key=lambda r: -r[1])
    return [{"signal": f, "contrib": round(v, 2), "z": round(float(tl.z[f][i]), 1)}
            for f, v in rows[:k] if v > 0]


def pick_moments(
    tl: Timeline,
    weights: dict[str, float],
    messages: list[ChatMessage],
    transcript: list[Segment],
    duration: float,
    p: ScoreParams,
    *,
    songs: bool = False,
) -> tuple[list[Moment], np.ndarray]:
    """Rank moments. With songs=True (karaoke), applause bursts become whole-song
    moments and talk reactions are scored without applause."""
    score, contrib = combined_score(tl, weights, p.feature_cap)
    sents = sentences(transcript)
    if not songs:
        moments = _reactions(tl, score, contrib, messages, sents, duration, p)
        return _rank(moments, p.top_n), score

    song_list = _songs(tl, score, contrib, messages, sents, duration, p)
    talk_w = {f: (0.0 if f == "applause" else w) for f, w in weights.items()}
    talk_score, talk_contrib = combined_score(tl, talk_w, p.feature_cap)
    talk = _reactions(tl, talk_score, talk_contrib, messages, sents, duration, p)
    # Song windows are generous (they may include the chat before a song), so only drop a
    # talk moment when its reaction *is* a song's applause wave.
    talk = [m for m in talk if not any(
        m.react_start_s < s.react_end_s and s.react_start_s < m.react_end_s for s in song_list)]
    ranked = _rank(song_list, p.top_n) + _rank(talk, max(3, p.top_n // 3))
    return _rank(ranked, len(ranked)), score


def _rank(moments: list[Moment], n: int) -> list[Moment]:
    moments = sorted(moments, key=lambda m: -m.score)[:n]
    for r, m in enumerate(moments, 1):
        m.rank = r
    return moments


def _songs(tl: Timeline, score: np.ndarray, contrib: dict[str, np.ndarray],
           messages: list[ChatMessage], sents: list[Segment], duration: float,
           p: ScoreParams) -> list[Moment]:
    bs = tl.bin_s
    applause = tl.z["applause"]
    ends = sorted(find_peaks(applause, p.song_applause_z, max(1, round(p.song_gap_s / bs)),
                             ignore_bins=round(p.edge_ignore_s / bs)))
    replay = np.clip(tl.z["replay"], 0.0, p.feature_cap)
    out: list[Moment] = []
    prev_end = 0.0
    for i in ends:
        a, b = reaction_extent(applause, i, p.song_applause_z, max_bins=round(60 / bs))
        song_end = a * bs + 10  # last note + start of the applause
        start = max(prev_end + 20, song_end - p.song_len_s, 0.0)
        start = snap(start, sents, back_s=10, fwd_s=0, to="start") if sents else start
        end = min(duration, song_end + 10)
        if end - start > p.song_max_s:
            start = end - p.song_max_s
        prev_end = song_end
        win = slice(int(start // bs), max(int(start // bs) + 1, int(end // bs)))
        out.append(Moment(
            rank=0, kind="song",
            score=round(float(score[i] + 0.5 * replay[win].mean()), 2),
            peak_s=i * bs + bs / 2, react_start_s=a * bs, react_end_s=(b + 1) * bs,
            start_s=round(start, 1), end_s=round(end, 1),
            reasons=reasons_at(tl, contrib, i),
            top_messages=top_messages(messages, start, end),
        ))
    return out


def _reactions(tl: Timeline, score: np.ndarray, contrib: dict[str, np.ndarray],
               messages: list[ChatMessage], sents: list[Segment], duration: float,
               p: ScoreParams) -> list[Moment]:
    bs = tl.bin_s
    gap = max(1, round(p.merge_gap_s / bs))
    thr = adaptive_threshold(score, p.min_score)
    peaks = find_peaks(score, thr, gap, ignore_bins=round(p.edge_ignore_s / bs))
    moments: list[Moment] = []
    for i in peaks:
        a, b = reaction_extent(score, i, thr, max_bins=round(p.max_clip_s / bs))
        react_a, react_b = a * bs, (b + 1) * bs
        peak_s = i * bs + bs / 2
        start, end = clip_window(peak_s, react_a, react_b, p, sents, duration)
        # Greeting waves (stream opening, raid hellos) are not clip material.
        penalty = 1.0 - greeting_share(messages, react_a, react_b)
        moments.append(Moment(
            rank=0, score=round(float(score[i]) * penalty, 2), peak_s=peak_s,
            react_start_s=react_a, react_end_s=react_b, start_s=start, end_s=end,
            reasons=reasons_at(tl, contrib, i),
            top_messages=top_messages(messages, react_a, react_b),
        ))
    return merge_overlapping(moments, p.max_clip_s)


def merge_overlapping(moments: list[Moment], max_clip_s: float) -> list[Moment]:
    """Merge overlapping clips into one if the union is not much longer than max_clip_s
    (two clips of the same scene are wasted editing time). The stronger moment's score,
    reasons and chat are kept."""
    out: list[Moment] = []
    for m in sorted(moments, key=lambda m: -m.score):
        for o in out:
            overlap = min(o.end_s, m.end_s) - max(o.start_s, m.start_s)
            union = max(o.end_s, m.end_s) - min(o.start_s, m.start_s)
            if overlap > 0 and union <= max_clip_s * 1.25:
                o.start_s, o.end_s = min(o.start_s, m.start_s), max(o.end_s, m.end_s)
                o.react_start_s = min(o.react_start_s, m.react_start_s)
                o.react_end_s = max(o.react_end_s, m.react_end_s)
                break
        else:
            out.append(m)
    return out


def fmt_ts(seconds: float) -> str:
    s = max(0, int(seconds))
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
