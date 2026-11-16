// Reading a scene and a voice track at a time t. Pure functions (no three.js, no page), so
// tests run them in node (tests/test_stage.py). Every picture the stage draws is a function
// of t and these, so a still at ?t=12.3 and playback agree.

export const clamp01 = (v) => Math.min(1, Math.max(0, v));
export const smooth = (v) => { v = clamp01(v); return v * v * (3 - 2 * v); };
// Cartoon timing: arrive fast and overshoot a little, then settle (poses); hit hard (a take).
export const backOut = (v, s = 1.4) => { v = clamp01(v) - 1; return 1 + (s + 1) * v * v * v + s * v * v; };
export const snap = (v) => 1 - (1 - clamp01(v)) ** 4;

// A cartoon action's offsets at `local` seconds into it: keys are [time, {bone: value, center, snap}]
// extremes (anticipation, the hit, overshoot, settle); between two keys it eases, or snaps
// into a key marked snap. Missing values are 0. Returns {bone: value} with center as a bone.
export function actionPose(keys, local) {
  let i = 0;
  while (i + 2 < keys.length && keys[i + 1][0] <= local) i++;
  const [t0, a] = keys[i], [t1, b] = keys[i + 1];
  const f = clamp01((local - t0) / Math.max(t1 - t0, 1e-6));
  const k = b.snap ? snap(f) : smooth(f);
  const out = {};
  for (const name of new Set([...Object.keys(a), ...Object.keys(b)])) {
    if (name === 'snap') continue;
    const va = a[name], vb = b[name], ref = vb ?? va;
    if (Array.isArray(ref)) {
      out[name] = [0, 1, 2].map((j) => (va?.[j] ?? 0) + ((vb?.[j] ?? 0) - (va?.[j] ?? 0)) * k);
    } else {
      out[name] = {};
      for (const f2 of new Set([...Object.keys(va || {}), ...Object.keys(vb || {})])) {
        out[name][f2] = (va?.[f2] ?? 0) + ((vb?.[f2] ?? 0) - (va?.[f2] ?? 0)) * k;
      }
    }
  }
  return out;
}

// A piecewise-linear envelope [[time, value], …] at x.
export function envelopeAt(points, x) {
  if (!points?.length || x <= points[0][0]) return points?.[0]?.[1] ?? 0;
  for (let i = 1; i < points.length; i++) {
    if (x <= points[i][0]) {
      const [x0, y0] = points[i - 1], [x1, y1] = points[i];
      return y0 + (y1 - y0) * (x - x0) / Math.max(x1 - x0, 1e-6);
    }
  }
  return points.at(-1)[1];
}

// The cue in force at t, the one before it, and how far (0..1) the move between them is.
export function cueAt(list, t) {
  if (!list || !list.length) return [null, null, 1];
  let i = 0;
  while (i + 1 < list.length && list[i + 1].t <= t) i++;
  const cur = list[i], prev = list[Math.max(i - 1, 0)];
  return [prev, cur, i === 0 ? 1 : smooth((t - cur.t) / (cur.ease ?? 0.3))];
}

// A set cue's value at t: eases from the previous value to this cue's.
export function laneAt(list, t, start = 0) {
  const [a, b, k] = cueAt(list, t);
  if (!b) return start;
  return a.v + (b.v - a.v) * k;
}

// The shot at t and how far through it we are (0..1, for push).
export function shotAt(shots, t, duration) {
  const [, s] = cueAt(shots, t);
  if (!s) return [null, 0];
  const next = shots.find((x) => x.t > s.t)?.t ?? duration;
  return [s, clamp01((t - s.t) / Math.max(next - s.t, 0.01))];
}

// Loudness around t: the mean of the last `span` values.
export function envAt(track, t, span = 3) {
  const i = Math.round(t * track.rate);
  let s = 0, n = 0;
  for (let k = i - span + 1; k <= i; k++) if (k >= 0 && k < track.env.length) { s += track.env[k]; n++; }
  return n ? s / n : 0;
}

const REST = [0, 'a', 0];
// The mouth cue at t, the one before it, and the crossfade between them (shutting is quicker).
export function mouthAt(track, t) {
  const v = track.mouth || [];
  let lo = 0, hi = v.length - 1, i = -1;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (v[m][0] <= t) { i = m; lo = m + 1; } else hi = m - 1; }
  if (i < 0) return [REST, REST, 1];
  return [v[i], i > 0 ? v[i - 1] : REST, smooth((t - v[i][0]) / (v[i][2] === 0 ? 0.045 : 0.07))];
}

// 0..1: how far inside one of the [start, end] spans t is, easing in and out.
export function spanAt(spans, t, ease = 0.14) {
  let best = 0;
  for (const [a, b] of spans || []) {
    if (t > a - ease && t < b + ease) best = Math.max(best, smooth(Math.min(t - a + ease, b + ease - t) / (2 * ease)));
  }
  return best;
}

export function rng(seed) { return () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296); }

// When she blinks: every 1.8-5 s, the same every time for the same seed.
export function blinkTimes(duration, seed = 57) {
  const r = rng(seed), out = [];
  for (let t = 1.2; t < duration; t += 1.8 + r() * 3.2) out.push(+t.toFixed(3));
  return out;
}

// How shut her eyes are from a blink at t (0..1; a blink takes 0.17 s).
export function blinkAt(blinks, t) {
  const b = blinks.find((x) => t >= x && t < x + 0.17);
  return b === undefined ? 0 : 1 - Math.abs((t - b) / 0.085 - 1);
}

// Where t in the cut is in the clip (for the room's screen): [[clip_t0, clip_t1, cut_t0]].
export function clipTime(segments, t) {
  for (const [c0, c1, off] of segments || []) if (t < off + (c1 - c0)) return c0 + Math.max(0, t - off);
  const last = (segments || []).at(-1);
  return last ? last[1] : t;
}
