"""The cut of an AI edit: the pieces Claude kept → segments in play order, with the
hard rules applied (talk starts at once, pauses shrink, the end comes soon after the last
line), and Claude's volume changes on those segments."""

from __future__ import annotations

from chatwatcher.edit import pieces as pc, service
from chatwatcher.edit.autoedit.plan import Plan, parse_range, parts

PAD_IN = 0.08            # kept talk starts this long before its first word…
PAD_OUT = 0.18           # …and ends this long after its last
QUIET_KEEP = 0.5         # a quiet piece Claude kept without saying how much: this long
QUIET_MAX = 1.5          # longest quiet stretch kept by default…
BEAT_MAX = 3.0           # …or when Claude asks for seconds ("p7:2.5"); longer needs "!" (action)
LONG_PAUSE = 1.0         # a pause inside a kept sentence longer than this is shortened…
KEEP_PAUSE = 0.4         # …to this (a breath, not a jump)
AUTO_BEAT = 0.35         # the gap between two kept sentences Claude said nothing about
TALK_REACH = 1.5         # a line's edge may move this far out to let her voice (a laugh) finish
MIN_JUMP = 0.4           # removing less than this would be a jittery micro-cut: keep it
FIRST_SOUND_S = 0.5      # someone talks (or something is loud) by then
TAIL_MAX = 1.5           # the edit ends at most this long after the last kept line
COLD_OPEN_MAX = 4.0
VOLUME_RANGE = (-24.0, 6.0)   # dB Claude may set on a piece (the editor allows -30..+10)
EDGE_SNAP = 0.25         # a volume change this close to a segment's edge moves onto it


# ── the cut ──────────────────────────────────────────────────────────────────

def _clear(t: float, speech: list[list[float]], lo: float, hi: float, out: int = 0) -> float:
    """t moved out of any stretch of voice it falls inside (to the nearer edge, or the
    outer one when out = -1 start / +1 end), staying within [lo, hi]: a cut never
    lands mid-sound."""
    for a, b in speech:
        if a + 0.03 < t < b - 0.03:
            edge = a if out < 0 else b if out > 0 else (a if t - a <= b - t else b)
            if not lo <= edge <= hi:                 # can't step out that way: try the other edge
                edge = b if edge == a else a
            return min(max(edge, lo), hi)
    return t


def _span(p: pc.Piece, mode: str, secs: float | None, prev: pc.Piece | None, nxt: pc.Piece | None,
          speech: list[list[float]] | None = None) -> tuple[float, float]:
    """The part of a piece that's kept, in clip time, with its edges out of any voice."""
    sp = speech or []
    lo_lim, hi_lim = (prev.t0 if prev else 0.0), (nxt.t1 if nxt else p.t1)
    if p.kind == "talk":
        lo = max(p.w0 - PAD_IN, lo_lim)
        hi = min(p.w1 + PAD_OUT, hi_lim)
        return (_clear(lo, sp, max(lo_lim, p.w0 - TALK_REACH), p.w0, -1),
                _clear(hi, sp, p.w1, min(hi_lim, p.w1 + TALK_REACH), +1))
    if mode == "action":
        return p.t0, p.t1
    keep = min(secs, BEAT_MAX) if secs is not None else QUIET_KEEP
    keep = min(keep, QUIET_MAX if secs is None else BEAT_MAX, p.length)
    near = [h for h in p.hits if h[0] - p.t0 <= BEAT_MAX + 1.0]
    if near:                                   # a bang soon after the line: keep around it
        lo = min(max(near[0][0] - keep * 0.35, p.t0), p.t1 - keep)
    elif prev is not None and prev.kind == "talk" and (nxt is None or p.length > 3 * keep):
        lo = p.t0                              # the reaction right after the line
    else:                                      # a breath between two lines: the middle
        lo = p.t0 + (p.length - keep) / 2
    return _clear(lo, sp, p.t0, p.t1), _clear(lo + keep, sp, p.t0, p.t1)


def _merge(spans: list[tuple[float, float]]) -> list[list[float]]:
    out: list[list[float]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1] + MIN_JUMP:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def assemble(ps: list[pc.Piece], keep: dict[int, tuple[str, float | None]], cold: list[int],
             duration: float, max_segments: int = service.MAX_SEGMENTS,
             speech: list[list[float]] | None = None) -> Plan:
    """Kept pieces → segments [start, end, 0] in play order, with the hard rules applied."""
    warnings = []
    idx = sorted(keep)
    # The start: drop quiet (non-action) pieces before the first line or hit.
    while idx and ps[idx[0]].kind == "quiet" and keep[idx[0]][0] != "action" and not ps[idx[0]].hits:
        idx.pop(0)
    # The end: drop quiet pieces after the last line, unless they're action.
    while idx and ps[idx[-1]].kind == "quiet" and keep[idx[-1]][0] != "action":
        last = ps[idx[-1]]
        if last.hits and len(idx) > 1:             # a hit right after the punchline is the reaction
            break
        idx.pop()
    if not idx:
        talk = [i for i, p in enumerate(ps) if p.kind == "talk"] or [0]
        idx = talk
        warnings.append("Claude kept nothing usable, so every line was kept.")
    # Two kept sentences with only a quiet piece between them: keep a breath of it, so
    # the lines don't run into each other.
    kept = set(idx)
    for i in list(idx):
        j = i + 2
        if j in kept and ps[i].kind == "talk" and ps[j].kind == "talk" and ps[i + 1].kind == "quiet" \
                and i + 1 not in kept:
            keep = keep | {i + 1: ("beat", min(AUTO_BEAT, ps[i + 1].length))}
            kept.add(i + 1)
    idx = sorted(kept)
    spans = []
    for i in idx:
        mode, secs = keep.get(i, ("all", None))
        lo, hi = _span(ps[i], mode, secs, ps[i - 1] if i else None, ps[i + 1] if i + 1 < len(ps) else None, speech)
        # Long pauses inside a sentence shrink to a breath (never cutting into her voice).
        for a, b in ps[i].pauses if ps[i].kind == "talk" else []:
            if b - a > LONG_PAUSE and mode != "action":
                cut0 = _clear(a + KEEP_PAUSE / 2, speech or [], a, b)
                cut1 = _clear(b - KEEP_PAUSE / 2, speech or [], a, b)
                if cut1 - cut0 >= MIN_JUMP:
                    spans.append((lo, cut0))
                    lo = cut1
        spans.append((lo, hi))
    story = _merge(spans)
    # The tail: at most TAIL_MAX after the last word, unless the last piece is action.
    last_talk = max((ps[i].w1 for i in idx if ps[i].kind == "talk"), default=None)
    end_mode, end_secs = keep.get(idx[-1], ("all", None))
    if last_talk is not None and end_mode != "action":
        tail = max(TAIL_MAX, min(end_secs or 0.0, BEAT_MAX) + PAD_OUT)
        story[-1][1] = min(story[-1][1], max(last_talk + tail, story[-1][0] + 0.5))
    # Too many segments for the renderer: close the smallest gaps.
    while len(story) > max_segments - (1 if cold else 0):
        k = min(range(len(story) - 1), key=lambda j: story[j + 1][0] - story[j][1])
        story[k][1] = story[k + 1][1]
        del story[k + 1]
    segs = [[round(s, 3), round(min(e, duration), 3), 0.0] for s, e in story if e - s >= service.MIN_SEGMENT_S]
    opened = False
    if cold:
        talk = [ps[i] for i in cold if ps[i].kind == "talk"] or [ps[i] for i in cold]
        s = max(talk[0].w0 - PAD_IN if talk[0].kind == "talk" else talk[0].t0, 0.0)
        e = min((talk[-1].w1 + PAD_OUT) if talk[-1].kind == "talk" else talk[-1].t1, s + COLD_OPEN_MAX, duration)
        starts_same = segs and abs(segs[0][0] - s) < 0.3
        if e - s >= 0.5 and not starts_same:
            segs.insert(0, [round(s, 3), round(e, 3), 0.0])
            opened = True
    return Plan(segs, round(duration - sum(e - s for s, e in story), 1), len(idx), len(ps), opened, warnings)


def apply_volume(items: list[str], ps: list[pc.Piece], segs: list[list[float]]) -> tuple[list[list[float]], int]:
    """Claude's volume changes on the cut: each segment is split where a changed piece
    starts or ends (the parts still play back to back) and the part gets that gain.
    Returns (segments, how many changes landed)."""
    changes: list[tuple[float, float, float]] = []
    for item in items:
        bits = parts(item)
        rng = parse_range(bits[0], len(ps)) if bits else []
        try:
            gain = float(bits[1].lower().replace("db", "")) if len(bits) > 1 else 0.0
        except ValueError:
            continue
        if not rng or abs(gain) < 1:
            continue
        changes.append((ps[rng[0]].t0, ps[rng[-1]].t1,
                        round(min(max(gain, VOLUME_RANGE[0]), VOLUME_RANGE[1]), 1)))
    if not changes:
        return segs, 0
    out, landed = [], set()
    for s, e, g in segs:
        cuts = {s, e}
        for a, b, _gain in changes:
            for t in (a, b):
                if s + EDGE_SNAP < t < e - EDGE_SNAP:
                    cuts.add(t)
        edges = sorted(cuts)
        for a, b in zip(edges, edges[1:]):
            gain = g
            mid = (a + b) / 2
            for k, (c0, c1, cg) in enumerate(changes):
                if c0 <= mid < c1:                      # the later change wins where two overlap
                    gain = cg
                    landed.add(k)
            if out and out[-1][1] == a and out[-1][2] == gain:
                out[-1][1] = round(b, 3)            # same level as the part before: one segment
            else:
                out.append([round(a, 3), round(b, 3), gain])
    # The renderer's limit: undo the smallest changes first (keep the ones that matter).
    while len(out) > service.MAX_SEGMENTS:
        k = min(range(len(out) - 1), key=lambda j: (out[j + 1][0] != out[j][1], abs(out[j][2] - out[j + 1][2])))
        if out[k + 1][0] != out[k][1]:
            break
        out[k][1] = out[k + 1][1]
        del out[k + 1]
    return out, len(landed)
