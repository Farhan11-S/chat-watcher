// Changing the edit: undo, segments (edges, split, add, move, volume), sounds and picture
// effects, and the subtitle lines (add, split, join, retime, names, polish).
import { $, DRAG_SNAP_S, MIN, S, SUB_MIN, ZOOMS, call, pk, video } from "./state.js";
import {
  body, clone, cloneFx, curFx, curSub, flash, fxStatus, post, r3, setSaved, setSpeaker, snap, sound,
  subAt, subStatus, transOf,
} from "./util.js";
import { FULL, fitZoom } from "./preview.js";
import { buffer } from "./audio.js";
import { seek, stopEditPlay } from "./playback.js";
import { save, showRender } from "./io.js";

// ── editing ──────────────────────────────────────────────────────────────
export function pushUndo() {
  S.undo.push({ segs: clone(S.segs), sel: S.sel, source: S.source, fx: cloneFx(S.fx), selFx: S.selFx, subs: cloneFx(S.subs), subStyle: { ...S.subStyle },
              selSub: S.selSub, subsMine: S.subsMine, layout: S.layout && { ...S.layout } });
  if (S.undo.length > 200) S.undo.shift();
  $('[data-ed-act="undo"]').disabled = false;
}
export function doUndo() {
  const u = S.undo.pop();
  if (!u) return;
  S.segs = u.segs; S.sel = u.sel; S.source = u.source; S.fx = u.fx; S.selFx = u.selFx;
  S.subs = u.subs; S.subStyle = u.subStyle; S.selSub = u.selSub; S.subsMine = u.subsMine;
  if (u.layout) S.layout = u.layout;
  S.listFor = null;
  S.subKey = "";
  $('[data-ed-act="undo"]').disabled = !S.undo.length;
  changed();
}
export function changed() {
  stopEditPlay();
  S.dirty = true;
  call.paintAll();
  clearTimeout(S.saveTimer);
  S.saveTimer = setTimeout(save, 600);
  setSaved("Saving…");
}
export function selectSeg(i, jump = true) {
  if (!S.segs.length) return;
  S.sel = (i + S.segs.length) % S.segs.length;
  S.selFx = null;
  if (jump) seek(S.segs[S.sel][0]);
  call.paintAll();
}
function selectFx(id, jump = true) {
  S.selFx = id;
  const f = curFx();
  if (f && jump) seek(f.t);
  call.paintAll();
}
export function setEdge(edge, t) {
  const s = S.segs[S.sel];
  if (!s) return;
  if (edge === 0 ? t > s[1] - MIN : t < s[0] + MIN) {
    flash(edge === 0 ? "The start must be before the end." : "The end must be after the start.");
    return;
  }
  pushUndo();
  const tr = edge === 0 ? transOf(s) : null;
  s[edge] = r3(Math.min(Math.max(0, t), S.D.duration));
  if (tr) tr.t = s[0];
  seek(s[edge]);
  changed();
}
export function nudge(edge, dt) {
  const s = S.segs[S.sel];
  if (!s) return;
  setEdge(edge, s[edge] + dt);
}
export function split() {
  const t = video.currentTime;
  let j = S.segs[S.sel] && t - S.segs[S.sel][0] >= MIN && S.segs[S.sel][1] - t >= MIN ? S.sel
    : S.segs.findIndex(s => t - s[0] >= MIN && s[1] - t >= MIN);
  if (j < 0) { flash("Put the playhead inside a segment to split it."); return; }
  pushUndo();
  const [a, b, g] = S.segs[j];
  S.segs.splice(j, 1, [a, r3(t), g], [r3(t), b, g]);
  S.sel = j + 1;
  S.selFx = null;
  changed();
}
export function add() {
  let a = video.currentTime, b = Math.min(S.D.duration, a + 5);
  if (b - a < 1) a = Math.max(0, b - 5);
  if (S.snapOn) { a = snap(a, DRAG_SNAP_S); b = snap(b, DRAG_SNAP_S); }
  if (b - a < MIN) return;
  pushUndo();
  S.segs.splice(S.sel + 1, 0, [r3(a), r3(b), 0]);
  S.sel = S.sel + 1;
  S.selFx = null;
  changed();
}
export function del() {
  if (S.selSub && curSub()) { deleteSub(S.selSub); return; }
  if (S.selFx) { pushUndo(); S.fx = S.fx.filter(f => f.id !== S.selFx); S.selFx = null; changed(); return; }
  if (S.segs.length <= 1) { flash("An edit needs at least one segment."); return; }
  pushUndo();
  S.segs.splice(S.sel, 1);
  S.sel = Math.min(S.sel, S.segs.length - 1);
  changed();
}
export function move(dir) {
  const j = S.sel + dir;
  if (j < 0 || j >= S.segs.length) return;
  pushUndo();
  [S.segs[S.sel], S.segs[j]] = [S.segs[j], S.segs[S.sel]];
  S.sel = j;
  changed();
}
export function setGain(g) {
  const s = S.segs[S.sel];
  if (!s || s[2] === g) return;
  pushUndo();
  s[2] = g;
  changed();
}
export function startOver() {
  const p = [[0, +S.D.duration.toFixed(3), 0]];
  const same = JSON.stringify(p) === JSON.stringify(S.segs);
  if (!same && S.D.saved && !confirm(`Replace your ${S.segs.length} segment(s) with the whole clip? (Undo brings them back.)`)) return;
  pushUndo();
  S.segs = p;
  S.source = "whole";
  S.sel = 0;
  S.selFx = null;
  changed();
  seek(S.segs[0][0]);
}

// sound effects
export function addFx() {
  if (!S.D.sounds.length) { fxStatus("Your sound library is empty: add sounds on the Sounds page first.", "err", true); return; }
  const snd = sound(S.lastSound) || S.D.sounds[0];
  let t = video.currentTime;
  if (S.snapOn) t = snap(t, DRAG_SNAP_S, S.D.hits);
  pushUndo();
  const f = { id: "u" + Math.random().toString(36).slice(2, 8), type: "sfx", t: r3(t), sound: snd.id,
              gain: snd.gain || 0, duck: 0, by: "you", why: "" };
  S.fx.push(f);
  S.selFx = f.id;
  buffer(f.sound).catch(() => {});
  changed();
}
export function editFx(change) {
  const f = curFx();
  if (!f) return;
  pushUndo();
  Object.assign(f, change, { by: "you" });
  if (change.sound) {
    S.lastSound = change.sound;
    try { localStorage.setItem("cw-ed-sound", S.lastSound); } catch (e) {}
    buffer(change.sound).catch(() => {});
  }
  changed();
}
// zooms, shakes and flashes
export function addVis(type) {
  const [d] = S.D.visual.types[type];
  let t = video.currentTime;
  if (S.snapOn && type !== "push") t = snap(t, DRAG_SNAP_S, S.D.hits);   // these land on a moment
  const f = { id: "u" + Math.random().toString(36).slice(2, 8), type, t: r3(t), d: Math.min(d, S.D.duration - t), by: "you", why: "" };
  if (f.d < S.D.visual.types[type][1]) { flash("Too close to the end of the clip."); return; }
  if (type === "frame") {
    if (S.kind !== "short") { flash("Frame shots are for Shorts: switch to the Short first."); return; }
    const names = Object.keys(S.D.regions);
    f.region = names.find(n => n !== (S.layout && S.layout.bottom)) || FULL;
  } else if (ZOOMS.includes(type)) {
    const names = Object.keys(S.D.regions);
    f.region = names.includes("face") ? "face" : names[0] || "";
    f.zoom = type === "punch" ? (f.region ? Math.min(fitZoom(f.region), 2.5) : 1.5) : 1.25;
  } else if (type === "shake") f.strength = 2;
  else if (type === "layer") {
    const names = Object.keys(S.D.regions);
    if (!names.length) { flash("Draw regions first: a layer shows one of them (⋯ More → ▢ Draw regions)."); return; }
    Object.assign(f, { region: names.find(n => n.includes("chat")) || names[0], at: "middle", dx: 0, dy: 0,
                       size: S.D.layer_info.default_size, opacity: S.D.layer_info.default_opacity });
  } else if (type === "caption") Object.assign(f, { fx: "pop", font: "", at: "" });
  else f.color = "white";
  pushUndo();
  S.fx.push(f);
  S.selFx = f.id;
  changed();
  if (ZOOMS.includes(type) && !f.region)
    fxStatus("No regions yet, so this zooms on the centre. Draw her face with ⋯ More → ▢ Draw regions to aim zooms at it.", "");
}
export function previewFx() {         // play from a little before the effect to a little after it
  const f = curFx();
  if (!f) return;
  stopEditPlay();
  seek(Math.max(0, f.t - 1));
  S.previewUntil = Math.min(S.D.duration, f.t + (f.d || (sound(f.sound) || { length: 1 }).length) + 1);
  video.play();
}
// subtitles
export const subIndex = id => S.subs.findIndex(c => c.id === id);
export function subsChanged() { S.subsMine = true; S.listFor = null; S.subKey = ""; changed(); }
export function selectSub(id, jump = true) {
  S.selSub = id;
  S.selFx = null;
  const c = curSub();
  if (c && jump) seek(c.t0);
  call.paintAll();
  const row = c && $(`.ed-line[data-sub="${c.id}"]`);
  if (row && !$("[data-ed-lines]").matches(":hover")) row.scrollIntoView({ block: "nearest" });
}
export function addSub() {
  const t = video.currentTime, next = S.subs.find(c => c.t0 > t), inside = subAt(t);
  if (inside) { selectSub(inside.id, false); flash("There's a line here already: edit it, or split it."); return; }
  const t1 = Math.min(t + 2, next ? next.t0 - 0.04 : S.D.duration);
  if (t1 - t < 0.3) { flash("No room for a new line here."); return; }
  pushUndo();
  const c = { id: "u" + Math.random().toString(36).slice(2, 8), t0: r3(t), t1: r3(t1), text: "…", by: "you" };
  S.subs.push(c);
  S.subs.sort((a, b) => a.t0 - b.t0);
  S.selSub = c.id;
  subsChanged();
  call.paintList();
  const span = $(`[data-sub-text="${c.id}"]`);
  if (span) { span.focus(); document.getSelection().selectAllChildren(span); }
}
export function splitSub(id) {
  const k = subIndex(id), c = S.subs[k];
  if (!c) return;
  let t = video.currentTime;
  if (!(t - c.t0 >= SUB_MIN && c.t1 - t >= SUB_MIN)) {         // playhead not inside: at a word near the middle
    const mid = (c.t0 + c.t1) / 2, starts = (S.D.sub_words || []).map(w => w[0]).filter(w => w - c.t0 >= SUB_MIN && c.t1 - w >= SUB_MIN);
    t = starts.length ? starts.reduce((a, b) => (Math.abs(b - mid) < Math.abs(a - mid) ? b : a)) : mid;
  }
  if (t - c.t0 < SUB_MIN || c.t1 - t < SUB_MIN) { flash("This line is too short to split."); return; }
  const words = c.text.split(" "), f = (t - c.t0) / (c.t1 - c.t0);
  let cut = Math.min(Math.max(1, Math.round(words.length * f)), words.length - 1);
  if (words.length < 2) cut = 1;
  pushUndo();
  const a = words.slice(0, cut).join(" ") || c.text, b = words.slice(cut).join(" ") || "…";
  S.subs.splice(k, 1, { ...c, t1: r3(t - 0.02), text: a, by: "you" },
              { id: "u" + Math.random().toString(36).slice(2, 8), t0: r3(t), t1: c.t1, text: b, by: "you" });
  S.selSub = S.subs[k + 1].id;
  subsChanged();
}
export function joinSub(id) {
  const k = subIndex(id), c = S.subs[k], n = S.subs[k + 1];
  if (!c || !n) { flash("There's no next line to join."); return; }
  pushUndo();
  S.subs.splice(k, 2, { ...c, t1: n.t1, text: (c.text + " " + n.text).trim(), by: "you" });
  S.selSub = c.id;
  subsChanged();
}
export function deleteSub(id) {
  const k = subIndex(id);
  if (k < 0) return;
  pushUndo();
  S.subs.splice(k, 1);
  S.selSub = null;
  subsChanged();
}
export function setSubVoice(id, k) {
  const c = S.subs.find(x => x.id === id);
  if (!c || (c.speaker ?? null) === k) return;
  pushUndo();
  setSpeaker(c, k);
  subsChanged();
}
export function setSubText(id, text) {
  const c = S.subs.find(x => x.id === id);
  text = text.replace(/\s+/g, " ").trim();
  if (!c || text === c.text) return;
  if (!text) { deleteSub(id); return; }
  const learnt = newNames(c.text, text);
  pushUndo();
  Object.assign(c, { text, by: "you" });
  subsChanged();
  if (learnt.length) addNames(learnt);
}
// Names you fix by retyping a line (a capitalised word that isn't a sentence's first, or
// one like holo1GP or CeCe) are remembered for the channel, so Claude spells them right.
const NOT_NAMES = new Set(["i'm", "i'll", "i've", "i'd", "okay", "oh", "yeah", "hey", "wait", "what", "why", "god"]);
function newNames(before, after) {
  const bare = w => w.replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, "");
  const had = new Set(before.replace(/\*/g, "").split(/\s+/).map(w => bare(w).toLowerCase()));
  const ws = after.replace(/\*/g, "").split(/\s+/), out = [];
  ws.forEach((raw, i) => {
    const w = bare(raw);
    if (w.length < 3 || had.has(w.toLowerCase()) || NOT_NAMES.has(w.toLowerCase())) return;
    const special = (/\d/.test(w) && /\p{L}/u.test(w)) || /\p{Ll}\p{Lu}/u.test(w);
    const first = i === 0 || /[.!?…]$/.test(ws[i - 1]);
    if (special || (/^\p{Lu}/u.test(w) && !first)) out.push(w);
  });
  return out;
}
async function addNames(terms) {
  try {
    const d = await post(`/moment/${pk}/names`, { add: terms });
    S.D.names = d.names;
    paintNames();
    if (d.added.length) subStatus(`Remembered for this channel: ${d.added.join(", ")}. Claude will spell ${d.added.length === 1 ? "it" : "them"} like that.`);
  } catch (e) { /* not worth interrupting your typing */ }
}
export async function saveNames() {
  try {
    const d = await post(`/moment/${pk}/names`, { terms: $("[data-ed-names-text]").value });
    S.D.names = d.names;
    paintNames();
    subStatus(`Saved ${d.names.length} name${d.names.length === 1 ? "" : "s"} for this channel.`);
  } catch (e) { subStatus("Couldn't save the names: " + e.message); }
}
export function paintNames() {
  $("[data-ed-names-count]").textContent = S.D.names.length;
  const ta = $("[data-ed-names-text]");
  if (document.activeElement !== ta) ta.value = S.D.names.join("\n");
}
export async function polishSubs() {
  const b = $('[data-ed-act="sub-polish"]');
  b.disabled = true;
  b.dataset.busy = "1";
  clearTimeout(S.saveTimer);
  subStatus("Claude is going over the lines… (usually 15–60 s)");
  try {
    const d = await post(`/moment/${pk}/subs/polish`, body());
    pushUndo();
    S.subs = d.subs;
    S.subsMine = true;
    S.selSub = null;
    S.listFor = null;
    S.subKey = "";
    S.D.names = d.names;
    S.dirty = false;
    S.D.saved = true;
    setSaved("Saved");
    call.paintAll();
    paintNames();
    showRender(d.render);
    const bits = [`Claude changed ${d.changed} line${d.changed === 1 ? "" : "s"}`];
    if (d.joined) bits.push(`joined ${d.joined}`);
    if (d.dropped) bits.push(`dropped ${d.dropped}`);
    subStatus(bits.join(", ") + ". " + (d.note || "") + (d.names_added.length ? ` New names remembered: ${d.names_added.join(", ")}.` : "")
              + " Ctrl+Z undoes.");
  } catch (e) {
    subStatus(e.message);
  } finally {
    b.dataset.busy = "";
    b.disabled = false;
  }
}
export function setSubTime(id, edge, t) {
  const k = subIndex(id), c = S.subs[k];
  if (!c) return;
  const lo = edge === "t0" ? (S.subs[k - 1] ? S.subs[k - 1].t1 : 0) : c.t0 + SUB_MIN;
  const hi = edge === "t0" ? c.t1 - SUB_MIN : (S.subs[k + 1] ? S.subs[k + 1].t0 : S.D.duration);
  pushUndo();
  c[edge] = r3(Math.min(Math.max(t, lo), hi));
  c.by = "you";
  seek(edge === "t0" ? c.t0 : Math.max(c.t0, c.t1 - 0.05));
  subsChanged();
}
export function rebuildSubs() {
  if (!S.D.subs_auto.length) { subStatus("No Whisper words for this clip yet: press 🎙 Transcribe first."); return; }
  if (S.subsMine && !confirm(`Replace your ${S.subs.length} subtitle line(s) with fresh ones from Whisper's words? (Undo brings yours back.)`)) return;
  pushUndo();
  S.subs = cloneFx(S.D.subs_auto);
  S.selSub = null;
  S.subsMine = false;           // they follow the transcript again
  S.listFor = null;
  S.subKey = "";
  changed();
}
export function setSubMode(mode) {
  // Lines that follow Whisper switch to the other way of cutting them; your own lines stay.
  const auto = (S.D.subs_auto_by_mode || {})[mode];
  pushUndo();
  S.subStyle.mode = mode;
  if (!S.subsMine && auto) { S.subs = cloneFx(auto); S.D.subs_auto = auto; S.selSub = null; S.listFor = null; }
  else if (auto) { S.D.subs_auto = auto; subStatus("Your own lines stay as they are; ↻ From transcript rebuilds them " + mode + " at a time."); }
  S.subKey = "";
  changed();
}
export function setSubStyle(change) {
  pushUndo();
  Object.assign(S.subStyle, change);
  S.subKey = "";
  changed();
}
