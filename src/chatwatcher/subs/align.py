"""Put Whisper's words where they are really said: CTC forced alignment.

Whisper says *what* was said, but its word times come from its attention and are loose:
±0.2-0.5 s, and words stretched over pauses ("can't" lasting 1.6 s). A wav2vec2 CTC model
(MMS, trained on 1,100+ languages, letters a-z) scores every 20 ms of audio for every
letter; forced alignment finds the best place for Whisper's exact text in that grid, so
each word gets the time its letters are heard (to within a frame or two).

Between words sits a "star" token that can stand for anything (game audio, a laugh,
speech Whisper missed), so one misheard or invented word doesn't drag its neighbours.
Words with no letters the model knows (numbers, Japanese, emoji) are placed between their
aligned neighbours, in Whisper's proportions.

(The first version clamped words into Silero VAD's speech stretches. Over loud game audio
VAD misses whole sentences, so that moved words away from the speech.)

words.json gets "aligned": VERSION and "speech": [[start, end], ...], every stretch of
voice (Silero VAD merged with the aligned words), which shows laughs and screams that
have no words and keeps cuts out of the voice. Whisper's own times are kept in
words.whisper.json. The model (ONNX, 1.3 GB) is downloaded into <data_dir>/models/mms-fa
the first time."""

from __future__ import annotations

import json
import math
import threading
import unicodedata
import urllib.request
from pathlib import Path
from typing import Any, Callable

VERSION = 2
RAW_FILE = "words.whisper.json"
MODEL_DIR = "mms-fa"
MODEL_FILE = "model.onnx"
MODEL_URL = "https://huggingface.co/deskpai/ctc_forced_aligner/resolve/main/04ac86b67129634da93aea76e0147ef3.onnx"
RATE = 16000
FRAME_S = 0.02            # the model's step
WINDOW_S, CONTEXT_S = 30, 2   # audio goes through the model in 30 s windows with 2 s either side
CHUNK_S = 90.0            # longest stretch aligned in one pass (memory grows with frames × letters)
NEAR_S = 0.6              # a word is looked for this far either side of Whisper's time
MIN_WORD_S = 0.06
END_PAD_S = 0.04          # a word's last letter is heard a little past its last peak
_VAD = {"threshold": 0.35, "min_speech_duration_ms": 120, "min_silence_duration_ms": 150, "speech_pad_ms": 40}

# The model's outputs: 0 = blank, 1-3 = unused specials, then these letters.
LETTERS = "aienoutsrmklgdhybpwcvjzf'qx"
BLANK = 0
_INDEX = {c: i + 4 for i, c in enumerate(LETTERS)}
STAR = 31                 # an extra column: "anything else" (speech Whisper missed, a laugh, the game)
STAR_COST = 1.0


class AlignError(Exception):
    pass


# ── the model ────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_sessions: dict[tuple[str, int], Any] = {}


def model_path(models_dir: Path) -> Path:
    return Path(models_dir) / MODEL_DIR / MODEL_FILE


def download(models_dir: Path, say: Callable[[str], None] | None = None) -> Path:
    """The model file, downloaded first if it isn't there yet."""
    path = model_path(models_dir)
    if path.is_file():
        return path
    (say or (lambda _m: None))("Downloading the word-timing model (1.3 GB, only this once)…")
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(".part")
    try:
        with urllib.request.urlopen(MODEL_URL, timeout=60) as r, open(part, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        part.replace(path)
    except OSError as e:
        part.unlink(missing_ok=True)
        raise AlignError(f"couldn't download the word-timing model: {e}") from e
    return path


def _session(path: Path, threads: int = 0) -> Any:
    import onnxruntime as ort
    key = (str(path), threads)
    with _lock:
        if key not in _sessions:
            opts = ort.SessionOptions()
            if threads:
                opts.intra_op_num_threads = threads
            _sessions[key] = ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])
        return _sessions[key]


def emissions(session: Any, audio: Any) -> Any:
    """(frames, 32) log-probabilities, one row per FRAME_S of audio; the last column is
    the star."""
    import numpy as np
    win, ctx = WINDOW_S * RATE, CONTEXT_S * RATE
    n = len(audio)
    count = max(1, math.ceil(n / win))
    padded = np.pad(np.asarray(audio, dtype=np.float32), (ctx, ctx + count * win - n))
    skip, keep = round(CONTEXT_S / FRAME_S), round(WINDOW_S / FRAME_S)
    parts = []
    with _lock:                                   # one at a time: it already uses every core
        for i in range(count):
            x = padded[i * win: i * win + win + 2 * ctx]      # as it is: normalising loses quiet lines
            logits = session.run(["logits"], {"input_values": x[None, :]})[0][0]
            parts.append(logits[skip: skip + keep])
    out = np.concatenate(parts)[: math.ceil(n / RATE / FRAME_S)].astype(np.float64)
    out -= out.max(axis=1, keepdims=True)
    out -= np.log(np.exp(out).sum(axis=1, keepdims=True))
    return with_star(out)


def with_star(logp: Any) -> Any:
    """The model's log-probabilities plus the star column: whatever is heard in that frame,
    less STAR_COST, so a letter that is really there always beats it."""
    import numpy as np
    return np.concatenate([logp, logp.max(axis=1, keepdims=True) - STAR_COST], axis=1)


# ── the text ─────────────────────────────────────────────────────────────────

def letters(word: str) -> list[int]:
    """A word as the model's letter ids ("Whaaat?" → w h a a a t); [] if it has none."""
    text = unicodedata.normalize("NFKD", str(word).lower().replace("’", "'").replace("‘", "'"))
    ids = [_INDEX[c] for c in text if c in _INDEX]
    return [] if all(i == _INDEX["'"] for i in ids) else ids


# ── forced alignment ─────────────────────────────────────────────────────────

def viterbi(em: Any, tokens: list[int], windows: list[tuple[int, int]] | None = None) -> list[int] | None:
    """The best CTC path of `tokens` through `em` (frames × classes): for every frame, the
    index in `tokens` it belongs to, or -1 for a blank. windows: (first, last frame) each
    token may be heard in; a blank between two tokens of the same window keeps to it too.
    None if the tokens don't fit."""
    import numpy as np
    T, L = len(em), len(tokens)
    ext = np.full(2 * L + 1, BLANK, dtype=np.int64)
    ext[1::2] = tokens
    S = len(ext)
    need = L + sum(1 for a, b in zip(tokens, tokens[1:]) if a == b)
    if L == 0 or T < need:
        return None
    lo, hi = np.zeros(S, dtype=np.int64), np.full(S, T, dtype=np.int64)
    if windows:
        for j, (a, b) in enumerate(windows):
            lo[2 * j + 1], hi[2 * j + 1] = a, b
            if j and windows[j - 1] == (a, b):             # inside one word
                lo[2 * j], hi[2 * j] = a, b
    skip = np.zeros(S, dtype=bool)
    skip[2:] = (ext[2:] != BLANK) & (ext[2:] != ext[:-2])
    neg = -1e30

    def cost(t: int) -> Any:
        c = em[t, ext].copy()
        c[(t < lo) | (t > hi)] = neg
        return c

    alpha = np.full(S, neg)
    alpha[:2] = cost(0)[:2]
    back = np.zeros((T, S), dtype=np.int8)
    stay = np.empty(S)
    one = np.empty(S)
    two = np.empty(S)
    for t in range(1, T):
        stay[:] = alpha
        one[0], one[1:] = neg, alpha[:-1]
        two[:2], two[2:] = neg, alpha[:-2]
        two[~skip] = neg
        cand = np.stack([stay, one, two])
        choice = cand.argmax(axis=0)
        alpha = np.maximum(cand[choice, np.arange(S)] + cost(t), neg)
        back[t] = choice
    s = S - 1 if alpha[S - 1] >= alpha[S - 2] else S - 2
    if alpha[s] <= neg / 2:
        return None
    path = [0] * T
    for t in range(T - 1, -1, -1):
        path[t] = (s - 1) // 2 if s % 2 else -1
        s -= int(back[t, s])
    return path


def _align_chunk(em: Any, offset: float, words: list[tuple[list[int], float, float]]) -> list[tuple[float, float] | None]:
    """(start, end) in seconds for each word of a chunk (None: no letters, or no fit).
    words: (letter ids, Whisper's start, end in clip seconds). A word is looked for within
    NEAR_S of where Whisper heard it: with nothing to hold it, a short word wanders off to
    a louder match seconds away."""
    tokens, owner = [STAR], [-1]
    windows: list[tuple[int, int]] = [(0, len(em))]
    for k, (ids, ws, we) in enumerate(words):
        if ids:
            win = (max(0, int((ws - NEAR_S - offset) / FRAME_S)), int(math.ceil((we + NEAR_S - offset) / FRAME_S)))
            tokens += ids + [STAR]
            owner += [k] * len(ids) + [-1]
            windows += [win] * len(ids) + [(0, len(em))]
    out: list[tuple[float, float] | None] = [None] * len(words)
    if len(tokens) == 1:
        return out
    path = viterbi(em, tokens, windows)
    if path is None:                              # Whisper's times are way off: align without them
        path = viterbi(em, tokens)
    if path is None:
        return out
    first: dict[int, int] = {}
    last: dict[int, int] = {}
    for t, j in enumerate(path):
        if j >= 0 and owner[j] >= 0:
            first.setdefault(owner[j], t)
            last[owner[j]] = t
    for k in first:
        out[k] = (offset + first[k] * FRAME_S, offset + (last[k] + 1) * FRAME_S + END_PAD_S)
    return out


def _chunks(flat: list[list[Any]], duration: float) -> list[tuple[float, float, int, int]]:
    """(start s, end s, first word, past last word): the audio split between words, at
    most about CHUNK_S long, so each pass stays small."""
    out, a, first = [], 0.0, 0
    for k in range(1, len(flat) + 1):
        if k == len(flat):
            out.append((a, duration, first, k))
        elif float(flat[k][1]) - a > CHUNK_S and k > first:
            end, start = float(flat[k - 1][1]), float(flat[k][0])
            cut = (end + start) / 2 if start >= end else start
            out.append((a, cut, first, k))
            a, first = cut, k
    return out


def retime(words: list[list[Any]], em: Any, duration: float) -> list[list[Any]]:
    """[[start, end, text, p], ...] → the same words at their aligned times, in order and
    not overlapping. Words that couldn't be aligned sit between their neighbours."""
    spans: list[tuple[float, float] | None] = []
    for a, b, i, j in _chunks(words, duration):
        f0, f1 = int(a / FRAME_S), min(int(math.ceil(b / FRAME_S)), len(em))
        spans += _align_chunk(em[f0:f1], f0 * FRAME_S, [(letters(w[2]), float(w[0]), float(w[1])) for w in words[i:j]])
    out: list[list[Any]] = []
    k = 0
    while k < len(words):
        if spans[k] is not None:
            s, e = spans[k]
            out.append([s, e, *words[k][2:]])
            k += 1
            continue
        j = k                                     # a run of unaligned words: Whisper's times,
        while j < len(words) and spans[j] is None:    # squeezed in between the aligned neighbours
            j += 1
        lo = out[-1][1] if out else 0.0
        hi = spans[j][0] if j < len(words) else duration
        a = float(words[k][0])
        b = max(float(words[j - 1][1]), a + MIN_WORD_S)
        need = MIN_WORD_S * (j - k)
        to_a, to_b = max(a, lo), min(b, hi)
        if to_b - to_a < need:
            to_a, to_b = (lo, hi) if hi - lo >= need else (a, b)   # no room: as Whisper had them
        scale = (to_b - to_a) / (b - a)
        out += [[to_a + (float(w[0]) - a) * scale, to_a + (float(w[1]) - a) * scale, *w[2:]] for w in words[k:j]]
        k = j
    prev = 0.0
    for w in out:                                 # in order, no overlaps, nothing too short
        w[0] = round(max(w[0], prev), 3)
        w[1] = round(max(min(w[1], duration), w[0] + MIN_WORD_S), 3)
        prev = w[1]
    return out


# ── speech ───────────────────────────────────────────────────────────────────

def speech(audio: Any) -> list[tuple[float, float]]:
    """Stretches of voice in 16 kHz mono float samples, in seconds (Silero VAD)."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    return [(s["start"] / RATE, s["end"] / RATE) for s in get_speech_timestamps(audio, VadOptions(**_VAD))]


def merge(spans: list[tuple[float, float]], gap: float = 0.15) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


# ── words.json ───────────────────────────────────────────────────────────────

def align(data: dict[str, Any], em: Any, voice: list[tuple[float, float]]) -> dict[str, Any]:
    """words.json's contents with every word re-timed, line edges following their words."""
    duration = float(data.get("duration_s") or len(em) * FRAME_S)
    flat = [w for line in data.get("lines") or [] for w in line.get("words") or []]
    timed = iter(retime(flat, em, duration))
    lines = []
    for line in data.get("lines") or []:
        ws = [next(timed) for _ in line.get("words") or []]
        lines.append(line | {"words": ws} | ({"start": ws[0][0], "end": ws[-1][1]} if ws else {}))
    said = [(w[0], w[1]) for line in lines for w in line["words"]]
    return data | {"lines": lines, "aligned": VERSION,
                   "speech": [[round(a, 3), round(b, 3)] for a, b in merge(list(voice) + said)]}


def write(folder: Path, data: dict[str, Any], audio: Any, models_dir: Path, threads: int = 0,
          say: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Align `data` (Whisper's words for this folder's clip) on `audio`, keep Whisper's
    times in words.whisper.json, write words.json. Returns the aligned data."""
    from chatwatcher.subs.transcribe import WORDS_FILE
    raw = {k: v for k, v in data.items() if k not in ("aligned", "speech")}
    (folder / RAW_FILE).write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    session = _session(download(models_dir, say), threads)
    (say or (lambda _m: None))("Lining the words up with the speech…")
    try:
        voice = speech(audio)
    except Exception:                             # noqa: BLE001 — no VAD: the words alone mark the voice
        voice = []
    out = align(raw, emissions(session, audio), voice)
    tmp = folder / (WORDS_FILE + ".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    tmp.replace(folder / WORDS_FILE)
    return out
