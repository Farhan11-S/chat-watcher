"""Server-rendered SVG charts (no JS chart library). Colours come from CSS variables
(--series-1 / --series-2, validated light + dark), so dark mode is handled in app.css."""

from __future__ import annotations

import json
import math
from html import escape
from typing import Any

from chatwatcher.scoring.moments import fmt_ts

W, LEFT, RIGHT = 1000, 44, 12


def _x(t: float, duration: float) -> float:
    return LEFT + (t / max(duration, 1.0)) * (W - LEFT - RIGHT)


def _tick_step(duration: float) -> int:
    for step in (60, 120, 300, 600, 900, 1800, 3600, 7200):
        if duration / step <= 9:
            return step
    return 10800


def _x_axis(duration: float, y: float) -> list[str]:
    out = []
    step = _tick_step(duration)
    for t in range(0, int(duration) + 1, step):
        x = _x(t, duration)
        out.append(f'<line class="tick" x1="{x:.1f}" x2="{x:.1f}" y1="{y:.1f}" y2="{y + 4:.1f}"/>')
        out.append(f'<text class="axis-label" x="{x:.1f}" y="{y + 16:.1f}" text-anchor="middle">{fmt_ts(t)}</text>')
    return out


def _downsample(values: list[float], bin_s: float, target: int = 500) -> list[tuple[float, float]]:
    """Max per bucket, so short peaks survive."""
    n = len(values)
    step = max(1, math.ceil(n / target))
    return [((i + step / 2) * bin_s, max(values[i:i + step])) for i in range(0, n, step)]


def score_timeline(scores: dict[str, Any], moments: list[dict[str, Any]], duration: float,
                   threshold: float, height: int = 200) -> dict[str, Any]:
    """Combined moment score over stream time, with each moment shaded and linked."""
    values = scores.get("score") or []
    bin_s = scores.get("bin_s") or 5
    duration = duration or len(values) * bin_s
    top, bottom = 34, 26
    plot_h = height - top - bottom
    ymax = max(max(values, default=0) * 1.1, threshold * 1.3, 0.5)

    def y(v: float) -> float:
        return top + (1 - min(v, ymax) / ymax) * plot_h

    pts = _downsample(values, bin_s)
    line = " ".join(f"{'M' if i == 0 else 'L'}{_x(t, duration):.1f},{y(v):.1f}" for i, (t, v) in enumerate(pts))
    base = top + plot_h
    area = (f"{line} L{_x(pts[-1][0], duration):.1f},{base:.1f} L{_x(pts[0][0], duration):.1f},{base:.1f} Z"
            if pts else "")

    parts = [f'<svg class="chart" viewBox="0 0 {W} {height}" role="img" '
             f'aria-label="Moment score over the stream; shaded bands are the detected moments">']
    # recessive grid: 0, threshold, max
    for v in sorted({0.0, round(ymax / 2, 1), round(ymax * 0.9, 1)}):
        parts.append(f'<line class="grid" x1="{LEFT}" x2="{W - RIGHT}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>')
        parts.append(f'<text class="axis-label" x="{LEFT - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{v:g}</text>')
    # moments behind the line; labels take a second row (or are dropped, keeping the
    # tooltip) when they would collide
    rows_end = [-1e9, -1e9]
    for m in sorted(moments, key=lambda m: m["start_s"]):
        x0, x1 = _x(m["start_s"], duration), _x(m["end_s"], duration)
        w = max(x1 - x0, 3)
        cls = "m-region m-song" if m.get("kind") == "song" else "m-region"
        cls += " m-rejected" if m.get("status") == "rejected" else ""
        label = f"#{m['rank']}" + (" ♪" if m.get("kind") == "song" else "")
        tip = f"#{m['rank']} {fmt_ts(m['start_s'])}–{fmt_ts(m['end_s'])} · score {m['score']:.2f}"
        cx, half = x0 + w / 2, len(label) * 3.6 + 3
        text = ""
        for row, ly in enumerate((top - 6, top - 19)):
            if cx - half > rows_end[row]:
                rows_end[row] = cx + half
                text = f'<text class="m-label" x="{cx:.1f}" y="{ly}" text-anchor="middle">{label}</text>'
                break
        parts.append(
            f'<a href="/moment/{m["id"]}" class="m-link"><title>{escape(tip)}</title>'
            f'<rect class="{cls}" x="{x0:.1f}" y="{top}" width="{w:.1f}" height="{plot_h}" rx="2"/>{text}</a>')
    parts.append(f'<line class="threshold" x1="{LEFT}" x2="{W - RIGHT}" y1="{y(threshold):.1f}" y2="{y(threshold):.1f}"/>')
    if pts:
        parts.append(f'<path class="series-area s1" d="{area}"/>')
        parts.append(f'<path class="series-line s1" d="{line}"/>')
    parts.append(f'<line class="baseline" x1="{LEFT}" x2="{W - RIGHT}" y1="{base:.1f}" y2="{base:.1f}"/>')
    parts += _x_axis(duration, base)
    parts.append('<line class="crosshair" x1="0" x2="0" y1="%d" y2="%.1f" visibility="hidden"/>' % (top, base))
    parts.append("</svg>")
    hover = {"left": LEFT, "right": RIGHT, "width": W, "duration": duration, "label": "score",
             "points": [[round(t, 1), round(v, 2)] for t, v in pts]}
    return {"svg": "".join(parts), "hover": json.dumps(hover, separators=(",", ":"))}


def heatmap_strip(heatmap: list[list[float]], duration: float, height: int = 70) -> dict[str, Any]:
    """YouTube's 'Most replayed' curve as its own small chart on the same time axis."""
    top, bottom = 6, 22
    plot_h = height - top - bottom
    base = top + plot_h

    def y(v: float) -> float:
        return top + (1 - v) * plot_h

    pts = [((s + e) / 2, v) for s, e, v in heatmap]
    line = " ".join(f"{'M' if i == 0 else 'L'}{_x(t, duration):.1f},{y(v):.1f}" for i, (t, v) in enumerate(pts))
    area = f"{line} L{_x(pts[-1][0], duration):.1f},{base} L{_x(pts[0][0], duration):.1f},{base} Z"
    parts = [f'<svg class="chart" viewBox="0 0 {W} {height}" role="img" '
             f'aria-label="YouTube most-replayed curve">',
             f'<path class="series-area s2" d="{area}"/>', f'<path class="series-line s2" d="{line}"/>',
             f'<line class="baseline" x1="{LEFT}" x2="{W - RIGHT}" y1="{base}" y2="{base}"/>']
    parts += _x_axis(duration, base)
    parts.append(f'<line class="crosshair" x1="0" x2="0" y1="{top}" y2="{base}" visibility="hidden"/>')
    parts.append("</svg>")
    hover = {"left": LEFT, "right": RIGHT, "width": W, "duration": duration, "label": "replayed",
             "points": [[round(t, 1), round(v, 2)] for t, v in pts]}
    return {"svg": "".join(parts), "hover": json.dumps(hover, separators=(",", ":"))}
