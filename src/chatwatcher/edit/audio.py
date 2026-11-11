"""A clip's audio, reduced to what the editor needs: a waveform to draw, pause points
(quiet dips between words or notes) that cuts snap to, so a cut never lands mid-word, and
hits (sudden jumps in loudness: a scream, a crash, the screech starting) that sound
effects snap to, so a sound lands on the exact moment it reacts to.

Streams almost always have background music, so "pause" is relative: a dip well below
the loudness around it, not digital silence."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from chatwatcher.clip.media import AUDIO_FROM_ZERO, MediaError, _exe

RATE = 50                  # waveform values per second (20 ms each)
_SR = 8000                 # decode rate; plenty for loudness
_SMOOTH = 8                # 160 ms moving average before looking for dips
_MIN_SPAN = 12             # a pause is the quietest point within ±240 ms…
_CONTEXT = 50              # …and at least _MIN_DEPTH dB below the loud level within ±1 s
_MIN_DEPTH = 6.0
_HIT_WINDOW = 4            # a hit is a rise of at least _HIT_RISE dB within 80 ms…
_HIT_RISE = 10.0
_HIT_AFTER = 10            # …up to a level (next 200 ms) at least _HIT_JUMP dB above the
_HIT_BEFORE = 100          # usual loud level of the 2 s before
_HIT_JUMP = 6.0
_VERSION = 3               # bump to recompute cached files (3: audio that starts late lines up)


@dataclass
class Waveform:
    duration_s: float
    peaks: list[int]                      # 0..100 per 1/RATE s, for drawing
    pauses: list[tuple[float, float]]     # (time s, depth dB), time-ordered
    hits: list[tuple[float, float]] = field(default_factory=list)   # (time s, rise dB)
    loudness: float = -20.0               # rough level of the clip's sound (dBFS), for previews

    def to_json(self) -> dict:
        return {"rate": RATE, "duration": self.duration_s, "peaks": self.peaks,
                "pauses": [[round(t, 2), round(d, 1)] for t, d in self.pauses],
                "hits": [[round(t, 2), round(d, 1)] for t, d in self.hits],
                "loudness": round(self.loudness, 1)}


def analyse(src: Path, cache: Path | None = None) -> Waveform:
    """Read src's audio (cached in `cache`, keyed by the file's size and time)."""
    st = src.stat()
    key = f"{_VERSION}:{src.name}:{st.st_size}:{int(st.st_mtime)}"
    if cache and cache.is_file():
        try:
            d = json.loads(cache.read_text())
            if d.get("key") == key:
                return Waveform(d["duration"], d["peaks"], [tuple(p) for p in d["pauses"]],
                                [tuple(h) for h in d["hits"]], d["loudness"])
        except (OSError, ValueError, KeyError):
            pass
    try:
        out = subprocess.run([_exe("ffmpeg"), "-v", "error", "-i", str(src), "-vn", "-ac", "1",
                              "-ar", str(_SR), *AUDIO_FROM_ZERO, "-f", "s16le", "-"], capture_output=True, timeout=300)
    except subprocess.TimeoutExpired as e:
        raise MediaError(f"reading the audio of {src.name} timed out") from e
    if out.returncode != 0:
        raise MediaError(f"couldn't read the audio of {src.name}: {out.stderr.decode(errors='replace')[:300]}")
    wf = from_samples(np.frombuffer(out.stdout, dtype="<i2").astype(np.float32) / 32768.0)
    if cache:
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps({"key": key} | wf.to_json()))
        tmp.replace(cache)
    return wf


def from_samples(x: np.ndarray, sr: int = _SR) -> Waveform:
    hop = sr // RATE
    n = len(x) // hop
    if n == 0:
        return Waveform(len(x) / sr, [], [], [])
    frames = x[: n * hop].reshape(n, hop)
    peak = np.abs(frames).max(axis=1)
    ref = max(float(np.percentile(peak, 99)), 1e-4)
    peaks = np.clip(np.round(peak / ref * 100), 0, 100).astype(int).tolist()

    rms = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1))
    level = 20 * np.log10(np.maximum(rms, 1e-5))                    # dBFS, floor -100
    smooth = np.convolve(level, np.ones(_SMOOTH) / _SMOOTH, mode="same")
    sounding = rms[level > max(-50.0, float(np.percentile(level, 20)))]
    loudness = 10 * np.log10(max(float((sounding ** 2).mean()), 1e-10)) if len(sounding) else -60.0
    return Waveform(round(len(x) / sr, 3), peaks, _pauses(smooth), _hits(level), float(loudness))


def _hits(level: np.ndarray) -> list[tuple[float, float]]:
    """Moments that get suddenly louder than the last couple of seconds were: a scream, a
    crash, the screech starting. (Every word after a pause is a sudden rise too, but no
    louder than talking, so it doesn't count.) Time = the start of the jump."""
    from numpy.lib.stride_tricks import sliding_window_view as win
    light = np.convolve(np.maximum(level, -60.0), np.ones(3) / 3, mode="same")
    n = len(light)
    if n < 2 * _MIN_SPAN + _HIT_WINDOW + 1:
        return []
    rise = np.zeros(n)
    rise[_HIT_WINDOW:] = light[_HIT_WINDOW:] - light[:-_HIT_WINDOW]
    after = win(np.pad(light, (0, _HIT_AFTER - 1), mode="edge"), _HIT_AFTER).max(axis=1)
    before = np.percentile(win(np.pad(light, (_HIT_BEFORE, 0), mode="edge")[:n + _HIT_BEFORE - 5],
                               _HIT_BEFORE - 4), 80, axis=1)[:n]
    jump = after - before                          # how much louder than lately
    sounding = light[light > -55]
    typical = float(np.percentile(sounding, 60)) if len(sounding) else -30.0
    strength = after - typical                     # how loud, next to her usual talking
    local_max = win(np.pad(rise, _MIN_SPAN, mode="edge"), 2 * _MIN_SPAN + 1).max(axis=1)
    idx = np.flatnonzero((rise >= local_max - 1e-9) & (rise >= _HIT_RISE) & (jump >= _HIT_JUMP)
                         & (strength >= -_HIT_JUMP))
    if not len(idx):
        return []
    runs = np.split(idx, np.flatnonzero(np.diff(idx) > _MIN_SPAN) + 1)
    return [(float(max(r[0] - _HIT_WINDOW + 1, 0) / RATE), float(_HIT_JUMP + strength[r].max())) for r in runs]


def _pauses(level: np.ndarray) -> list[tuple[float, float]]:
    from numpy.lib.stride_tricks import sliding_window_view as win
    n = len(level)
    if n < 2 * _MIN_SPAN + 1:
        return []
    local_min = win(np.pad(level, _MIN_SPAN, mode="edge"), 2 * _MIN_SPAN + 1).min(axis=1)
    loud = np.percentile(win(np.pad(level, _CONTEXT, mode="edge"), 2 * _CONTEXT + 1), 80, axis=1)
    depth = loud - level
    idx = np.flatnonzero((level <= local_min + 1e-9) & (depth >= _MIN_DEPTH))
    if not len(idx):
        return []
    # A flat quiet stretch (e.g. real silence) is many minima in a row: count it once, at its middle.
    runs = np.split(idx, np.flatnonzero(np.diff(idx) > _MIN_SPAN) + 1)
    return [(float((r[len(r) // 2] + 0.5) / RATE), float(depth[r].max())) for r in runs]


def snap(t: float, pauses: list[tuple[float, float]], window: float) -> float:
    """The pause (or hit) nearest t, deeper ones pulling harder; t if none is within window."""
    best, best_cost = t, None
    for p, depth in pauses:
        dt = abs(p - t)
        if dt > window:
            continue
        cost = dt / (1 + depth / 12)
        if best_cost is None or cost < best_cost:
            best, best_cost = p, cost
    return best
