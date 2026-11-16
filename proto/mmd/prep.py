"""Throwaway: cut the Short's audio for one moment and write the lip-sync track.

    .venv/bin/python proto/mmd/prep.py 57

Writes media/audio.m4a and media/track.json. Times in track.json start at 0 = the first
frame of the Short's cut. Mouth shapes come from the letters of each word, timed letter by letter with the
project's CTC aligner; how wide it opens follows the loudness of the voice band. Voice
with no words in it is marked as laughter.
"""
from __future__ import annotations

import json
import re
import unicodedata
import sqlite3
import subprocess
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
ROOT = HERE.parent.parent
RATE = 60  # envelope samples per second
SR = 16000


def run(*args: str) -> bytes:
    return subprocess.run(args, check=True, capture_output=True).stdout


def envelope(pcm: np.ndarray) -> np.ndarray:
    hop = SR // RATE
    n = len(pcm) // hop
    rms = np.sqrt((pcm[: n * hop].reshape(n, hop).astype(np.float64) ** 2).mean(axis=1))
    floor, top = np.percentile(rms, 25), np.percentile(rms, 97)
    env = np.clip((rms - floor * 1.6) / max(top - floor * 1.6, 1e-6), 0, 1)
    return env ** 0.7


# English spelling → mouth shapes. A cue is [time, vowel, how open 0..1]: vowels open the
# mouth, m/b/p shut it, other consonants hold the vowel before them half open.
PAIRS = {"ee": "i", "ea": "i", "ie": "i", "oo": "u", "ew": "u", "ue": "u", "ou": "a", "ow": "o", "oa": "o",
         "ai": "e", "ay": "e", "ey": "e", "oi": "o", "oy": "o", "au": "o", "aw": "o"}
SINGLE = {"a": "a", "e": "e", "i": "i", "y": "i", "o": "o", "u": "a"}
SHUT, ROUND = set("mbp"), set("w")


def shapes(chars: list[tuple[str, float]], end: float) -> list[list]:
    """chars: (letter, the time it is heard) for one word → mouth cues."""
    word = "".join(c for c, _ in chars)
    if len(word) > 2 and word.endswith("e") and re.search(r"[aiouy]", word[:-1]):
        chars = chars[:-1]                                   # silent e
    out, hold, i = [], "a", 0
    while i < len(chars):
        c, t = chars[i]
        pair = c + (chars[i + 1][0] if i + 1 < len(chars) else "")
        if pair in PAIRS:
            hold = PAIRS[pair]; out.append([t, hold, 1.0]); i += 2
        elif c in SINGLE and not (c == "y" and i == 0):
            hold = SINGLE[c]; out.append([t, hold, 1.0]); i += 1
        elif c in SHUT:
            out.append([t, hold, 0.0]); i += 1
        elif c in ROUND:
            hold = "u"; out.append([t, "u", 0.7]); i += 1
        else:
            if not out or out[-1][2] != 0.45 or out[-1][1] != hold:
                out.append([t, hold, 0.45])
            i += 1
    out.append([end, hold, 0.0])
    return out


def letter_times(audio: np.ndarray, words: list[tuple[float, float, str]]) -> list[list[tuple[str, float]] | None]:
    """When each letter of each word is heard, from the project's CTC aligner (20 ms steps).
    words are (start, end, text) in the audio's own time. None for a word it could not place."""
    sys.path.insert(0, str(ROOT / "src"))
    from chatwatcher.subs import align
    em = align.emissions(align._session(align.model_path(ROOT / "data" / "models")), audio)
    tokens, owner, windows = [align.STAR], [None], [(0, len(em))]
    for k, (a, b, text) in enumerate(words):
        ids = align.letters(text)
        keep = [c for c in unicodedata.normalize("NFKD", text.lower().replace("\u2019", "'")) if c in align._INDEX]
        if not ids or len(keep) != len(ids):
            continue
        win = (max(0, int((a - 0.3) / align.FRAME_S)), int(np.ceil((b + 0.3) / align.FRAME_S)))
        for n, c in enumerate(keep):
            tokens.append(ids[n]); owner.append((k, c)); windows.append(win)
        tokens.append(align.STAR); owner.append(None); windows.append((0, len(em)))
    path = align.viterbi(em, tokens, windows) or align.viterbi(em, tokens)
    out: list = [None] * len(words)
    if path is None:
        return out
    seen = set()
    for frame, j in enumerate(path):
        if j >= 0 and owner[j] is not None and j not in seen:
            seen.add(j)
            k, c = owner[j]
            out[k] = (out[k] or []) + [(c, frame * align.FRAME_S)]
    return out


def laughs(audio: np.ndarray, env: np.ndarray, words: list[list], dur: float) -> list[list[float]]:
    """Voice with no words in it: laughing (or a scream, a hum). Voice = the project's VAD,
    or a loud stretch of the voice band."""
    voiced = np.zeros(len(env), dtype=bool)
    try:
        from chatwatcher.subs import align
        for a, b in align.speech(audio):
            voiced[int(a * RATE): int(b * RATE)] = True
    except Exception as err:                                 # noqa: BLE001
        print("no VAD:", err)
    k = RATE // 4
    loud = np.convolve(env, np.ones(k) / k, mode="same") > 0.16
    said = np.zeros(len(env), dtype=bool)
    for a, b, _ in words:
        said[max(int((a - 0.12) * RATE), 0): int((b + 0.12) * RATE)] = True
    on = (voiced | loud) & ~said
    spans, start = [], None
    for i, v in enumerate(list(on) + [False]):
        if v and start is None:
            start = i
        elif not v and start is not None:
            spans.append([start / RATE, i / RATE]); start = None
    merged: list[list[float]] = []
    for a, b in spans:
        if merged and a - merged[-1][1] < 0.35:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [[round(a, 2), round(min(b, dur), 2), round(float(env[int(a * RATE): int(b * RATE)].mean()), 2)]
            for a, b in merged if b - a >= 0.4]


def best_offset(env: np.ndarray, words: list, seg0_clip: float) -> float:
    """words.json may not share the clip's clock: slide it until speech lines up with sound."""
    best, best_score = 0.0, -1.0
    for off in np.arange(-1.0, 6.0, 0.02):
        mask = np.zeros(len(env))
        for t0, t1, *_ in words:
            a, b = int((t0 + off - seg0_clip) * RATE), int((t1 + off - seg0_clip) * RATE)
            mask[max(a, 0): max(min(b, len(env)), 0)] = 1
        if mask.sum() < RATE:
            continue
        score = float((env * mask).sum() / mask.sum() - (env * (1 - mask)).sum() / max((1 - mask).sum(), 1))
        if score > best_score:
            best, best_score = float(off), score
    return round(best, 2)


def main(pk: int) -> None:
    db = sqlite3.connect(f"file:{ROOT / 'data' / 'chatwatcher.db'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    clip = db.execute("select * from clips where moment_id=?", (pk,)).fetchone()
    short = db.execute("select * from shorts where moment_id=?", (pk,)).fetchone()
    seg = json.loads(short["segments_json"])[0]
    zero = clip["zero_s"]
    s0, s1 = seg[0] - zero, seg[1] - zero  # clip time
    dur = s1 - s0
    media = HERE / "media"
    media.mkdir(exist_ok=True)

    run("ffmpeg", "-y", "-v", "error", "-ss", f"{s0:.3f}", "-t", f"{dur:.3f}", "-i", clip["path"],
        "-vn", "-c:a", "aac", "-b:a", "160k", str(media / "audio.m4a"))
    raw = run("ffmpeg", "-v", "error", "-ss", f"{s0:.3f}", "-t", f"{dur:.3f}", "-i", clip["path"],
              "-vn", "-ac", "1", "-ar", str(SR), "-af", "highpass=f=180,lowpass=f=3200",
              "-f", "s16le", "-")
    env = envelope(np.frombuffer(raw, dtype=np.int16))
    plain = run("ffmpeg", "-v", "error", "-ss", f"{s0:.3f}", "-t", f"{dur:.3f}", "-i", clip["path"],
                "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-")
    audio = np.frombuffer(plain, dtype=np.int16).astype(np.float32) / 32768

    data = json.loads((Path(clip["folder"]) / "words.json").read_text())
    words = [w for line in data["lines"] for w in line["words"]]
    off = best_offset(env, words, s0)
    mine = [(t0 + off - s0, t1 + off - s0, text) for t0, t1, text, *_ in words]
    mine = [w for w in mine if w[1] > 0 and w[0] < dur]
    timed = letter_times(audio, mine)

    out_words, cues, placed = [], [], 0
    for (a, b, text), chars in zip(mine, timed):
        if chars:                                            # heard letter by letter
            placed += 1
            a, b = chars[0][1], max(chars[-1][1] + 0.08, chars[0][1] + 0.06)
        else:                                                # fall back: spread evenly over Whisper's time
            keep = [c for c in re.sub(r"[^a-z]", "", text.lower())] or ["a"]
            chars = [(c, a + i * (b - a) / len(keep)) for i, c in enumerate(keep)]
        out_words.append([round(a, 3), round(b, 3), text.strip()])
        cues += [[round(t, 3), v, k] for t, v, k in shapes(chars, b)]
    cues.sort(key=lambda c: c[0])
    ha = [h for h in laughs(audio, env, out_words, dur) if h[2] >= 0.08]

    lines = [{"t0": round(c["t0"] - seg[0], 3), "t1": round(c["t1"] - seg[0], 3), "text": c["text"]}
             for c in json.loads(short["subs_json"] or "[]") if c["t1"] > seg[0] and c["t0"] < seg[1]]

    # Voice inside a subtitle line you wrote is her talking (Whisper missed the words), not laughing.
    def subtitled(a: float, b: float) -> float:
        return sum(max(0.0, min(b, l["t1"]) - max(a, l["t0"])) for l in lines) / (b - a)
    mumbles = [h[:2] for h in ha if subtitled(h[0], h[1]) > 0.5]
    ha = [h for h in ha if subtitled(h[0], h[1]) <= 0.5]

    (media / "track.json").write_text(json.dumps({
        "moment": pk, "duration": round(dur, 3), "rate": RATE, "word_offset": off,
        "env": [round(float(v), 3) for v in env], "words": out_words, "mouth": cues, "laughs": ha, "mumbles": mumbles, "lines": lines,
    }))
    print(f"duration {dur:.2f}s, {len(out_words)} words ({placed} timed letter by letter), {len(cues)} mouth cues, "
          f"word clock offset {off:+.2f}s, {len(lines)} subtitle lines")
    print("laughs [start, end, loudness]:", ha)
    print("speech with no words found (mouth follows loudness):", mumbles)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 57)
