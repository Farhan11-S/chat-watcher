// Helpers every module uses: formatting, small DOM builders, POST, snapping, the theme's
// colours, subtitle wrapping (the same rules as subs/lines.py), the status lines, and what
// gets saved (body).
import { $, S, VIS } from "./state.js";

// ── helpers ──────────────────────────────────────────────────────────────
export function fmt(t, d = 2) {
  const k = 10 ** d;
  let v = Math.round(Math.max(0, t) * k);
  const m = Math.floor(v / (60 * k));
  v -= m * 60 * k;
  let s = (v / k).toFixed(d);
  if (v < 10 * k) s = "0" + s;
  return m + ":" + s;
}
export const r3 = v => Math.round(v * 1000) / 1000;
export const clone = a => a.map(s => s.slice());
export const cloneFx = a => a.map(f => ({ ...f }));
export const total = () => S.segs.reduce((n, s) => n + s[1] - s[0], 0);
export const hold = () => { window.cwHold = true; };   // don't auto-reload the page under the editor
export const sound = id => S.D.sounds.find(s => s.id === id);
export const curFx = () => S.fx.find(f => f.id === S.selFx);
export const kept = t => S.segs.some(s => t >= s[0] && t < s[1]);
export const isVis = f => VIS.includes(f.type);
export const keptFx = f => (isVis(f) ? S.segs.some(s => f.t < s[1] && f.t + f.d > s[0]) : kept(f.t));
export const nice = v => String(Math.round(v * 100) / 100);
export function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
export function btn(text, title, data) {
  const b = el("button", "small", text);
  b.type = "button";
  if (title) b.title = title;
  for (const [k, v] of Object.entries(data || {})) b.dataset[k] = v;
  return b;
}
export function select(options, value, data, label, custom = v => (v > 0 ? "+" : "") + v + " dB") {
  const s = el("select");
  for (const [k, v] of Object.entries(data)) s.dataset[k] = v;
  s.setAttribute("aria-label", label);
  for (const [val, text] of options) {
    const o = el("option", "", text);
    o.value = val;
    if (String(val) === String(value)) o.selected = true;
    s.appendChild(o);
  }
  if (!options.some(([val]) => String(val) === String(value))) {   // a value from Claude or an older edit
    const o = el("option", "", custom(value));
    o.value = value;
    o.selected = true;
    s.appendChild(o);
  }
  return s;
}
export async function post(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  let d = {};
  try { d = await r.json(); } catch (e) {}
  if (!r.ok || d.ok === false) throw new Error(d.error || ("HTTP " + r.status));
  return d;
}
export function snap(t, win, points) {
  let best = t, cost = Infinity;
  for (const [p, depth] of points || S.D.pauses) {
    const dt = Math.abs(p - t);
    if (dt > win) continue;
    const c = dt / (1 + depth / 12);
    if (c < cost) { cost = c; best = p; }
  }
  return best;
}
function colors() {
  const cs = getComputedStyle(document.documentElement);
  const v = n => cs.getPropertyValue(n).trim();
  S.C = { bg: v("--surface-2"), wave: v("--text-3"), waveIn: v("--series-1"), seg: v("--series-1-soft"),
        segLine: v("--series-1"), sel: v("--series-2"), text: v("--text"), text2: v("--text-2"), text3: v("--text-3"),
        play: v("--bad"), other: v("--other"), hot: v("--series-2"), cool: v("--series-1"), border: v("--border") };
}
colors();
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { colors(); S.needsDraw = true; });
window.addEventListener("cw-theme", () => { colors(); S.needsDraw = true; });   // the sidebar's theme button
export const beatColor = b => ({ hook: S.C.hot, payoff: S.C.hot, reaction: S.C.cool, callback: S.C.cool })[b] || S.C.other;

// ── subtitle layout: the same rules as subs/lines.py (wrap, runs), so the preview matches ──
const WIDE = /[\u1100-\u115f\u2e80-\ua4cf\uac00-\ud7a3\uf900-\ufaff\ufe30-\ufe4f\uff00-\uff60\uffe0-\uffe6]/g;
export const subWidth = t => { const p = t.replace(/\*/g, ""); return p.length + (p.match(WIDE) || []).length; };
export function wrapSub(text, max = S.D.sub_layout.max_line, lines = 2) {
  if (subWidth(text) <= max || lines < 2) return [text];
  const half = subWidth(text) / 2, chars = [...text];
  let cuts = chars.map((ch, i) => (ch === " " ? i : -1)).filter(i => i >= 0);
  if (!cuts.length) cuts = chars.map((_, i) => i).slice(1);
  let best = cuts[0], cost = Infinity;
  for (const i of cuts) {
    const c = Math.abs(subWidth(chars.slice(0, i).join("")) - half);
    if (c < cost) { cost = c; best = i; }
  }
  const a = chars.slice(0, best).join("").trimEnd(), b = chars.slice(best).join("").trimStart();
  if (!(a && b)) return [text];
  if (lines === 2) return [a, b];
  return [...wrapSub(a, max, Math.floor(lines / 2)), ...wrapSub(b, max, lines - Math.floor(lines / 2))];
}
export function runsOf(line, on) {
  const out = [];
  line.split("*").forEach((part, i) => { if (i) on = !on; if (part) out.push([part, on]); });
  return [out, on];
}
export const curSub = () => S.subs.find(c => c.id === S.selSub);
export const subAt = t => S.subs.find(c => c.t0 <= t && t < c.t1);
export const overlayOf = id => (S.D.overlays || []).find(o => o.id === id);
export const ovPicture = f => f.mode !== "sound";
export const ovSound = f => f.mode !== "picture";
// A segment's transition: the effect at its start (clip time), only on a real cut.
export const cutInto = i => i > 0 && S.segs[i] && Math.abs(S.segs[i][0] - S.segs[i - 1][1]) >= 0.002;
export const transOf = s => S.fx.find(f => f.type === "transition" && Math.abs(f.t - s[0]) < 0.05);
export function subStatus(text) { $("[data-ed-sub-status]").textContent = text; }
export function setSpeaker(c, k) {
  if (k === null) delete c.speaker; else c.speaker = k;
  c.by = "you";
}

let flashTimer = null;
export function flash(text) {
  const s = $("[data-ed-saved]");
  s.textContent = text;
  s.className = "ed-warn";
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => { s.textContent = ""; s.className = "muted"; }, 2500);
}
// A message under the list (where you're typing), for a few seconds.
let listMsgTimer = null;
export function listFlash(text) {
  const m = $("[data-ed-list-msg]");
  m.textContent = text;
  m.hidden = false;
  clearTimeout(listMsgTimer);
  listMsgTimer = setTimeout(() => { m.hidden = true; }, 5000);
}
export function setSaved(text, bad) {
  const s = $("[data-ed-saved]");
  clearTimeout(flashTimer);
  s.textContent = text;
  s.className = bad ? "ed-warn" : "muted";
}
export function libraryStatus() {
  const n = S.D.sounds.length;
  // Only when something needs doing: the status line is for news, not a manual.
  const regionsTip = Object.keys(S.D.regions).length ? "" : "Draw the regions (⋯ More → ▢ Draw regions) so the camera can show her face, the game…";
  if (!n) fxStatus("No sounds in your library yet. " + regionsTip, "", true);
  else if (regionsTip) fxStatus(regionsTip, "");
}
export function fxStatus(text, kind, linkSounds) {
  const s = $("[data-ed-fx-status]");
  s.textContent = text;
  s.className = "ed-fx-status " + (kind || "muted");
  if (linkSounds) {
    const a = el("a", "", " Open Sounds →");
    a.href = "/sounds";
    s.appendChild(a);
  }
}

// What gets saved (io.js) and sent to Claude (ai.js).
export const body = () => ({ segments: S.segs.map(s => [r3(s[0]), r3(s[1]), s[2] || 0]), source: S.source,
                      effects: S.fx.map(f => ({ ...f, t: r3(f.t) })),
                      subs: S.subsMine ? S.subs.map(c => ({ ...c, t0: r3(c.t0), t1: r3(c.t1) })) : "auto",
                      subs_style: S.subStyle, kind: S.kind, ...(S.kind === "short" && S.layout ? { layout: S.layout } : {}) });
