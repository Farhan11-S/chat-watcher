// Saving the edit and rendering it: autosave, the render job's status, the result's player.
import { $, S, pk, video } from "./state.js";
import { body, btn, el, fmt, post, setSaved } from "./util.js";
import { stopEditPlay } from "./playback.js";

let renderPoll = null, watchVideo = null;
const outFile = () => (S.kind === "short" ? "short.mp4" : "edit.mp4");
export async function save() {
  if (S.saving) { clearTimeout(S.saveTimer); S.saveTimer = setTimeout(save, 300); return; }
  S.saving = true;
  S.dirty = false;
  try {
    const d = await post(`/moment/${pk}/edit`, body());
    S.D.saved = true;
    setSaved("Saved");
    showRender(d.render);
    if (S.kind === "short") window.dispatchEvent(new Event("cw-short-changed"));   // the cover follows the layout
  } catch (e) {
    S.dirty = true;
    setSaved("Couldn't save: " + e.message, true);
  } finally {
    S.saving = false;
  }
}
// The intro panel (maker.js) changed something edit.mp4 depends on: show whether it's out of date.
window.addEventListener("cw-render-changed", async () => {
  try {
    const d = await (await fetch(`/api/moment/${pk}/render?kind=${S.kind}`, { cache: "no-store" })).json();
    showRender(d.render);
    if (d.render.state === "queued" || d.render.state === "running") pollRender();
  } catch (e) {}
});
window.addEventListener("pagehide", () => {
  if (!S.dirty) return;
  fetch(`/moment/${pk}/edit`, { method: "POST", keepalive: true, headers: { "Content-Type": "application/json" },
                                body: JSON.stringify(body()) });
});
export async function render() {
  clearTimeout(S.saveTimer);
  const b = $('[data-ed-act="render"]');
  b.disabled = true;
  try {
    const d = await post(`/moment/${pk}/render`, body());
    S.dirty = false;
    S.D.saved = true;
    setSaved("Saved");
    showRender(d.render);
    pollRender();
  } catch (e) {
    setSaved(e.message, true);
  } finally {
    b.disabled = false;
  }
}
export function pollRender() {
  clearTimeout(renderPoll);
  renderPoll = setTimeout(async () => {
    try {
      const d = await (await fetch(`/api/moment/${pk}/render?kind=${S.kind}`, { cache: "no-store" })).json();
      showRender(d.render);
      if (d.render.state === "queued" || d.render.state === "running") pollRender();
    } catch (e) { pollRender(); }
  }, 1200);
}
export function showRender(r) {
  const box = $("[data-ed-render-state]");
  box.textContent = "";
  const mb = n => (n / 1048576).toFixed(1) + " MB";
  if (r.state === "queued") {
    const why = r.worker === "stopped" ? "The worker isn't running: start it with cw serve or cw worker."
      : r.behind ? `It starts when the worker finishes its current ${r.behind} job.` : "Starting…";
    box.append(el("p", "ed-status busy", "Queued. " + why));
  } else if (r.state === "running") {
    const pct = Math.round((r.progress || 0) * 100);
    const bar = el("div", "ed-progress");
    const fill = el("span");
    fill.style.width = pct + "%";
    bar.appendChild(fill);
    box.append(el("p", "ed-status busy", `Rendering… ${pct}%`), bar);
  } else if (r.state === "error") {
    box.append(el("p", "ed-status err", "Render failed: " + (r.error || "unknown error")));
  } else if (r.state === "done" || r.state === "stale") {
    const p = el("p", "ed-status " + (r.state === "done" ? "ok" : ""));
    p.textContent = r.state === "done"
      ? `✓ Rendered: ${fmt(r.length || 0, 1)} · ${mb(r.size || 0)}`
      : `Your edit changed since the last render. Render again to update ${outFile()}.`;
    box.appendChild(p);
    const acts = el("div", "actions");
    const watch = btn(watchVideo ? "Hide" : "▶ Watch", "Watch the rendered edit", { edAct: "watch" });
    const dl = el("a", "btn small", "⬇ Download");
    dl.href = r.url.replace("?", "?dl=1&");
    acts.append(watch, dl);
    if (r.srt) {
      const srt = el("a", "btn small", "⬇ .srt");
      srt.href = r.srt;
      srt.title = "The subtitles as a file for YouTube's CC (on the edit's timeline)";
      acts.appendChild(srt);
    }
    acts.appendChild(btn("copy path", "Copy the file's path on this computer", { copy: r.path }));
    box.appendChild(acts);
    if (watchVideo) {
      if (watchVideo.dataset.src !== r.url) { watchVideo.src = r.url; watchVideo.dataset.src = r.url; }
      box.appendChild(watchVideo);
    }
    box.dataset.url = r.url;
  } else {
    box.append(el("p", "faint", S.kind === "short"
      ? "Render makes short.mp4 (1080×1920) next to the clip: your cut in the layout above, sounds mixed in, zooms, subtitles and the title burned in, levelled loudness."
      : "Render makes edit.mp4 next to the clip: frame-exact cuts, your sounds mixed in, levelled loudness, ready to upload."));
  }
}
export function toggleWatch() {
  const box = $("[data-ed-render-state]");
  if (watchVideo) { watchVideo.pause(); watchVideo.remove(); watchVideo = null; }
  else {
    watchVideo = el("video", "ed-watch");
    watchVideo.controls = true;
    watchVideo.src = box.dataset.url;
    watchVideo.dataset.src = box.dataset.url;
    video.pause();
    stopEditPlay();
  }
  const b = box.querySelector('[data-ed-act="watch"]');
  if (b) b.textContent = watchVideo ? "Hide" : "▶ Watch";
  if (watchVideo) { box.appendChild(watchVideo); watchVideo.play(); }
}
