// Sound effects and overlay sounds in the preview (Web Audio), and ducking the clip under them.
import { S, SOUND_LEVEL, video } from "./state.js";
import { ovSound, overlayOf, sound } from "./util.js";

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
export function stopSounds() { for (const s of playing) { try { s.stop(); } catch (e) {} } playing.clear(); duckUntil = 0; }
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
  const v = Math.max(0, Math.min(1, userVol * segG * duck));
  if (Math.abs(video.volume - v) > 0.002) { lastSetVol = v; video.volume = v; }
}
