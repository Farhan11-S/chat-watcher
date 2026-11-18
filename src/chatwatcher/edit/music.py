"""Your music library: whole tracks under data/music/, details in the `music` table, and
where a track goes in an edit (the music bed).

On import a track is levelled (its loud part about as loud as normal talking, like the
sound library) and listened to: its tempo, every beat, and its drop (where it hits
hardest) are found and stored, so an edit can land the drop on the punchline and cut on
the beat. You can move the drop on the Sounds page when the guess is wrong.

A music bed is one effect in an edit, {"type": "music", "t": where the drop lands (clip
time, like every effect), "track", "lead": seconds of music before the drop (-1: from the
start of the edit), "gain", "duck"}. bed() turns it into where it plays in the rendered
edit; the editor's preview does the same sums (static/editor/audio.js).
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from chatwatcher.clip.media import MediaError, _exe
from chatwatcher.config import Config
from chatwatcher.edit import timing as tm

SR = 44100                  # stored
AN_SR = 22050               # listened to
HOP = 512                   # analysis frame step (23 ms)
LEVEL_DBFS = -14.0          # the track's loud part, after levelling (= edit.sounds.LEVEL_DBFS)
PEAK_DBFS = -1.0
MAX_SECONDS = 600.0
MIN_SECONDS = 5.0
MAX_UPLOAD_BYTES = 80 * 1024 * 1024
BPM_RANGE = (70.0, 190.0)
MUSIC = "music"
HUSH = "hush"               # a moment of silence (and black picture): the beat before a drop, a stutter
HUSH_STYLES = ("silence", "black")
HUSH_D = (0.6, 0.1, 3.0)    # default, shortest, longest (seconds)
GAIN_RANGE = (-30.0, 6.0)   # the bed's volume, 0 = as loud as her talking
DUCK_RANGE = (-30.0, 0.0)   # how much lower under her words
DEFAULT_GAIN = -6.0
DEFAULT_DUCK = -10.0
LEAD_MAX = 120.0
DUCK_RAMP = 0.12            # the bed dips this fast under her words…
DUCK_HOLD = 0.25            # …and stays down through gaps shorter than this
FADE_OUT = 1.5              # at the end of the edit
FADE_IN = 0.04              # the bed comes in hard (a soft one would blur the drop)
MOODS = {                   # what a track is for; Claude reads these with the description
    "eurobeat": "racing, drifting, speed, a car chase",
    "phonk": "tuff edits: a flex, a clutch play, a cold comeback, an aura moment",
    "hype": "a win, a cheer, an epic moment",
    "hopecore": "warm and earnest: a sweet or wholesome moment, an accidental life lesson",
    "dreamcore": "floaty, nostalgic, eerie-soft: a surreal or liminal moment",
    "sad": "sad violin / piano: a fake-dramatic loss, a tragic fail",
    "funny": "goofy, silly: a dumb mistake, chaos",
    "tension": "suspense: a build-up, horror, a close call",
    "chill": "lo-fi, calm background",
}
_ID = re.compile(r"^[a-z0-9_]{1,32}$")


class MusicError(Exception):
    pass


def music_dir(cfg: Config) -> Path:
    return cfg.paths.data_dir / "music"


def list_music(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [_row(r) for r in conn.execute("SELECT * FROM music ORDER BY name COLLATE NOCASE")]


def get_track(conn: sqlite3.Connection, track_id: str) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM music WHERE id = ?", (track_id,)).fetchone()
    return _row(r) if r else None


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    try:
        d["beats"] = json.loads(d.pop("beats_json") or "[]")
    except json.JSONDecodeError:
        d["beats"] = []
    return d


def track_path(cfg: Config, track: dict[str, Any]) -> Path:
    return music_dir(cfg) / track["file"]


def valid_id(track_id: str) -> bool:
    return bool(_ID.match(track_id or ""))


def make_id(conn: sqlite3.Connection, name: str) -> str:
    import unicodedata
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9]+", "_", text).strip("_")[:28] or "track"
    tid, n = base, 2
    while conn.execute("SELECT 1 FROM music WHERE id = ?", (tid,)).fetchone():
        tid, n = f"{base}_{n}", n + 1
    return tid


# ── listening: tempo, beats, drop ───────────────────────────────────────────

@dataclass
class Listened:
    bpm: float
    beats: list[float]          # seconds
    drop_s: float               # where it hits hardest (on a beat)


def _frames(x: np.ndarray, n: int = 2048) -> np.ndarray:
    """|STFT| of mono x, HOP apart, Hann window, frame f centred on sample f * HOP:
    (frames, bins)."""
    x = np.pad(x, (n // 2, n // 2))
    if len(x) < n:
        x = np.pad(x, (0, n - len(x)))
    count = 1 + (len(x) - n) // HOP
    idx = np.arange(n)[None, :] + HOP * np.arange(count)[:, None]
    return np.abs(np.fft.rfft(x[idx] * np.hanning(n)[None, :], axis=1)).astype(np.float32)


def onset_strength(spec: np.ndarray) -> np.ndarray:
    """How much new sound starts in each frame (log spectral flux, local mean removed)."""
    logs = np.log1p(100.0 * spec)
    flux = np.maximum(np.diff(logs, axis=0, prepend=logs[:1]), 0.0).sum(axis=1)
    win = max(1, int(0.5 * AN_SR / HOP))
    local = np.convolve(flux, np.ones(win) / win, mode="same")
    env = np.maximum(flux - local, 0.0)
    return env / (env.max() or 1.0)


def tempo(env: np.ndarray) -> float:
    """Beats per minute: the autocorrelation peak, weighted towards 120 (half/double-time
    guesses lose to the tempo people tap)."""
    fps = AN_SR / HOP
    env = env - env.mean()
    n = len(env)
    ac = np.fft.irfft(np.abs(np.fft.rfft(env, 2 * n)) ** 2)[:n]
    lo, hi = int(fps * 60 / BPM_RANGE[1]), int(math.ceil(fps * 60 / BPM_RANGE[0]))
    if hi >= n or lo < 1:
        return 120.0
    lags = np.arange(lo, hi + 1)
    bpm = 60 * fps / lags
    weight = np.exp(-0.5 * (np.log2(bpm / 120.0) / 0.9) ** 2)
    score = ac[lags] * weight
    k = int(np.argmax(score))
    # parabolic refinement between lags
    if 0 < k < len(score) - 1:
        a, b, c = score[k - 1], score[k], score[k + 1]
        den = a - 2 * b + c
        off = 0.5 * (a - c) / den if den else 0.0
    else:
        off = 0.0
    return float(60 * fps / (lags[k] + off))


def beat_track(env: np.ndarray, bpm: float, tightness: float = 100.0) -> list[float]:
    """Beat times (seconds) by dynamic programming (Ellis 2007): beats sit on onsets and
    stay about one period apart."""
    fps = AN_SR / HOP
    period = 60 * fps / bpm
    n = len(env)
    score = env.astype(np.float64).copy()
    back = np.full(n, -1, dtype=np.int64)
    lo, hi = int(round(period / 2)), int(round(2 * period))
    offs = np.arange(lo, hi + 1)
    penalty = -tightness * np.log(offs / period) ** 2
    for t in range(lo, n):
        prev = t - offs
        ok = prev >= 0
        if not ok.any():
            continue
        cand = score[prev[ok]] + penalty[ok]
        j = int(np.argmax(cand))
        score[t] = env[t] + cand[j]
        back[t] = prev[ok][j]
    # the last beat: the best score in the last period
    tail = max(0, n - int(period) - 1)
    t = tail + int(np.argmax(score[tail:])) if n else 0
    out = []
    while t >= 0:
        out.append(t)
        t = int(back[t])
    return [round(f * HOP / AN_SR, 3) for f in reversed(out)]


def find_drop(spec: np.ndarray, beats: list[float]) -> float:
    """The beat where the track gets much louder and fuller, the bass most of all: the
    biggest rise from the 4 s before to the 4 s after."""
    fps = AN_SR / HOP
    if not beats:
        return 0.0
    hz = np.fft.rfftfreq(2048, 1 / AN_SR)
    bass = spec[:, hz < 150].sum(axis=1)
    full = spec.sum(axis=1)
    e = 10 * np.log10(1e-9 + full ** 2) + 10 * np.log10(1e-9 + bass ** 2)
    w = int(4 * fps)
    c = np.concatenate([[0.0], np.cumsum(e)])
    best, best_t = -np.inf, beats[0]
    for b in beats:
        f = int(b * fps)
        if f - w < 0 or f + w > len(e):
            continue
        rise = (c[f + w] - c[f]) / w - (c[f] - c[f - w]) / w
        if rise > best:
            best, best_t = rise, b
    if best == -np.inf:          # a short track: the loudest beat
        best_t = max(beats, key=lambda b: e[min(int(b * fps), len(e) - 1)])
    return float(best_t)


def listen(mono: np.ndarray) -> Listened:
    """Tempo, beats and drop of a mono track at AN_SR."""
    spec = _frames(mono)
    env = onset_strength(spec)
    bpm = tempo(env)
    beats = beat_track(env, bpm)
    return Listened(round(bpm, 1), beats, round(find_drop(spec, beats), 3))


# ── import ──────────────────────────────────────────────────────────────────

def _decode(src: Path, sr: int, channels: int) -> np.ndarray:
    try:
        out = subprocess.run([_exe("ffmpeg"), "-v", "error", "-i", str(src), "-vn", "-ac", str(channels),
                              "-ar", str(sr), "-t", str(MAX_SECONDS + 1), "-f", "f32le", "-"],
                             capture_output=True, timeout=300)
    except subprocess.TimeoutExpired as e:
        raise MusicError("reading the file took too long") from e
    if out.returncode != 0 or not out.stdout:
        raise MusicError("this isn't an audio or video file ffmpeg can read")
    return np.frombuffer(out.stdout, dtype="<f4").reshape(-1, channels)


def loud_level(x: np.ndarray, sr: int) -> float:
    """dBFS of the track's loud part: the mean power of its loudest 30% of seconds (a
    quiet intro or outro doesn't make the rest too loud)."""
    p = (x.astype(np.float64) ** 2).mean(axis=1)
    n = len(p) // sr
    if n < 1:
        return 10 * math.log10(max(float(p.mean()), 1e-12))
    secs = np.sort(p[: n * sr].reshape(n, sr).mean(axis=1))[::-1]
    top = secs[: max(1, int(math.ceil(n * 0.3)))]
    return 10 * math.log10(max(float(top.mean()), 1e-12))


def import_track(cfg: Config, conn: sqlite3.Connection, src: Path, *, original: str, name: str = "",
                 mood: str = "", description: str = "", source: str = "") -> dict[str, Any]:
    """Level `src` into data/music/<id>.flac, listen to it, and add it to the library."""
    name = (name or Path(original).stem).strip()[:80] or "track"
    x = _decode(src, SR, 2)
    if len(x) / SR < MIN_SECONDS:
        raise MusicError(f"a track must be at least {MIN_SECONDS:.0f} s long (sound effects go in the sound library)")
    if len(x) / SR > MAX_SECONDS:
        raise MusicError(f"tracks must be under {MAX_SECONDS / 60:.0f} minutes")
    if float(np.abs(x).max()) < 10 ** (-80 / 20):
        raise MusicError("the file is silent")
    peak = 20 * math.log10(max(float(np.abs(x).max()), 1e-9))
    gain = min(LEVEL_DBFS - loud_level(x, SR), PEAK_DBFS - peak)
    x = (x * 10 ** (gain / 20)).astype(np.float32)
    heard = listen(_decode(src, AN_SR, 1)[:, 0])
    tid = make_id(conn, name)
    folder = music_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    dest, tmp = folder / f"{tid}.flac", folder / f".{tid}.tmp.flac"
    try:
        out = subprocess.run([_exe("ffmpeg"), "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "2",
                              "-i", "-", "-c:a", "flac", "-sample_fmt", "s16", str(tmp)],
                             input=x.tobytes(), capture_output=True, timeout=300)
    except (subprocess.TimeoutExpired, MediaError) as e:
        tmp.unlink(missing_ok=True)
        raise MusicError(str(e)) from e
    if out.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise MusicError(f"couldn't save the track: {out.stderr.decode(errors='replace')[:200]}")
    tmp.replace(dest)
    conn.execute(
        """INSERT INTO music (id, name, mood, description, source, file, duration_s, bpm, beats_json, drop_s,
                              found_drop_s, original)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (tid, name, clean_mood(mood), description.strip()[:500], source.strip()[:300], dest.name,
         round(len(x) / SR, 3), heard.bpm, json.dumps(heard.beats), heard.drop_s, heard.drop_s, original[:200]))
    return get_track(conn, tid) or {}


def clean_mood(mood: str) -> str:
    m = (mood or "").strip().lower()
    return m if m in MOODS else ""


def update_track(conn: sqlite3.Connection, track_id: str, *, name: str, mood: str, description: str,
                 source: str, gain_db: float, drop_s: float | None = None) -> None:
    t = get_track(conn, track_id)
    if not t:
        raise MusicError("no such track")
    drop = t["drop_s"] if drop_s is None or not math.isfinite(drop_s) else min(max(drop_s, 0.0), t["duration_s"])
    conn.execute("UPDATE music SET name = ?, mood = ?, description = ?, source = ?, gain_db = ?, drop_s = ? "
                 "WHERE id = ?",
                 (name.strip()[:80] or track_id, clean_mood(mood), description.strip()[:500], source.strip()[:300],
                  round(min(max(gain_db, -30.0), 10.0), 1), round(drop, 3), track_id))


def delete_track(cfg: Config, conn: sqlite3.Connection, track_id: str) -> None:
    """Edits that used it keep their marker; rendering skips a track that's gone."""
    t = get_track(conn, track_id)
    if not t:
        return
    p = track_path(cfg, t)
    if p.parent == music_dir(cfg) and p.is_file():
        p.unlink()
    conn.execute("DELETE FROM music WHERE id = ?", (track_id,))


def nearest_beat(beats: list[float], t: float) -> float:
    if not beats:
        return t
    i = int(np.searchsorted(beats, t))
    near = [b for b in beats[max(0, i - 1): i + 1]]
    return min(near, key=lambda b: abs(b - t))


# ── the bed in an edit ──────────────────────────────────────────────────────

@dataclass
class Bed:
    """Where the music plays in the rendered edit (its own timeline, before any intro)."""
    path: Path
    at: float                   # seconds into the edit it starts
    offset: float               # seconds into the track it starts from
    length: float
    gain_db: float              # 0 = as loud as her talking
    duck_db: float              # lower under her words
    fade_in: float = FADE_IN
    fade_out: float = 0.0
    beats: list[float] = field(default_factory=list)   # edit time
    drop_at: float | None = None                       # edit time


def place_times(t: float, segs: list[list[float]], fps: float) -> list[float]:
    from chatwatcher.edit.render import place
    return place(t, segs, fps)


def bed(fx: dict[str, Any], track: dict[str, Any], path: Path, segs: list[list[float]], fps: float) -> Bed | None:
    """The music effect fx (clip time) on the cut segs → where it plays, or None when its
    drop is outside the cut (nothing to line it up with)."""
    from chatwatcher.edit.render import lengths
    total = sum(lengths(segs, fps))
    at_drop = place_times(fx["t"], segs, fps)
    if not at_drop:
        return None
    drop_edit = at_drop[0]
    track_drop = float(track.get("drop_s") or 0.0)
    lead = float(fx.get("lead", -1))
    start = max(0.0, drop_edit - track_drop) if lead < 0 else max(0.0, drop_edit - lead, drop_edit - track_drop)
    offset = track_drop - (drop_edit - start)
    length = min(total - start, float(track["duration_s"]) - offset)
    if length < 0.2:
        return None
    runs_out = start + length < total - 0.05             # the track ends before the edit does
    beats = [round(start + b - offset, 3) for b in track.get("beats") or [] if offset <= b < offset + length]
    return Bed(path, round(start, 4), round(offset, 4), round(length, 4),
               float(fx.get("gain", DEFAULT_GAIN)), float(fx.get("duck", DEFAULT_DUCK)),
               fade_out=0.0 if runs_out else min(FADE_OUT, length / 4), beats=beats, drop_at=round(drop_edit, 4))


def speech_on_edit(speech: list[list[float]], segs: list[list[float]], fps: float) -> list[tuple[float, float]]:
    """Her talking (clip-time spans) as edit-time spans, merged across short gaps."""
    from chatwatcher.edit.render import lengths
    out: list[tuple[float, float]] = []
    offset = 0.0
    for seg, length in zip(segs, lengths(segs, fps)):
        s, k = seg[0], tm.speed(seg)               # a retimed part (edit.timing) plays at its speed
        for a, b in speech:
            lo, hi = max(a, s), min(b, s + length * k)
            if hi > lo:
                out.append((offset + (lo - s) / k, offset + (hi - s) / k))
        offset += length
    out.sort()
    merged: list[tuple[float, float]] = []
    for a, b in out:
        if merged and a - merged[-1][1] < DUCK_HOLD:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def hush_windows(effects: list[dict[str, Any]], segs: list[list[float]], fps: float) -> list[tuple[float, float]]:
    """Every hush (clip time) as edit-time windows where all sound stops."""
    out = []
    for fx in effects:
        if fx.get("type") == HUSH:
            out += [(a, a + float(fx.get("d", HUSH_D[0]))) for a in place_times(fx["t"], segs, fps)]
    return sorted(out)


def volume_expr(spans: list[tuple[float, float]], depth_db: float, ramp: float) -> str:
    """An ffmpeg volume expression (of t) that is 1, and 10^(depth/20) inside the spans,
    with ramps of `ramp` seconds round each. Spans closer than two ramps are joined, so
    no two ramps overlap and the terms can simply be added."""
    if not spans or depth_db >= 0:
        return "1"
    merged: list[tuple[float, float]] = []
    for a, b in sorted(spans):
        if merged and a - merged[-1][1] < 2 * ramp + 0.002:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    g = 1 - 10 ** (depth_db / 20)
    terms = [f"clip((t-{a - ramp:.3f})/{ramp},0,1)*clip(({b + ramp:.3f}-t)/{ramp},0,1)" for a, b in merged]
    return f"(1-{g:.4f}*({'+'.join(terms)}))"


def info() -> dict[str, Any]:
    """For the editor."""
    return {"moods": MOODS, "gain": GAIN_RANGE, "duck": DUCK_RANGE, "default_gain": DEFAULT_GAIN,
            "default_duck": DEFAULT_DUCK, "fade_in": FADE_IN, "fade_out": FADE_OUT, "duck_ramp": DUCK_RAMP,
            "level": LEVEL_DBFS, "hush": {"styles": HUSH_STYLES, "d": HUSH_D}}
