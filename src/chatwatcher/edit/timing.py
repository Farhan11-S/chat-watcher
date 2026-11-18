"""Time effects: slow motion, fast forward, a speed ramp (slamming into slow motion, the
tuff edit's move) and a freeze frame (the "stare": the picture holds, pushes in on her face,
maybe in black and white, while the music plays on).

An effect {"type": "time", "t", "d", "style", ...} in clip time:
    slow    [t, t + d) plays at `rate` (0.2-0.9): it lasts d / rate in the edit
    fast    [t, t + d) plays at `rate` (1.25-4)
    ramp    [t, t + d) slows down in steps from RAMP_FROM to `rate`, landing on the moment at t + d
    freeze  the frame at t holds for d seconds (edit time), pushing in to `zoom` on `region`
            ("" = the middle), black and white when `mono`
sound (slow, fast, ramp): tempo (her voice at its own pitch), deep (pitch follows the
speed: the slow-motion "noooo"), mute. A freeze is silent; music and sounds play on.

The render cuts the edit's segments where these start and end (retime) into parts
[start, end, gain, speed, extra]: a part plays (end - start) / speed seconds, so a freeze
is one frame at a tiny speed. render.lengths and render.place read the speed, so sounds,
music and subtitles land where the picture is. Each part is drawn at its own speed (its
zooms, captions and overlays slow down with it) and then retimed (render.retime_filter)."""

from __future__ import annotations

import math
from typing import Any, Callable

TIME = "time"
STYLES = ("slow", "fast", "ramp", "freeze")
SOUNDS = ("tempo", "deep", "mute")
RATES = {"slow": (0.5, 0.2, 0.9), "fast": (2.0, 1.25, 4.0), "ramp": (0.3, 0.2, 0.9)}   # default, lowest, highest
D = {"slow": (1.5, 0.2, 10.0), "fast": (3.0, 0.3, 60.0), "ramp": (1.5, 0.4, 8.0), "freeze": (2.0, 0.3, 8.0)}
D_LIMITS = (1.5, 0.2, 60.0)       # any style (the editor's timeline)
RAMP_FROM = 1.6                   # a ramp starts this fast…
RAMP_STEP_S = 0.12                # …and changes speed every this many clip seconds (at most RAMP_STEPS times)
RAMP_STEPS = 8
ZOOM = (1.25, 1.0, 2.5)           # a freeze's push-in: default, none, most
FREEZE_BELOW = 0.05               # a part slower than this is a held frame


def clean(fx: dict[str, Any], i: int) -> dict[str, Any]:
    """The time-effect fields of effect i. Raises ValueError with a message for the editor."""
    style = fx.get("style") if fx.get("style") in STYLES else "slow"
    lo_d = D[style]
    try:
        d = float(fx.get("d", lo_d[0]))
        rate = float(fx.get("rate", RATES.get(style, (1, 1, 1))[0]))
        zoom = float(fx.get("zoom", ZOOM[0]))
    except (TypeError, ValueError):
        raise ValueError(f"Effect {i} has a bad length, speed or zoom.")
    if not all(math.isfinite(v) for v in (d, rate, zoom)):
        raise ValueError(f"Effect {i} has a bad length, speed or zoom.")
    out: dict[str, Any] = {"style": style, "d": round(min(max(d, lo_d[1]), lo_d[2]), 3)}
    if style == "freeze":
        from chatwatcher.edit import regions
        out |= {"zoom": round(min(max(zoom, ZOOM[1]), ZOOM[2]), 2), "mono": bool(fx.get("mono")),
                "region": regions.normal_name(str(fx.get("region") or "")) or ""}
    else:
        _d, lo, hi = RATES[style]
        out |= {"rate": round(min(max(rate, lo), hi), 3),
                "sound": fx.get("sound") if fx.get("sound") in SOUNDS else "tempo"}
    return out


def speed(part: list[Any]) -> float:
    return float(part[3]) if len(part) > 3 and part[3] else 1.0


def extra(part: list[Any]) -> dict[str, Any]:
    return part[4] if len(part) > 4 and isinstance(part[4], dict) else {}


def _end(f: dict[str, Any]) -> float:
    return float(f["t"]) + (0.0 if f.get("style") == "freeze" else float(f.get("d") or 0))


def retime(segs: list[list[float]], effects: list[dict[str, Any]], fps: float,
           aim: Callable[[str], tuple[float, float] | None] | None = None) -> list[list[Any]]:
    """The segments cut into parts at the time effects (clip time). Parts at normal speed
    stay [start, end, gain]; the others get a speed and an extra {"sound"} or {"freeze":
    {"zoom", "x", "y", "mono"}}. aim(region): where a freeze pushes in to, as shares of
    the output picture (None: the middle). Effects that overlap one before them are skipped."""
    marks = []
    for f in sorted((f for f in effects if f.get("type") == TIME and f.get("style") in STYLES), key=lambda f: float(f["t"])):
        if not marks or float(f["t"]) >= _end(marks[-1]):
            marks.append(f)
    if not marks:
        return segs
    fps = fps or 30.0
    frame = 1 / fps
    out: list[list[Any]] = []
    for seg in segs:
        s, e, g = float(seg[0]), float(seg[1]), seg[2] if len(seg) > 2 else 0

        def plain(a: float, b: float) -> None:
            if b - a >= frame / 2:
                out.append([round(a, 4), round(b, 4), g])

        cur = s
        for f in marks:
            t, d = float(f["t"]), float(f.get("d") or 0)
            if f["style"] == "freeze":
                t = min(t, e - frame)
                if not (s <= t < e) or t < cur - 1e-6:
                    continue
                plain(cur, t)
                x, y = (aim(f.get("region") or "") if aim and f.get("region") else None) or (0.5, 0.5)
                out.append([round(t, 4), round(t + frame, 4), g, frame / max(d, frame),
                            {"freeze": {"zoom": float(f.get("zoom") or 1), "x": x, "y": y, "mono": bool(f.get("mono"))}}])
                cur = t + frame
                continue
            a, b = max(t, s, cur), min(t + d, e)
            if b - a < 2 * frame:
                continue
            plain(cur, a)
            rate = float(f.get("rate") or RATES[f["style"]][0])
            ex = {"sound": f.get("sound") if f.get("sound") in SOUNDS else "tempo"}
            if f["style"] == "ramp":
                n = max(2, min(RAMP_STEPS, int((b - a) / RAMP_STEP_S + 1e-6)))
                edges = [a + (b - a) * k / n for k in range(n + 1)]
                for k in range(n):
                    out.append([round(edges[k], 4), round(edges[k + 1], 4), g,
                                round(RAMP_FROM * (rate / RAMP_FROM) ** (k / (n - 1)), 4), ex])
            else:
                out.append([round(a, 4), round(b, 4), g, rate, ex])
            cur = b
        plain(cur, e)
    return out


def info() -> dict[str, Any]:
    return {"styles": STYLES, "sounds": SOUNDS, "rates": RATES, "d": D, "ramp_from": RAMP_FROM,
            "ramp_step": RAMP_STEP_S, "ramp_steps": RAMP_STEPS, "zoom": ZOOM}
