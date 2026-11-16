"""Mouth shapes for the 3D stage's voice track (stage.track), from how each word sounds.

English spelling is a poor guide to the mouth ("my" is a→i, "the" barely opens, "cloud"
closes to u), so each word is looked up in the CMU Pronouncing Dictionary (130k words,
BSD licence, downloaded once into <data_dir>/models like the aligner) and its sounds are
turned into the five shapes MMD models have (あいうえお), shut lips for m/b/p, and how
open: stressed vowels open fully, unstressed ones less. Words it doesn't know fall back to
spelling rules (track.shapes).

The mouth itself is never played up (that looked worse): the scene exaggerates through
her body instead (gestures, figure.js ACTIONS). tokens() still reads your subtitles'
emphasis ("quoted" speech, *starred* words, SHOUTED words, a line ending in !), which
Claude sees in the lines it is given."""

from __future__ import annotations

import re
import urllib.request
from pathlib import Path
from typing import Any

CMU_URL = "https://raw.githubusercontent.com/cmusphinx/cmudict/master/cmudict.dict"
CMU_FILE = "cmudict.dict"
MAX_GAIN = 1.6

# ARPAbet vowels → the shapes they pass through; lax vowels open less.
VOWELS = {"AA": ("a",), "AE": ("a",), "AH": ("a",), "AO": ("o",), "AW": ("a", "u"), "AY": ("a", "i"),
          "EH": ("e",), "ER": ("u",), "EY": ("e", "i"), "IH": ("i",), "IY": ("i",), "OW": ("o", "u"),
          "OY": ("o", "i"), "UH": ("u",), "UW": ("u",)}
LAX = {"AH": 0.75, "ER": 0.6, "IH": 0.8, "UH": 0.8}
STRESS = {"1": 1.0, "2": 0.85, "0": 0.6}
SHUT = {"M", "B", "P"}
SHAPED = {"F": ("i", 0.2), "V": ("i", 0.2), "W": ("u", 0.7), "R": ("u", 0.45), "SH": ("u", 0.45),
          "CH": ("u", 0.45), "JH": ("u", 0.45), "ZH": ("u", 0.45)}
INTERJECTIONS = {"oh", "god", "wow", "omg", "what", "no", "wait", "ah", "huh", "whoa", "yes", "hey"}

_dicts: dict[str, dict[str, list[str]]] = {}


def cmudict(models_dir: Path) -> dict[str, list[str]]:
    """word → its first pronunciation, e.g. "my" → ["M", "AY1"]. {} when it can't be had."""
    path = Path(models_dir) / CMU_FILE
    if str(path) in _dicts:
        return _dicts[str(path)]
    if not path.is_file():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            with urllib.request.urlopen(CMU_URL, timeout=30) as r:
                tmp.write_bytes(r.read())
            tmp.replace(path)
        except OSError:
            return {}
    out: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) < 2:
            continue
        word = re.sub(r"\(\d+\)$", "", parts[0])
        out.setdefault(word, parts[1:])
    _dicts[str(path)] = out
    return out


def norm(word: str) -> str:
    """A word as the dictionary spells it: lower case, letters and apostrophes."""
    return re.sub(r"[^a-z']", "", word.lower().replace("’", "'")).strip("'")


def tokens(text: str, state: dict[str, bool]) -> list[tuple[str, float]]:
    """A subtitle line → (word, how much to play it up) for each word. state carries an
    open "quote" or *star* over to the next line."""
    bang = 0.12 if re.search(r"!\W*$", text) else 0.0
    out: list[tuple[str, float]] = []
    cur, quoted, starred = "", False, False

    def flush() -> None:
        word = norm(cur)
        if word:
            letters = re.sub(r"[^A-Za-z]", "", cur)
            bonus = bang + 0.15 * quoted + 0.3 * starred + 0.1 * (word in INTERJECTIONS) \
                + 0.3 * (len(letters) > 1 and letters.isupper())
            out.append((word, round(bonus, 3)))

    for ch in text + " ":
        if ch.isalnum() or ch in "'’":
            if not cur:
                quoted, starred = state.get("quote", False), state.get("star", False)
            cur += ch
            continue
        if cur:
            flush()
            cur = ""
        if ch == "“":
            state["quote"] = True
        elif ch == "”":
            state["quote"] = False
        elif ch == '"':
            state["quote"] = not state.get("quote", False)
        elif ch == "*":
            state["star"] = not state.get("star", False)
    return out


def _vowel_runs(letters: list[str]) -> list[int]:
    """The index of the first letter of each run of vowel letters ("cloud" → [2])."""
    out, prev = [], False
    for i, c in enumerate(letters):
        v = c in "aeiou" or (c == "y" and i > 0)
        if v and not prev:
            out.append(i)
        prev = v
    return out


def visemes(phones: list[str], chars: list[tuple[str, float]], start: float, end: float,
            gain: float = 1.0) -> list[list[Any]]:
    """One word's sounds (ARPAbet, with stress) timed onto its letters → mouth cues
    [t, vowel, open]. chars: (letter, when it is heard), from the aligner or spread evenly."""
    n = len(phones)
    if not n:
        return []
    times: list[float | None] = [None] * n
    vowel_at = [i for i, p in enumerate(phones) if p[:2] in VOWELS]
    runs = _vowel_runs([c for c, _ in chars])
    if vowel_at and len(runs) == len(vowel_at):           # each vowel sound on its vowel letters
        for i, r in zip(vowel_at, runs):
            times[i] = chars[r][1]
    else:                                                 # else spread over the letters
        for i in range(n):
            times[i] = chars[round(i * (len(chars) - 1) / max(n - 1, 1))][1] if chars else start + (end - start) * i / n
    times[0] = times[0] if times[0] is not None else (chars[0][1] if chars else start)
    for i in range(n):                                    # consonants between the fixed points
        if times[i] is None:
            j = next((k for k in range(i + 1, n) if times[k] is not None), None)
            lo, hi = times[i - 1], times[j] if j is not None else end
            gap = (j if j is not None else n) - i + 1
            times[i] = lo + (hi - lo) / gap
    for i in range(1, n):                                 # never backwards
        times[i] = max(times[i], times[i - 1])
    out: list[list[Any]] = []
    hold = "a"
    for i, p in enumerate(phones):
        t = float(times[i])
        nxt = float(times[i + 1]) if i + 1 < n else end
        base, stress = p[:2], p[2:3]
        if base in VOWELS:
            shapes = VOWELS[base]
            k = LAX.get(base, 1.0) * STRESS.get(stress, 1.0) * gain
            out.append([t, shapes[0], k])
            if len(shapes) == 2:                          # a glide: a→i in "my"
                out.append([t + 0.55 * max(nxt - t, 0.06), shapes[1], k * 0.75])
            hold = shapes[-1]
        elif p in SHUT:
            out.append([t, hold, 0.0])
        elif p in SHAPED:
            v, k = SHAPED[p]
            out.append([t, v, k])
            hold = v
        elif not out or out[-1][2] != 0.4:
            out.append([t, hold, 0.4])
    out.append([end, hold, 0.0])
    return [[round(t, 3), v, round(min(k, MAX_GAIN), 3)] for t, v, k in out]
