// The Short's layout panel, the Long / Short switch, and loading an edit into S (apply).
import { $, S, call, pk, root, video } from "./state.js";
import { btn, clone, cloneFx, curFx, el, hold, setSaved } from "./util.js";
import { isShort } from "./preview.js";
import { seek, stopEditPlay, toggle } from "./playback.js";
import { pollRender, save, showRender } from "./io.js";
import { changed, pushUndo } from "./editing.js";
import { clampSize } from "./overlays.js";
import { resize } from "./timeline.js";
import { showTab } from "./panels.js";
import { toggleRegions } from "./regions.js";

// ── the Short's layout panel, and the Long / Short switch ────────────────
export function paintLayout() {
  const box = $("[data-ed-layout]");
  const tab = $("[data-ed-tab-short]");
  tab.hidden = S.kind !== "short" || !S.layout;
  if (tab.hidden && tab.classList.contains("on")) showTab("sel");
  box.hidden = S.kind !== "short" || !S.layout;
  if (box.hidden) return;
  root.querySelectorAll("[data-lay-type]").forEach(b => b.classList.toggle("on", b.dataset.layType === S.layout.type));
  root.querySelectorAll("[data-lay-for]").forEach(r => { r.hidden = r.dataset.layFor !== S.layout.type; });
  for (const inp of box.querySelectorAll("[data-lay]")) {
    const k = inp.dataset.lay;
    if (inp.tagName === "SELECT") {
      const names = ["", ...Object.keys(S.D.regions)];
      if (S.layout[k] && !names.includes(S.layout[k])) names.push(S.layout[k]);
      const want = names.join("|") + "#" + Object.keys(S.D.regions).join("|");
      if (inp.dataset.names !== want) {
        inp.textContent = "";
        for (const n of names) {
          const o = el("option", "", n ? n + (S.D.regions[n] ? "" : " (not drawn)") : "whole frame");
          o.value = n;
          inp.appendChild(o);
        }
        inp.dataset.names = want;
      }
      inp.value = S.layout[k] || "";
    } else if (document.activeElement !== inp) inp.value = S.layout[k] ?? "";
  }
  const cen = $("[data-lay-centre]");
  cen.textContent = "";
  for (const n of Object.keys(S.D.regions)) cen.appendChild(btn("◎ " + n, `Centre the crop on the ${n} box`, { layCentre: n }));
  const tt = $("[data-lay-titles]");
  tt.textContent = "";
  (S.D.titles || []).forEach((t, i) => tt.appendChild(btn(`Claude's #${i + 1}`, t, { layTitle: i })));
  const hint = [];
  if (S.layout.type === "stack" && !Object.keys(S.D.regions).length)
    hint.push("Draw a face box (and a game box) with ⋯ More → ▢ Draw regions: until then each half shows the whole frame.");
  $("[data-lay-hint]").textContent = hint.join(" ");
}
export function setLayout(change) {
  pushUndo();
  Object.assign(S.layout, change);
  S.subKey = "";
  changed();
}
export function paintKind() {
  root.classList.toggle("ed-is-short", S.kind === "short");
  root.querySelectorAll("[data-ed-kind]").forEach(b => {
    b.classList.toggle("on", b.dataset.edKind === S.kind);
    b.setAttribute("aria-selected", b.dataset.edKind === S.kind ? "true" : "false");
  });
  $('[data-ed-act="render"]').textContent = S.kind === "short" ? "Render Short" : "Render edit";
  const other = S.D.other || {}, made = s => s === "done" || s === "stale";
  $("[data-ed-kind-note]").textContent = S.kind === "short"
    ? (made(other.long) ? "edit.mp4 is rendered too" : "")
    : (made(other.short) ? "short.mp4 is rendered too" : "");
}
export async function fetchEdit(k) {
  try {
    return await (await fetch(`/api/moment/${pk}/edit?kind=${k}`, { cache: "no-store" })).json();
  } catch (e) {
    return { ok: false, error: "Couldn't reach the dashboard. Is cw serve still running?" };
  }
}
// Take one edit's data (long or short) as the editor's state.
export function apply(d) {
  S.D = d;
  S.kind = d.kind || "long";
  S.segs = clone(d.segments);
  S.fx = cloneFx(d.effects || []);
  S.source = d.source;
  S.subs = cloneFx(d.subs || []);
  S.subStyle = { ...d.subs_style };
  S.subsMine = d.subs_source === "saved";
  S.layout = d.layout ? { ...d.layout } : null;
  S.undo = []; S.sel = 0; S.selFx = null; S.selSub = null; S.subKey = ""; S.listFor = null; S.dirty = false; S.layEditing = false;
  $('[data-ed-act="undo"]').disabled = true;
  paintKind();
}
export async function switchKind(k) {
  if (!S.D || k === S.kind) return;
  if (S.regionMode) toggleRegions(false);
  video.pause();
  stopEditPlay();
  clearTimeout(S.saveTimer);
  while (S.saving) await new Promise(r => setTimeout(r, 100));
  if (S.dirty) await save();
  if (S.dirty) return;                      // couldn't save: stay, the message says why
  const d = await fetchEdit(k);
  if (!d.ok) { setSaved(d.error || "Couldn't load the edit.", true); return; }
  try { localStorage.setItem("cw-ed-kind", k); } catch (e) {}
  apply(d);
  resize();
  call.paintAll();
  showRender(d.render);
  if (d.render.state === "queued" || d.render.state === "running") pollRender();
  setSaved(d.saved ? "Saved" : k === "short" ? "New Short from the whole clip, with your edit's effects and subtitles" : "");
  call.loadChat();
  if (S.segs[0]) seek(S.segs[0][0]);
  S.needsDraw = true;
}
video.parentElement.addEventListener("click", ev => {        // the Short's preview covers the video's controls
  if (isShort() && ev.target === video) { hold(); toggle(); }
});
root.addEventListener("input", ev => {
  const t = ev.target;
  if (t.matches("[data-vis-size]")) {          // live while you drag the slider; saved on release
    const f = curFx();
    if (!f) return;
    hold();
    if (!S.ovSizing) { pushUndo(); S.ovSizing = true; }
    f.size = clampSize(f, +t.value / 100);
    f.by = "you";
    const pct = t.parentElement.querySelector(".ed-ov-pct");
    if (pct) pct.textContent = Math.round(f.size * 100) + "%";
    S.needsDraw = true;
    return;
  }
  if (t.matches("[data-text-input]")) {        // live while you type; saved when you leave the box
    const f = curFx();
    if (!f || f.type !== "text") return;
    if (!S.txEditing) { pushUndo(); S.txEditing = true; }
    f.text = t.value.slice(0, S.D.text_info.max);
    f.by = "you";
    S.needsDraw = true;
    return;
  }
  if (!t.matches("[data-lay]") || t.tagName === "SELECT" || !S.layout) return;
  hold();
  if (!S.layEditing) { pushUndo(); S.layEditing = true; }
  S.layout[t.dataset.lay] = t.type === "range" ? +t.value : t.value;
  S.subKey = "";
  S.needsDraw = true;
});
