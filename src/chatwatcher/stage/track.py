"""The voice track the 3D figure moves to, for one stage's cut (stage.store):

    stage_audio.m4a   the cut's sound (every piece of the cut, joined)
    stage_track.json  times in seconds from the start of the cut:
        env       loudness of the voice band, RATE values a second (0..1)
        words     [[start, end, text]]
        mouth     [[t, vowel, open]]: vowel a i u e o, open 0..1. Each letter of each word
                  is timed by the project's CTC aligner (subs.align); vowels open the mouth,
                  m/b/p shut it, other consonants hold the vowel before them half open
        laughs    [[start, end, loudness]]: voice with no words in it
        mumbles   [[start, end]]: voice with no words inside a subtitle line (talk Whisper
                  missed): the mouth just follows the loudness there
        lines     [{t0, t1, text}]: the subtitles (the edit's, else Whisper's lines)
        segments  [[clip_t0, clip_t1, cut_t0]]: where each piece of the cut is in clip.mp4

Both files sit next to clip.mp4 and are made again when the cut, the clip or words.json
change (`key`)."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import unicodedata
from pathlib import Path
from typing import Any, Callable

from chatwatcher.config import Config
from chatwatcher.edit import clipref as cr
from chatwatcher.stage import mouth as mth

VERSION = 4
TRACK_FILE = "stage_track.json"
AUDIO_FILE = "stage_audio.m4a"
RATE = 60                 # loudness values per second
SR = 16000                # what the aligner and the voice detector hear
OUT_SR = 48000

# English spelling → mouth shapes (a vowel pair first, then one letter).
PAIRS = {"ee": "i", "ea": "i", "ie": "i", "oo": "u", "ew": "u", "ue": "u", "ou": "a", "ow": "o", "oa": "o",
         "ai": "e", "ay": "e", "ey": "e", "oi": "o", "oy": "o", "au": "o", "aw": "o"}
SINGLE = {"a": "a", "e": "e", "i": "i", "y": "i", "o": "o", "u": "a"}
SHUT, ROUND = set("mbp"), set("w")


class TrackError(Exception):
    pass


# ── sound ────────────────────────────────────────────────────────────────────

def _pcm(path: Path, rate: int, channels: int, af: str | None = None) -> Any:
    """The clip's sound on the picture's clock. A downloaded clip's sound often starts a
    second or more after its picture; decoded plainly that gap is dropped and every time
    read from the sound is early by it. aresample's first_pts=0 fills it with silence."""
    import numpy as np
    from chatwatcher.clip.media import _exe
    chain = "aresample=async=1:first_pts=0" + (f",{af}" if af else "")
    cmd = [_exe("ffmpeg"), "-v", "error", "-nostdin", "-i", str(path), "-vn", "-ac", str(channels), "-ar", str(rate),
           "-af", chain]
    try:
        raw = subprocess.run(cmd + ["-f", "s16le", "-"], check=True, capture_output=True, timeout=600).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise TrackError(f"Couldn't read the clip's sound: {getattr(e, 'stderr', b'')[-300:]!r}") from e
    return np.frombuffer(raw, dtype=np.int16)


def envelope(pcm: Any) -> Any:
    """Loudness, RATE values a second, scaled so quiet room tone is 0 and her loudest is ~1."""
    import numpy as np
    hop = SR // RATE
    n = len(pcm) // hop
    if n == 0:
        return np.zeros(0)
    rms = np.sqrt((pcm[: n * hop].reshape(n, hop).astype(np.float64) ** 2).mean(axis=1))
    floor, top = np.percentile(rms, 25), np.percentile(rms, 97)
    return np.clip((rms - floor * 1.6) / max(top - floor * 1.6, 1e-6), 0, 1) ** 0.7


def _slices(arr: Any, rate: int, channels: int, cut: list[list[float]]) -> Any:
    import numpy as np
    parts = []
    for c0, c1, _ in cut:
        a, b = int(round(c0 * rate)) * channels, int(round(c1 * rate)) * channels
        parts.append(arr[max(a, 0): max(b, 0)])
    return np.concatenate(parts) if parts else arr[:0]


# ── words ────────────────────────────────────────────────────────────────────

def shapes(chars: list[tuple[str, float]], end: float) -> list[list[Any]]:
    """One word's letters (letter, when it is heard) → mouth cues [t, vowel, open]."""
    word = "".join(c for c, _ in chars)
    if len(word) > 2 and word.endswith("e") and re.search(r"[aiouy]", word[:-1]):
        chars = chars[:-1]                                   # silent e
    out: list[list[Any]] = []
    hold, i = "a", 0
    while i < len(chars):
        c, t = chars[i]
        pair = c + (chars[i + 1][0] if i + 1 < len(chars) else "")
        if pair in PAIRS:
            hold = PAIRS[pair]
            out.append([t, hold, 1.0])
            i += 2
        elif c in SINGLE and not (c == "y" and i == 0):
            hold = SINGLE[c]
            out.append([t, hold, 1.0])
            i += 1
        elif c in SHUT:
            out.append([t, hold, 0.0])
            i += 1
        elif c in ROUND:
            hold = "u"
            out.append([t, "u", 0.7])
            i += 1
        else:
            if not out or out[-1][2] != 0.45 or out[-1][1] != hold:
                out.append([t, hold, 0.45])
            i += 1
    out.append([end, hold, 0.0])
    return out


def letter_times(audio: Any, words: list[tuple[float, float, str]], models_dir: Path) -> list[list[tuple[str, float]] | None]:
    """When each letter of each word is heard (20 ms steps); words are (start, end, text) in
    the audio's own time. None for a word that couldn't be placed, or every word when the
    aligner's model isn't downloaded (transcribing downloads it)."""
    import numpy as np
    out: list[list[tuple[str, float]] | None] = [None] * len(words)
    try:
        from chatwatcher.subs import align
        path = align.model_path(models_dir)
        if not path.is_file():
            return out
        em = align.emissions(align._session(path), np.asarray(audio, dtype=np.float32))
    except Exception:                                       # noqa: BLE001 — no aligner: Whisper's times
        return out
    tokens, owner, windows = [align.STAR], [None], [(0, len(em))]
    for k, (a, b, text) in enumerate(words):
        ids = align.letters(text)
        keep = [c for c in unicodedata.normalize("NFKD", text.lower().replace("’", "'")) if c in align._INDEX]
        if not ids or len(keep) != len(ids):
            continue
        win = (max(0, int((a - 0.3) / align.FRAME_S)), int(np.ceil((b + 0.3) / align.FRAME_S)))
        for n, c in enumerate(keep):
            tokens.append(ids[n])
            owner.append((k, c))
            windows.append(win)
        tokens.append(align.STAR)
        owner.append(None)
        windows.append((0, len(em)))
    if len(tokens) == 1:
        return out
    path_ = align.viterbi(em, tokens, windows) or align.viterbi(em, tokens)
    if path_ is None:
        return out
    seen: set[int] = set()
    for frame, j in enumerate(path_):
        if j >= 0 and owner[j] is not None and j not in seen:
            seen.add(j)
            k, c = owner[j]
            out[k] = (out[k] or []) + [(c, frame * align.FRAME_S)]
    return out


def audio_start(path: Path) -> float:
    """When the clip's sound starts on the picture's clock (often 1-5 s into a download)."""
    from chatwatcher.clip.media import _exe
    try:
        out = subprocess.run([_exe("ffprobe"), "-v", "error", "-select_streams", "a:0", "-show_entries",
                              "stream=start_time", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=60).stdout.strip().splitlines()
        return max(0.0, float(out[0])) if out and out[0] not in ("", "N/A") else 0.0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0


def word_offset(env: Any, words: list[list[Any]], guesses: list[float]) -> float:
    """Seconds to add to words.json's times to get clip time. Transcribing decodes the sound
    plainly, so its times start where the sound starts (audio_start), not at the picture's
    0; a clip downloaded again after it was transcribed can be off too. Each guess is tried
    ±0.4 s against the loudness (env, on the picture's clock) and the best fit wins."""
    import numpy as np
    if len(env) == 0 or not words:
        return 0.0
    centres = [0.0] + [g for g in guesses if abs(g) > 0.3]
    best, best_score = 0.0, -1e9
    for c in centres:
        for off in np.arange(c - 0.4, c + 0.401, 0.02):
            mask = np.zeros(len(env))
            for t0, t1, *_ in words:
                a, b = int((t0 + off) * RATE), int((t1 + off) * RATE)
                mask[max(a, 0): max(min(b, len(env)), 0)] = 1
            if mask.sum() < RATE:
                continue
            score = float((env * mask).sum() / mask.sum() - (env * (1 - mask)).sum() / max((1 - mask).sum(), 1))
            if score > best_score + 1e-9:
                best, best_score = float(off), score
    return round(best, 2)


def voiced_spans(audio: Any, env: Any, words: list[list[Any]], dur: float) -> list[list[float]]:
    """Voice with no words in it, [start, end, loudness]: the voice detector's speech, or a
    loud stretch of the voice band, away from every word."""
    import numpy as np
    voiced = np.zeros(len(env), dtype=bool)
    try:
        from chatwatcher.subs import align
        for a, b in align.speech(np.asarray(audio, dtype=np.float32)):
            voiced[int(a * RATE): int(b * RATE)] = True
    except Exception:                                       # noqa: BLE001 — no VAD: loudness alone
        pass
    k = RATE // 4
    loud = np.convolve(env, np.ones(k) / k, mode="same") > 0.16 if len(env) else voiced
    said = np.zeros(len(env), dtype=bool)
    for a, b, _ in words:
        said[max(int((a - 0.12) * RATE), 0): int((b + 0.12) * RATE)] = True
    on = (voiced | loud) & ~said
    spans, start = [], None
    for i, v in enumerate(list(on) + [False]):
        if v and start is None:
            start = i
        elif not v and start is not None:
            spans.append([start / RATE, i / RATE])
            start = None
    merged: list[list[float]] = []
    for a, b in spans:
        if merged and a - merged[-1][1] < 0.35:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [[round(a, 2), round(min(b, dur), 2), round(float(env[int(a * RATE): int(b * RATE)].mean()), 2)]
            for a, b in merged if b - a >= 0.4]


def to_cut(t0: float, t1: float, cut: list[list[float]]) -> list[tuple[float, float]]:
    """A clip-time span → the cut-time spans it covers (none when it was cut out)."""
    out = []
    for c0, c1, off in cut:
        a, b = max(t0, c0), min(t1, c1)
        if b > a:
            out.append((off + a - c0, off + b - c0))
    return out


def said(whisper: list[tuple[float, float, str]], lines: list[dict[str, Any]],
         from_subs: bool) -> tuple[list[dict[str, Any]], int]:
    """The words she says, each {a, b, text, bonus}: a-b is where to look for it. Where your
    subtitles say something Whisper didn't hear (or heard wrong: "from above i was wrong
    capture"), a line's words are your subtitle's, looked for anywhere in that line;
    elsewhere they are Whisper's. bonus: how much to play the word up (stage.mouth.tokens).
    Also returns how many lines used the subtitles' words instead of Whisper's."""
    def gap(i: int, ln: dict[str, Any]) -> float:
        mid = (whisper[i][0] + whisper[i][1]) / 2
        return max(ln["t0"] - mid, mid - ln["t1"], 0.0)
    owner = {}                                   # each of Whisper's words goes to the line it is nearest
    for i in range(len(whisper)):
        best = min(range(len(lines)), key=lambda k: gap(i, lines[k]), default=None)
        if best is not None and gap(i, lines[best]) <= 0.3:
            owner[i] = best
    state: dict[str, bool] = {}
    out: list[dict[str, Any]] = []
    rewritten = 0
    for k, ln in enumerate(lines):
        toks = mth.tokens(ln["text"], state)
        inside = [i for i, o in owner.items() if o == k]
        heard = [mth.norm(whisper[i][2]) for i in inside]
        common = sum(1 for w, _ in toks if w in heard) / max(len(toks), 1)
        readable = sum(1 for w, _ in toks if re.search("[a-z]", w)) >= 0.8 * len(toks)
        other_speech = common < 0.2 and len(inside) >= 0.6 * len(toks)     # a translation, not her words
        if toks and from_subs and readable and [w for w, _ in toks] != heard and not other_speech:
            rewritten += 1
            out += [{"a": ln["t0"], "b": ln["t1"], "text": w, "bonus": bonus} for w, bonus in toks]
        else:
            bonus = dict(toks)
            out += [{"a": whisper[i][0], "b": whisper[i][1], "text": whisper[i][2],
                     "bonus": bonus.get(mth.norm(whisper[i][2]), 0.0)} for i in inside]
    used = set(owner)
    out += [{"a": a, "b": b, "text": t, "bonus": 0.1 * (mth.norm(t) in mth.INTERJECTIONS)}
            for i, (a, b, t) in enumerate(whisper) if i not in used]
    out.sort(key=lambda w: w["a"])
    return out, rewritten


def fill_gaps(words: list[dict[str, Any]], timed: list[Any]) -> list[tuple[list[tuple[str, float]], bool]]:
    """Every word's letters with times, and whether the aligner heard them: a word it
    couldn't place (or a run of them) is spread over the gap between its neighbours."""
    out: list[tuple[list[tuple[str, float]], bool]] = []
    i = 0
    while i < len(words):
        if timed[i]:
            out.append((timed[i], True))
            i += 1
            continue
        j = i
        while j < len(words) and not timed[j]:
            j += 1
        lo = max(words[i]["a"], out[-1][0][-1][1] + 0.08) if out else words[i]["a"]
        hi = min(words[j - 1]["b"], timed[j][0][1] - 0.04) if j < len(words) else words[j - 1]["b"]
        if hi <= lo:
            hi = lo + 0.25 * (j - i)
        step = (hi - lo) / (j - i)
        for k in range(i, j):
            letters = list(re.sub(r"[^a-z]", "", words[k]["text"].lower())) or ["a"]
            a = lo + (k - i) * step
            out.append(([(c, a + n * step * 0.85 / len(letters)) for n, c in enumerate(letters)], False))
        i = j
    return out


# ── the track ────────────────────────────────────────────────────────────────

def _cut(st: dict[str, Any], clip: cr.ClipRef) -> list[list[float]]:
    out, off = [], 0.0
    for a, b in st["segments"]:
        c0, c1 = max(0.0, a - clip.zero_s), min(clip.duration_s, b - clip.zero_s)
        if c1 > c0:
            out.append([round(c0, 3), round(c1, 3), round(off, 3)])
            off += c1 - c0
    if not out:
        raise TrackError("The stage's cut is outside the clip: set the stage up again (Re-take the cut).")
    return out


def _subs(conn: sqlite3.Connection, moment_pk: int, shape: str) -> list[dict[str, Any]] | None:
    for kind in ((shape, "long") if shape == "short" else ("long",)):
        row = cr.stored(conn, moment_pk, kind)
        if row is not None and row["subs_json"]:
            try:
                subs = json.loads(row["subs_json"])
            except json.JSONDecodeError:
                continue
            if subs:
                return subs
    return None


def key_of(clip: cr.ClipRef, cut: list[list[float]], subs: Any) -> str:
    wp = clip.folder / "words.json"
    ws = wp.stat() if wp.is_file() else None
    raw = json.dumps([VERSION, str(clip.path), clip.path.stat().st_size, cut, subs,
                      [ws.st_size, ws.st_mtime_ns] if ws else None])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def load(folder: Path) -> dict[str, Any] | None:
    try:
        return json.loads((folder / TRACK_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def current(conn: sqlite3.Connection, moment_pk: int, st: dict[str, Any]) -> tuple[cr.ClipRef, str, dict[str, Any] | None]:
    """(clip, the key the track should have, the track on disk if it has it)."""
    clip = cr.clip_ref(conn, moment_pk)
    key = key_of(clip, _cut(st, clip), _subs(conn, moment_pk, st["shape"]))
    have = load(clip.folder)
    ok = have if have and have.get("key") == key and (clip.folder / AUDIO_FILE).is_file() else None
    return clip, key, ok


def ensure(cfg: Config, conn: sqlite3.Connection, moment_pk: int, st: dict[str, Any],
           say: Callable[[str], None] | None = None) -> dict[str, Any]:
    """The stage's track, made now if the one on disk is for another cut (10-40 s: the
    aligner runs over the cut's audio)."""
    clip, key, have = current(conn, moment_pk, st)
    if have:
        return have
    if not clip.has_audio:
        raise TrackError("This clip has no sound.")
    import numpy as np
    say = say or (lambda _m: None)
    cut = _cut(st, clip)
    dur = round(sum(c1 - c0 for c0, c1, _ in cut), 3)
    say("Reading the clip's sound…")
    plain = _pcm(clip.path, SR, 1)
    voice = _pcm(clip.path, SR, 1, "highpass=f=180,lowpass=f=3200")
    stereo = _pcm(clip.path, OUT_SR, 2)
    audio = _slices(plain, SR, 1, cut).astype(np.float32) / 32768
    env = envelope(_slices(voice, SR, 1, cut))

    warnings: list[str] = []
    data = cr.words(clip) or {}
    flat = [w for line in data.get("lines") or [] for w in line.get("words") or []]
    lead = audio_start(clip.path)
    words_s = float(data.get("duration_s") or 0)
    off = word_offset(envelope(voice), flat, [lead] + ([clip.duration_s - words_s] if words_s else []))
    if abs(off) > 0.3:
        why = (f"its sound starts {lead:.1f}s after the picture and the transcript doesn't count that gap"
               if abs(off - lead) < 0.45 else "it was transcribed from another download of the clip")
        warnings.append(f"The transcript is {off:+.1f}s off this clip ({why}); the 3D stage moves the words to fit.")
    whisper = []
    for t0, t1, text, *_ in flat:
        spans = to_cut(t0 + off, t1 + off, cut)
        if spans:
            whisper.append((spans[0][0], spans[-1][1], str(text)))

    subs = _subs(conn, moment_pk, st["shape"])
    lines = []
    if subs:
        for c in subs:
            for a, b in to_cut(float(c["t0"]) - clip.zero_s, float(c["t1"]) - clip.zero_s, cut):
                lines.append({"t0": round(a, 3), "t1": round(b, 3), "text": str(c.get("text", ""))})
    else:
        for line in data.get("lines") or []:
            ws = line.get("words") or []
            if ws:
                for a, b in to_cut(ws[0][0] + off, ws[-1][1] + off, cut):
                    lines.append({"t0": round(a, 3), "t1": round(b, 3), "text": str(line.get("text", "")).strip()})
    lines.sort(key=lambda x: x["t0"])

    mine, rewritten = said(whisper, lines, bool(subs))
    if rewritten:
        warnings.append(f"Whisper's words didn't match {rewritten} of your subtitle line(s); the mouth follows "
                        "your subtitles there.")
    say(f"Timing {len(mine)} words letter by letter…")
    models_dir = Path(cfg.paths.data_dir) / "models"
    timed = fill_gaps(mine, letter_times(audio, [(w["a"], w["b"], w["text"]) for w in mine], models_dir))
    pron = mth.cmudict(models_dir)
    words, mouth, placed, known = [], [], 0, 0
    for w, (chars, aligned) in zip(mine, timed):
        placed += aligned
        a, b = chars[0][1], max(chars[-1][1] + 0.08, chars[0][1] + 0.06)
        words.append([round(a, 3), round(b, 3), w["text"].strip()])
        phones = pron.get(mth.norm(w["text"]))
        if phones:
            known += 1
            mouth += mth.visemes(phones, chars, a, b)
        else:
            mouth += [[round(t, 3), v, k] for t, v, k in shapes(chars, b)]
    mouth.sort(key=lambda c: c[0])
    if mine and not placed:
        warnings.append("The letter aligner isn't downloaded (🎙 Transcribe downloads it): the mouth follows "
                        "Whisper's word times, which are looser.")

    spans = [s for s in voiced_spans(audio, env, words, dur) if s[2] >= 0.08]

    def subtitled(a: float, b: float) -> float:
        return sum(max(0.0, min(b, x["t1"]) - max(a, x["t0"])) for x in lines) / (b - a)
    mumbles = [s[:2] for s in spans if subs and subtitled(s[0], s[1]) > 0.5]
    laughs = [s for s in spans if not (subs and subtitled(s[0], s[1]) > 0.5)]

    say("Writing the cut's sound…")
    from chatwatcher.clip.media import _exe
    tmp_audio = clip.folder / (AUDIO_FILE + ".tmp.m4a")
    try:
        subprocess.run([_exe("ffmpeg"), "-y", "-v", "error", "-f", "s16le", "-ar", str(OUT_SR), "-ac", "2", "-i", "-",
                        "-c:a", "aac", "-b:a", "160k", str(tmp_audio)],
                       input=_slices(stereo, OUT_SR, 2, cut).tobytes(), check=True, capture_output=True, timeout=600)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        tmp_audio.unlink(missing_ok=True)
        raise TrackError(f"Couldn't write the cut's sound: {getattr(e, 'stderr', b'')[-300:]!r}") from e
    tmp_audio.replace(clip.folder / AUDIO_FILE)
    track = {"key": key, "version": VERSION, "duration": dur, "rate": RATE, "word_offset": off,
             "segments": cut, "env": [round(float(v), 3) for v in env], "words": words, "mouth": mouth,
             "laughs": laughs, "mumbles": mumbles, "lines": lines, "warnings": warnings,
             "placed": placed, "known": known}
    tmp = clip.folder / (TRACK_FILE + ".tmp")
    tmp.write_text(json.dumps(track), encoding="utf-8")
    tmp.replace(clip.folder / TRACK_FILE)
    return track
