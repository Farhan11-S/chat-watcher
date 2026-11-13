// The timeline canvas: time ↔ x, drawing the lanes (beats, cut, sounds, camera, effects,
// captions, ruler, playhead) and finding what is under the pointer.
import {
  $, EDGE_PX, FX_PX, HEIGHT, ROW, S, STRENGTH, Y_CAM, Y_FX, Y_RULER, Y_SUBS, Y_VFX, ZOOMS, call,
  canvas, ctx, laneOf, scroller, spacer, video,
} from "./state.js";
import {
  beatColor, curFx, curSub, cutInto, fmt, isVis, kept, keptFx, nice, overlayOf, sound, total,
  transOf,
} from "./util.js";
import { FULL } from "./preview.js";
import { cueColor } from "./captions.js";

// ── geometry: canvas covers the visible part; the spacer gives the scroll width ──
const fullWidth = () => Math.max(scroller.clientWidth, 100) * S.zoom;
export const X = t => t / S.D.duration * fullWidth();                  // time → x in the full timeline
export const T = x => Math.min(S.D.duration, Math.max(0, x / fullWidth() * S.D.duration));
export const gx = ev => ev.clientX - canvas.getBoundingClientRect().left + scroller.scrollLeft;
export const gy = ev => ev.clientY - canvas.getBoundingClientRect().top;

export function resize() {
  if (!S.D) return;
  const w = scroller.clientWidth, dpr = window.devicePixelRatio || 1;
  spacer.style.width = fullWidth() + "px";
  spacer.style.height = HEIGHT + "px";
  canvas.style.width = w + "px";
  canvas.style.height = HEIGHT + "px";
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(HEIGHT * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  S.needsDraw = true;
  call.paintRegions();
}

export function draw() {
  if (!S.D) return;
  const w = scroller.clientWidth, off = scroller.scrollLeft, W = fullWidth(), dur = S.D.duration;
  const x = t => t / dur * W - off;
  const t0 = off / W * dur, t1 = (off + w) / W * dur;
  ctx.clearRect(0, 0, w, HEIGHT);
  ctx.fillStyle = S.C.bg;
  ctx.fillRect(0, ROW.beats, w, ROW.wave);
  ctx.font = "11px system-ui, sans-serif";
  ctx.textBaseline = "alphabetic";

  // Claude's storyboard beats along the top
  for (const b of S.D.beats) {
    if (b.end < t0 || b.start > t1) continue;
    const a = x(b.start), z = x(b.end);
    ctx.globalAlpha = 0.3;
    ctx.fillStyle = beatColor(b.beat);
    ctx.fillRect(a, 2, Math.max(1, z - a - 1), ROW.beats - 4);
    ctx.globalAlpha = 1;
    if (z - a > 34) { ctx.fillStyle = S.C.text2; ctx.fillText(b.beat, a + 4, ROW.beats - 6, z - a - 8); }
  }
  // segment backgrounds
  for (const s of S.segs) { const a = x(s[0]); ctx.fillStyle = S.C.seg; ctx.fillRect(a, ROW.beats, x(s[1]) - a, ROW.wave); }
  // waveform: kept parts in colour, the rest grey
  const mid = ROW.beats + ROW.wave / 2, amp = ROW.wave / 2 - 5, P = S.D.peaks, rate = S.D.rate;
  for (let px = 0; px < w; px++) {
    const ta = (px + off) / W * dur, tb = (px + 1 + off) / W * dur;
    const i0 = Math.floor(ta * rate), i1 = Math.max(i0 + 1, Math.ceil(tb * rate));
    let m = 0;
    for (let i = i0; i < i1 && i < P.length; i++) if (P[i] > m) m = P[i];
    const h = Math.max(0.5, m / 100 * amp);
    ctx.fillStyle = kept(ta) ? S.C.waveIn : S.C.wave;
    ctx.fillRect(px, mid - h, 1, h * 2);
  }
  // pause points: where cuts snap
  ctx.fillStyle = S.C.text3;
  for (const [p] of S.D.pauses) {
    if (p < t0 || p > t1) continue;
    ctx.fillRect(Math.round(x(p)), Y_FX - 4, 1, 4);
  }
  // segment edges and numbers (selected one last, on top)
  const order = S.segs.map((_, i) => i).filter(i => i !== S.sel).concat(S.segs[S.sel] ? [S.sel] : []);
  ctx.font = "600 11px system-ui, sans-serif";
  for (const i of order) {
    const s = S.segs[i], a = x(s[0]), z = x(s[1]), on = i === S.sel && !S.selFx, col = on ? S.C.sel : S.C.segLine;
    if (z < -20 || a > w + 20) continue;
    ctx.strokeStyle = col;
    ctx.lineWidth = on ? 2 : 1;
    ctx.strokeRect(a + 0.5, ROW.beats + 0.5, Math.max(1, z - a - 1), ROW.wave - 1);
    ctx.fillStyle = col;
    ctx.fillRect(a - 1, ROW.beats, 3, ROW.wave);
    ctx.fillRect(z - 2, ROW.beats, 3, ROW.wave);
    const label = String(i + 1) + (s[2] ? " · " + (s[2] > 0 ? "+" : "") + s[2] + " dB" : "");
    const lw = ctx.measureText(label).width + 8;
    ctx.fillRect(a, ROW.beats, lw, 15);
    ctx.fillStyle = "#ffffff";
    ctx.fillText(label, a + 4, ROW.beats + 11);
  }
  drawFx(x, t0, t1, w);
  for (let i = 1; i < S.segs.length; i++) {          // transitions: a tag at the cut
    const tr = cutInto(i) && transOf(S.segs[i]);
    if (!tr) continue;
    const a = x(S.segs[i][0]);
    if (a < -40 || a > w + 40) continue;
    ctx.font = "600 10px system-ui, sans-serif";
    const lw = ctx.measureText(tr.style).width + 10;
    ctx.fillStyle = S.C.hot;
    ctx.fillRect(a - lw / 2, ROW.beats + ROW.wave - 15, lw, 14);
    ctx.fillStyle = "#fff";
    ctx.fillText(tr.style, a - lw / 2 + 5, ROW.beats + ROW.wave - 4);
  }
  drawVfx(x, t0, t1, w, "cam");
  drawVfx(x, t0, t1, w, "vfx");
  drawSubs(x, t0, t1, w);
  // ruler
  const pxPerS = W / dur;
  const step = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300].find(s => s * pxPerS >= 70) || 600;
  ctx.font = "11px system-ui, sans-serif";
  ctx.fillStyle = S.C.text2;
  for (let t = Math.floor(t0 / step) * step; t <= t1 + step; t += step) {
    const px = x(t);
    ctx.fillRect(px, Y_RULER, 1, 4);
    ctx.fillText(fmt(t, step < 1 ? 1 : 0), px + 3, Y_RULER + 14);
  }
  // playhead
  const ph = x(video.currentTime);
  ctx.fillStyle = S.C.play;
  ctx.fillRect(ph - 1, 0, 2, Y_RULER);
  // time of the edge, sound or effect being dragged
  const df = S.drag && (S.drag.fx || S.drag.vis) ? curFx() : null, ds = S.drag && S.drag.sub ? curSub() : null;
  const dragT = df ? (S.drag.edge === "end" ? df.t + df.d : df.t) : ds ? (S.drag.edge === "t1" ? ds.t1 : ds.t0)
    : S.drag && S.drag.edge !== undefined && S.segs[S.drag.i] ? S.segs[S.drag.i][S.drag.edge] : null;
  if (dragT !== null && dragT !== undefined) {
    const label = fmt(dragT), lw = ctx.measureText(label).width + 8, px = Math.min(Math.max(x(dragT) - lw / 2, 0), w - lw);
    ctx.fillStyle = S.C.sel;
    ctx.fillRect(px, ROW.beats + ROW.wave / 2 - 9, lw, 18);
    ctx.fillStyle = "#ffffff";
    ctx.fillText(label, px + 4, ROW.beats + ROW.wave / 2 + 4);
  }
}

// The sound lane: hits (where sounds snap) as ticks, each sound as a diamond with its length.
function drawFx(x, t0, t1, w) {
  const mid = Y_FX + ROW.fx / 2;
  ctx.fillStyle = S.C.border;
  ctx.fillRect(0, Y_FX, w, 1);
  ctx.globalAlpha = 0.55;
  ctx.fillStyle = S.C.hot;
  for (const [t, s] of S.D.hits) {
    if (t < t0 || t > t1) continue;
    const h = Math.max(3, Math.min(9, 3 + s / 4));
    ctx.fillRect(Math.round(x(t)), Y_FX + 1, 1, h);
  }
  ctx.globalAlpha = 1;
  const list = S.fx.filter(f => f.type === "sfx").sort((a, b) => a.t - b.t);
  list.forEach((f, k) => {
    const snd = sound(f.sound), len = snd ? snd.length : 0.5;
    const a = x(f.t), z = x(f.t + len), on = f.id === S.selFx;
    if (z < -10 || a > w + 10) return;
    const col = on ? S.C.sel : f.by === "claude" ? S.C.cool : S.C.hot;
    ctx.globalAlpha = kept(f.t) ? 1 : 0.35;
    // where it plays, through the waveform
    ctx.fillStyle = col;
    ctx.globalAlpha *= 0.5;
    ctx.fillRect(Math.round(a), ROW.beats, 1, ROW.wave);
    ctx.globalAlpha = kept(f.t) ? 0.35 : 0.15;
    ctx.fillRect(a, mid - 2, Math.max(2, z - a), 4);
    ctx.globalAlpha = kept(f.t) ? 1 : 0.35;
    ctx.beginPath();
    ctx.moveTo(a, mid - 7); ctx.lineTo(a + 7, mid); ctx.lineTo(a, mid + 7); ctx.lineTo(a - 7, mid); ctx.closePath();
    ctx.fill();
    if (on) { ctx.strokeStyle = S.C.text; ctx.lineWidth = 1.5; ctx.stroke(); }
    const next = list[k + 1] ? x(list[k + 1].t) : w;
    const room = next - a - 14;
    if (room > 24) {
      ctx.fillStyle = S.C.text2;
      ctx.font = (on ? "600 " : "") + "11px system-ui, sans-serif";
      ctx.fillText(snd ? snd.name : f.sound + " (missing)", a + 10, mid + 4, room);
    }
    ctx.globalAlpha = 1;
  });
}

// The picture lane: punch-ins as blocks, push-ins as ramps, shakes as zigzags, flashes as
// fading bars. Drag one to move it, drag its right end to change its length.
function visLabel(f) {
  if (ZOOMS.includes(f.type)) return `${f.type === "punch" ? "punch" : "push"} ${f.region || "centre"} ${nice(f.zoom)}×`;
  if (f.type === "shake") return "shake " + (STRENGTH[f.strength] || "");
  if (f.type === "frame") return f.top || f.bottom ? `${f.top || "whole"} / ${f.bottom || "whole"}`
                                                  : f.region === FULL ? "whole picture" : "show " + (f.region || "?");
  if (f.type === "text") return `“${f.text}”`;
  if (f.type === "spot") return "◯ " + (f.style || "spotlight");
  if (f.type === "layer") return `▣ ${f.region || "?"} ${Math.round((f.opacity || 0.7) * 100)}%`;
  if (f.type === "caption") return "Aa " + [f.fx || "pop", f.font, f.at, f.size].filter(Boolean).join(" · ");
  if (f.type === "overlay") return (f.at === "fill" ? "⧉ cutaway · " : "") + ({ sound: "♪ only · ", picture: "muted · " }[f.mode] || "")
                                   + (overlayOf(f.overlay) || { name: f.overlay + " (gone)" }).name;
  return "flash " + f.color;
}
// A colour per region, so the Camera lane shows at a glance what's on screen when.
const REGION_COLS = ["#2a78d6", "#0f9d74", "#c2410c", "#8b5cf6", "#d4a106", "#db2777", "#0891b2", "#65a30d"];
export function regionCol(name) {
  const k = Object.keys(S.D.regions).indexOf(name);
  return k < 0 ? S.C.text3 : REGION_COLS[k % REGION_COLS.length];
}
function drawVfx(x, t0, t1, w, lane) {
  const Y = lane === "cam" ? Y_CAM : Y_VFX, H = lane === "cam" ? ROW.cam : ROW.vfx;
  const top = Y + 3, bot = Y + H - 3, mid = (top + bot) / 2;
  ctx.fillStyle = S.C.border;
  ctx.fillRect(0, Y, w, 1);
  const list = S.fx.filter(f => isVis(f) && laneOf(f) === lane).sort((a, b) => a.t - b.t);
  list.forEach((f, k) => {
    const a = x(f.t), z = Math.max(x(f.t + f.d), a + 4), on = f.id === S.selFx;
    if (z < -10 || a > w + 10) return;
    const col = on ? S.C.sel : lane === "cam" ? regionCol(f.region) : f.by === "claude" ? S.C.cool : S.C.hot,
          alpha = keptFx(f) ? 1 : 0.35;
    ctx.fillStyle = col;
    ctx.strokeStyle = col;
    if (f.type === "push") {
      ctx.globalAlpha = alpha * 0.6;
      ctx.beginPath(); ctx.moveTo(a, bot); ctx.lineTo(z, top); ctx.lineTo(z, bot); ctx.closePath(); ctx.fill();
    } else if (f.type === "shake") {
      ctx.globalAlpha = alpha;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      for (let px = a, up = true; px <= z; px += 4, up = !up) {
        const amp = (bot - top) / 2 * (1 - (px - a) / Math.max(1, z - a) * 0.7);
        px === a ? ctx.moveTo(px, mid) : ctx.lineTo(px, mid + (up ? -amp : amp));
      }
      ctx.stroke();
    } else if (f.type === "flash") {
      const g = ctx.createLinearGradient(a, 0, z, 0);
      g.addColorStop(0, col);
      g.addColorStop(1, "transparent");
      ctx.globalAlpha = alpha * 0.8;
      ctx.fillStyle = g;
      ctx.fillRect(a, top, z - a, bot - top);
    } else if (f.type === "frame") {                       // a shot showing another region: solid
      ctx.globalAlpha = alpha * 0.85;
      ctx.fillRect(a, top, z - a, bot - top);
    } else {
      ctx.globalAlpha = alpha * 0.45;
      ctx.fillRect(a, top, z - a, bot - top);
    }
    ctx.globalAlpha = alpha;
    ctx.fillStyle = col;
    ctx.fillRect(a, top, 2, bot - top);                       // where it starts
    if (on) { ctx.strokeStyle = S.C.text; ctx.lineWidth = 1.5; ctx.strokeRect(a + 0.5, top + 0.5, z - a - 1, bot - top - 1); }
    if (on || z - a > 14) { ctx.fillStyle = on ? S.C.text : col; ctx.fillRect(z - 2, top, 2, bot - top); }   // drag to resize
    const room = (list[k + 1] ? x(list[k + 1].t) : w) - a - 8;
    ctx.font = (on ? "600 " : "") + "11px system-ui, sans-serif";
    const label = [visLabel(f), f.type].find(s => ctx.measureText(s).width <= room);   // never squashed
    if (label) {
      ctx.fillStyle = S.C.text;
      ctx.fillText(label, a + 5, mid + 4);
    }
    ctx.globalAlpha = 1;
  });
}

// The subtitle lane: one block per line, its text inside. Drag an edge to retime it, the
// middle to move it; edges snap to Whisper's word times.
function drawSubs(x, t0, t1, w) {
  ctx.fillStyle = S.C.border;
  ctx.fillRect(0, Y_SUBS, w, 1);
  ctx.font = "11px system-ui, sans-serif";
  // Lines that overlap (two voices at once) share the lane: one on top, one below.
  const order = S.subs.slice().sort((p, q) => p.t0 - q.t0), half = new Map();
  for (let i = 0; i < order.length; i++)
    for (let j = i + 1; j < order.length && order[j].t0 < order[i].t1; j++) {
      if (!half.has(order[i].id)) half.set(order[i].id, 0);
      half.set(order[j].id, half.get(order[i].id) === 0 ? 1 : 0);
    }
  for (const c of S.subs) {
    if (c.t1 < t0 || c.t0 > t1) continue;
    const row = half.get(c.id), mid = (Y_SUBS + 3 + Y_SUBS + ROW.subs - 3) / 2;
    const top = row === undefined ? Y_SUBS + 3 : row === 0 ? Y_SUBS + 3 : mid + 1;
    const bot = row === undefined ? Y_SUBS + ROW.subs - 3 : row === 0 ? mid - 1 : Y_SUBS + ROW.subs - 3;
    const a = x(c.t0), z = Math.max(x(c.t1), a + 3), on = c.id === S.selSub, vc = cueColor(c);
    const col = on ? S.C.sel : vc ? "#" + vc : c.by === "you" ? S.C.hot : c.by === "claude" ? S.C.cool : S.C.other;
    ctx.globalAlpha = (S.subStyle.on ? 1 : 0.5) * (S.segs.some(s => c.t0 < s[1] && c.t1 > s[0]) ? 1 : 0.35);
    ctx.fillStyle = col;
    ctx.globalAlpha *= on ? 0.55 : 0.3;
    ctx.fillRect(a, top, z - a - 1, bot - top);
    ctx.globalAlpha /= on ? 0.55 : 0.3;
    ctx.fillRect(a, top, 2, bot - top);
    if (on) { ctx.strokeStyle = S.C.text; ctx.lineWidth = 1.5; ctx.strokeRect(a + 0.5, top + 0.5, z - a - 2, bot - top - 1); }
    if (z - a > 24) {
      ctx.fillStyle = S.C.text;
      ctx.fillText(fitText(c.text.replace(/\*/g, ""), z - a - 9), a + 5, bot - 5);
    }
    ctx.globalAlpha = 1;
  }
}

function fitText(text, room) {          // cut with "…" rather than squash
  if (ctx.measureText(text).width <= room) return text;
  let lo = 0, hi = text.length;
  while (lo < hi) { const m = (lo + hi + 1) >> 1; if (ctx.measureText(text.slice(0, m) + "…").width <= room) lo = m; else hi = m - 1; }
  return lo ? text.slice(0, lo) + "…" : "";
}
export function follow(t) {           // keep the playhead on screen while playing zoomed in
  const px = X(t), l = scroller.scrollLeft, w = scroller.clientWidth;
  if (px < l || px > l + w - 16) scroller.scrollLeft = px - w * 0.15;
}
export function paintTime(t) {
  let text = fmt(t);
  if (S.editPlay && S.segs[S.editPlay.i]) {
    const before = S.segs.slice(0, S.editPlay.i).reduce((n, s) => n + s[1] - s[0], 0);
    text += "  ·  edit " + fmt(before + Math.max(0, t - S.segs[S.editPlay.i][0]), 1) + " / " + fmt(total(), 1);
  }
  $("[data-ed-time]").textContent = text;
}

// ── hit-testing ───────────────────────────────────────────────────────────
export function hitSeg(x) {
  let best = null, bestD = EDGE_PX + 1;
  const order = S.segs.map((_, i) => i).sort((a, b) => (b === S.sel) - (a === S.sel));
  for (const i of order) {
    for (const edge of [0, 1]) {
      const d = Math.abs(x - X(S.segs[i][edge]));
      if (d < bestD) { best = { i, edge }; bestD = d; }
    }
  }
  if (best) return best;
  const inside = order.find(i => x >= X(S.segs[i][0]) && x <= X(S.segs[i][1]));
  return inside === undefined ? null : { i: inside };
}
export function hitFx(x) {
  let best = null, bestD = FX_PX + 1;
  for (const f of S.fx) {
    if (f.type !== "sfx") continue;
    const d = Math.abs(x - X(f.t)) - (f.id === S.selFx ? 0.5 : 0);
    if (d < bestD) { best = f; bestD = d; }
  }
  return best;
}
// The effect under x in the picture lane, and whether x is on its right end (to resize it).
export function hitVfx(x, lane) {
  const under = S.fx.filter(f => isVis(f) && laneOf(f) === lane && x >= X(f.t) - 4 && x <= Math.max(X(f.t + f.d), X(f.t) + 4) + 4);
  if (!under.length) return null;
  const f = under.find(f => f.id === S.selFx) || under.sort((a, b) => b.t - a.t)[0];
  const end = X(f.t + f.d);
  return { f, edge: Math.abs(x - end) <= EDGE_PX && end - X(f.t) > 10 ? "end" : null };
}
// The subtitle line under x, and whether x is on its start or end edge.
export function hitSubAt(x) {
  const c = S.subs.find(c => c.id === S.selSub && x >= X(c.t0) - 4 && x <= X(c.t1) + 4)
         || S.subs.find(c => x >= X(c.t0) - 3 && x <= X(c.t1) + 3);
  if (!c) return null;
  const a = X(c.t0), z = X(c.t1), wide = z - a > 14;
  return { c, edge: wide && Math.abs(x - a) <= EDGE_PX ? "t0" : wide && Math.abs(x - z) <= EDGE_PX ? "t1" : null };
}
