"""Put Whisper's words where the speech really is.

Whisper's word times are loose (large-v3-turbo especially): measured on real clips, 15-25%
of words start where nobody is talking, pause edges are off by 0.25-0.4 s, and words get
stretched over pauses ("i'm" lasting 2.3 s). Cuts made on those times clip words and miss
pauses.

So Whisper says *what* was said, and Silero VAD (the speech detector that ships with
faster-whisper, ~30 ms precise) says *when*: each word is pulled inside the stretch of real
speech it overlaps most. Words VAD has nothing near (a shout under loud game audio) keep
Whisper's times.

words.json gets "aligned": VERSION and "speech": [[start, end], ...] (every stretch of
voice, including laughs and screams that have no words); Whisper's own times are kept in
words.whisper.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

VERSION = 1
RAW_FILE = "words.whisper.json"
RATE = 16000
NEAR_S = 0.6             # a word with no speech under it moves into speech this close
MIN_WORD_S = 0.06
_VAD = {"threshold": 0.5, "min_speech_duration_ms": 120, "min_silence_duration_ms": 150, "speech_pad_ms": 40}


def speech(audio: Any) -> list[tuple[float, float]]:
    """Stretches of voice in 16 kHz mono float samples, in seconds."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    return [(s["start"] / RATE, s["end"] / RATE) for s in get_speech_timestamps(audio, VadOptions(**_VAD))]


def retime(words: list[list[Any]], regions: list[tuple[float, float]]) -> list[list[Any]]:
    """[[start, end, text, p], ...] → the same words, each inside the speech it overlaps most,
    in order and not overlapping."""
    out: list[list[Any]] = []
    prev_end = 0.0
    for w in words:
        s, e = float(w[0]), float(w[1])
        over = [(min(e, b) - max(s, a), a, b) for a, b in regions if a < e and b > s]
        if over:
            _, a, b = max(over)
            s, e = max(s, a), min(e, b)
        else:
            before = [(a, b) for a, b in regions if b <= s and s - b <= NEAR_S]
            after = [(a, b) for a, b in regions if a >= e and a - e <= NEAR_S]
            length = e - s
            if after and (not before or after[0][0] - e <= s - before[-1][1]):
                a, b = after[0]
                s, e = a, min(b, a + length)
            elif before:
                a, b = before[-1]
                s, e = max(a, b - length), b
        s = max(s, prev_end)
        e = max(e, s + MIN_WORD_S)
        out.append([round(s, 3), round(e, 3), *w[2:]])
        prev_end = e
    return out


def align(data: dict[str, Any], regions: list[tuple[float, float]]) -> dict[str, Any]:
    """words.json's contents with every word re-timed, line edges following their words."""
    flat = [w for line in data.get("lines") or [] for w in line.get("words") or []]
    timed = iter(retime(flat, regions))
    lines = []
    for line in data.get("lines") or []:
        ws = [next(timed) for _ in line.get("words") or []]
        lines.append(line | {"words": ws} | ({"start": ws[0][0], "end": ws[-1][1]} if ws else {}))
    return data | {"lines": lines, "aligned": VERSION, "speech": [[round(a, 3), round(b, 3)] for a, b in regions]}


def write(folder: Path, data: dict[str, Any], audio: Any) -> dict[str, Any]:
    """Align `data` (Whisper's words for this folder's clip) on `audio`, keep Whisper's
    times in words.whisper.json, write words.json. Returns the aligned data."""
    from chatwatcher.subs.transcribe import WORDS_FILE
    raw = {k: v for k, v in data.items() if k not in ("aligned", "speech")}
    (folder / RAW_FILE).write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    out = align(raw, speech(audio))
    tmp = folder / (WORDS_FILE + ".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    tmp.replace(folder / WORDS_FILE)
    return out


def ensure(folder: Path, clip: Path, data: dict[str, Any]) -> dict[str, Any]:
    """words.json aligned (once per clip, a second or two); data as it is if that fails."""
    if data.get("aligned") == VERSION:
        return data
    try:
        from chatwatcher.subs.transcribe import decode_audio
        raw = data
        rp = folder / RAW_FILE
        if rp.is_file():                         # aligned by an older version: start from Whisper's times
            raw = json.loads(rp.read_text(encoding="utf-8"))
        return write(folder, raw, decode_audio(clip))
    except Exception:                            # noqa: BLE001 — no VAD, no audio: Whisper's times will do
        return data
