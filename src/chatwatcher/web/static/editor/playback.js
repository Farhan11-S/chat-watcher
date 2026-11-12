// Playing the edit: seeking, ▶ Play edit (kept parts only), and the toolbar's buttons.
import { $, S, video } from "./state.js";
import { audio, stopSounds } from "./audio.js";

export function seek(t) {
  video.currentTime = Math.min(Math.max(0, t), S.D.duration);
  S.needsDraw = true;
}
export function playEdit(from = 0, only = false, at = null) {
  if (!S.segs.length) return;
  audio();
  S.editPlay = { i: from, only };
  const t = at === null ? S.segs[from][0] : at;
  S.armFrom = t - 0.0005;
  seek(t);
  video.play();
  paintButtons();
}
// Play the edit from the segment closest to the playhead: from the playhead itself when
// it's inside one, else from the start of the nearest segment. Then on to the end.
export function playFromHere() {
  if (!S.segs.length) return;
  const t = video.currentTime;
  const inside = S.segs.findIndex(s => s[0] <= t && t < s[1] - 0.1);
  if (inside >= 0) { playEdit(inside, false, t); return; }
  const gap = s => (t < s[0] ? s[0] - t : t - s[1]);
  let best = 0;
  S.segs.forEach((s, i) => { if (gap(s) < gap(S.segs[best])) best = i; });
  playEdit(best);
}
export function stopEditPlay() {
  if (!S.editPlay) return;
  S.editPlay = null;
  paintButtons();
}
export function toggle() {
  audio();
  if (video.paused) video.play(); else { video.pause(); stopEditPlay(); }
}
video.addEventListener("pause", () => { if (S.editPlay && !video.seeking) stopEditPlay(); stopSounds(); S.previewUntil = null; S.needsDraw = true; });
video.addEventListener("play", () => audio());
video.addEventListener("seeked", () => { S.needsDraw = true; });

export function paintButtons() {
  const rb = $('[data-ed-act="regions"]');
  const nr = Object.keys(S.D.regions).length;
  rb.textContent = S.regionMode ? "✓ Done with regions" : nr ? `▢ Draw regions (${nr})` : "▢ Draw regions";
  rb.classList.toggle("on", S.regionMode);
  const b = $('[data-ed-act="play-edit"]');
  b.textContent = S.editPlay && !S.editPlay.only ? "⏹ Stop" : "▶ Play edit";
  $('[data-ed-act="clear-claude"]').disabled = !S.fx.some(f => f.by === "claude");
}
