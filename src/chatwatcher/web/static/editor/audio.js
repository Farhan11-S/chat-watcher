// Sound effects and overlay sounds in the preview (Web Audio), and ducking the clip under them;
// the music bed while ▶ Play edit plays (edit/music.py: bed(), the same sums), and hushes.
import { S, SOUND_LEVEL, video } from "./state.js";
import { editTime, musicFx, ovSound, overlayOf, partAt, partLen, sound, timeParts, total as editTotal, track } from "./util.js";

// ── sound playback for the preview (Web Audio) ───────────────────────────
let actx = null;
const buffers = new Map(), playing = new Set();
let duckUntil = 0, duckF = 1;
export function audio() {
  if (!actx) { try { actx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) { return null; } }
  if (actx.state === "suspended") actx.resume();
  return actx;
}
export function buffer(id) {          // a library sound, or "ov:<id>": an overlay's own sound
  if (!buffers.has(id)) {
    const a = audio();
    if (!a) return Promise.reject(new Error("no audio"));
    const url = id.startsWith("ov:") ? `/media/overlay/${id.slice(3)}/sound` : `/media/sound/${id}`;
    buffers.set(id, fetch(url).then(r => { if (!r.ok) throw new Error(r.status); return r.arrayBuffer(); })
      .then(b => a.decodeAudioData(b)).catch(e => { buffers.delete(id); throw e; }));
  }
  return buffers.get(id);
}
export const preload = () => S.fx.forEach(f => {
  if (f.type === "sfx") buffer(f.sound).catch(() => {});
  else if (f.type === "overlay" && (overlayOf(f.overlay) || {}).sound) buffer("ov:" + f.overlay).catch(() => {});
});
export async function playSound(id, gainDb) {
  const a = audio();
  if (!a) return;
  let b;
  try { b = await buffer(id); } catch (e) { return; }
  const src = a.createBufferSource(), g = a.createGain();
  // gain 0 = about as loud as her talking: move the library level to this clip's level
  g.gain.value = userVol * 10 ** ((gainDb + (S.D.loudness - SOUND_LEVEL)) / 20);
  src.buffer = b;
  src.connect(g).connect(a.destination);
  src.onended = () => playing.delete(src);
  playing.add(src);
  src.start();
}
export function stopSounds() { for (const s of playing) { try { s.stop(); } catch (e) {} } playing.clear(); duckUntil = 0; stopMusic(); }
export function fireOverlay(f) {
  const ov = overlayOf(f.overlay);
  if (ov && ov.sound && ovSound(f)) playSound("ov:" + f.overlay, (f.gain || 0) + (ov.gain || 0));
}
export function fire(f) {
  const snd = sound(f.sound);
  if (!snd) return;
  playSound(f.sound, f.gain);
  if (f.duck < 0) {
    const until = performance.now() + snd.length * 1000;
    duckF = duckUntil > performance.now() ? Math.min(duckF, 10 ** (f.duck / 20)) : 10 ** (f.duck / 20);
    duckUntil = Math.max(duckUntil, until);
  }
}

// The clip's own volume: your setting × the segment's volume × ducking under a sound.
let userVol = video.volume, lastSetVol = null;
video.addEventListener("volumechange", () => {
  if (lastSetVol !== null && Math.abs(video.volume - lastSetVol) < 0.002) return;
  userVol = video.volume;
});
export function mixVolume() {
  const segG = S.editPlay && S.segs[S.editPlay.i] ? Math.min(1, 10 ** ((S.segs[S.editPlay.i][2] || 0) / 20)) : 1;
  const duck = duckUntil > performance.now() && !video.paused ? duckF : 1;
  const part = S.editPlay ? partAt(video.currentTime, S.editPlay.i) : null;    // a time effect's "mute"
  const muted = hushed(video.currentTime) || (part && part.f && part.f.sound === "mute");
  const v = muted ? 0 : Math.max(0, Math.min(1, userVol * segG * duck));
  if (Math.abs(video.volume - v) > 0.002) { lastSetVol = v; video.volume = v; }
}

// ── the music bed ──────────────────────────────────────────────────────────
// The bed on the edit's timeline: { start, offset, length, drop, fadeOut } or null.
export function musicBed() {
  const f = musicFx(), tr = f && track(f.track);
  if (!tr) return null;
  const drop = editTime(f.t);
  if (drop === null) return null;
  const total = editTotal();
  const lead = f.lead === undefined ? -1 : f.lead;
  const start = lead < 0 ? Math.max(0, drop - tr.drop) : Math.max(0, drop - lead, drop - tr.drop);
  const offset = tr.drop - (drop - start), length = Math.min(total - start, tr.length - offset);
  if (length < 0.2) return null;
  const runsOut = start + length < total - 0.05;
  return { start, offset, length, drop, fadeOut: runsOut ? 0 : Math.min(S.D.music_info.fade_out, length / 4) };
}
// The bed as clip-time spans (for the timeline): the parts of kept segments it plays under.
export function musicSpans() {
  const b = musicBed();
  if (!b) return [];
  const out = [];
  let o = 0;
  for (const p of timeParts()) {
    const L = partLen(p), a = Math.max(b.start, o), z = Math.min(b.start + b.length, o + L);
    if (z > a) out.push([p.s + (a - o) * p.k, p.s + (z - o) * p.k]);
    o += L;
  }
  return out;
}
export const hushed = t => S.fx.some(f => f.type === "hush" && t >= f.t && t < f.t + f.d);
const speaking = t => (S.D.speech || []).some(([a, b]) => t >= a - 0.12 && t < b + 0.25);

let music = null;      // { el: <audio>, id }
export function stopMusic() { if (music) { music.el.pause(); } }
// Every frame: while ▶ Play edit plays, the bed plays where it should, at its volume.
export function syncMusic(t) {
  const b = S.editPlay && (!video.paused || S.frozen) && !video.seeking ? musicBed() : null;
  const e = b ? editTime(t, S.editPlay.i) : null;
  if (!b || e === null || e < b.start || e >= b.start + b.length) { stopMusic(); return; }
  const f = musicFx(), tr = track(f.track);
  if (!music || music.id !== tr.id) {
    stopMusic();
    music = { el: new Audio(`/media/music/${tr.id}`), id: tr.id };
    music.el.preload = "auto";
  }
  const want = b.offset + (e - b.start), el = music.el;
  if (el.paused || Math.abs(el.currentTime - want) > 0.15) {
    try { el.currentTime = want; } catch (err) {}
    if (el.paused) el.play().catch(() => {});
  }
  const into = e - b.start, left = b.start + b.length - e;
  let g = (f.gain ?? S.D.music_info.default_gain) + (tr.gain || 0) + (S.D.loudness - S.D.music_info.level);
  if (speaking(t)) g += f.duck ?? S.D.music_info.default_duck;
  let k = userVol * 10 ** (g / 20);
  if (b.fadeOut > 0 && left < b.fadeOut) k *= left / b.fadeOut;
  if (into < S.D.music_info.fade_in) k *= into / S.D.music_info.fade_in;
  if (hushed(t)) k = 0;
  k = Math.max(0, Math.min(1, k));
  // a quick glide, not a jump (the render dips over 0.12 s)
  el.volume = Math.abs(el.volume - k) < 0.02 ? k : el.volume + (k - el.volume) * 0.35;
}
