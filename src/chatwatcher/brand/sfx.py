"""The intro's sound effects, synthesised (no files needed): whoosh, glitch, clap, boom,
chime and riser. Mono float arrays at SR, roughly -1..1."""

from __future__ import annotations

import math
import random

import numpy as np

SR = 48000


def _one_pole(noise: np.ndarray, cut: np.ndarray) -> np.ndarray:
    """lp += cut * (x - lp), sample by sample (cut may change over time)."""
    out = np.empty_like(noise)
    lp = 0.0
    for i in range(len(noise)):
        lp += cut[i] * (noise[i] - lp)
        out[i] = lp
    return out


def whoosh(rng: random.Random, dur: float = 0.35, up: bool = True) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / n
    env = np.sin(np.pi * t) ** 2
    cut = 0.02 + 0.35 * (t if up else 1 - t)
    noise = np.random.default_rng(rng.randrange(1 << 30)).uniform(-1, 1, n)
    lp = _one_pole(noise, cut)
    hp = np.diff(lp, prepend=0.0)
    return (lp * 0.7 + hp * 0.6) * env


def glitch(rng: random.Random, dur: float = 0.28) -> np.ndarray:
    n = int(dur * SR)
    out = np.zeros(n)
    i = 0
    while i < n:
        hold = rng.randint(30, 400)
        out[i:i + hold] = rng.choice([-1, 1]) * rng.uniform(0.3, 0.9) * 0.5
        i += hold
    t = np.arange(n) / SR
    freqs = np.array([rng.choice([180, 360, 720, 1440]) for _ in range(n // 1200 + 1)])
    tone = np.sin(2 * np.pi * freqs[np.arange(n) // 1200] * t) * ((np.arange(n) % 2400) < 1200)
    return (out + tone * 0.3) * (1 - np.arange(n) / n) ** 0.5


def clap(rng: random.Random) -> np.ndarray:
    n = int(0.18 * SR)
    t = np.arange(n) / SR
    noise = np.random.default_rng(rng.randrange(1 << 30)).uniform(-1, 1, n)
    lp = _one_pole(noise, np.full(n, 0.5))
    body = np.sin(2 * np.pi * 210 * t) * np.exp(-t * 40)
    return lp * np.exp(-t * 55) * 1.1 + body * 0.8


def boom(dur: float = 0.9) -> np.ndarray:
    t = np.arange(int(dur * SR)) / SR
    f = 40 + 90 * np.exp(-t * 18)
    phase = 2 * np.pi * np.cumsum(f) / SR
    return np.sin(phase) * np.exp(-t * 4.5)


def chime(dur: float = 1.4) -> np.ndarray:
    n = int(dur * SR)
    out = np.zeros(n)
    for f, off in ((1318.5, 0.0), (1975.5, 0.07), (2637.0, 0.14), (3951.0, 0.21)):
        o = int(off * SR)
        t = np.arange(n - o) / SR
        out[o:] += 0.22 * np.sin(2 * np.pi * f * t) * np.exp(-t * 3.2)
    return out


def riser(rng: random.Random, dur: float) -> np.ndarray:
    n = max(1, int(dur * SR))
    t = np.arange(n) / n
    noise = np.random.default_rng(rng.randrange(1 << 30)).uniform(-1, 1, n)
    return _one_pole(noise, 0.02 + 0.4 * t * t) * t ** 2


def add(buf: np.ndarray, at_s: float, samples: np.ndarray, gain: float = 1.0) -> None:
    """Mix mono or stereo samples into a stereo buffer (n, 2) at at_s."""
    i0 = int(round(at_s * SR))
    if samples.ndim == 1:
        samples = np.repeat(samples[:, None], 2, axis=1)
    a, b = max(0, i0), min(len(buf), i0 + len(samples))
    if b > a:
        buf[a:b] += samples[a - i0:b - i0] * gain


def db(gain_db: float) -> float:
    return math.pow(10, gain_db / 20)
