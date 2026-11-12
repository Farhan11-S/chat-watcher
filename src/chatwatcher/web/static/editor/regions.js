// Regions: boxes on the picture that zooms aim at, shared by every stream of the channel.
import { $, REGION_NAMES, S, ZOOMS, call, pk, regLayer, root, video } from "./state.js";
import { btn, el, flash, hold, libraryStatus, post, setSaved } from "./util.js";
import { drawStage, pictureBox, place } from "./preview.js";
import { paintButtons, stopEditPlay } from "./playback.js";
import { showRender } from "./io.js";
import { changed, pushUndo } from "./editing.js";

let regSel = null, regDrag = null, regTimer = null;

// ── regions: boxes on the picture that zooms aim at, shared by every stream of the channel ──
export function toggleRegions(onOff = !S.regionMode) {
  S.regionMode = onOff;
  root.classList.toggle("ed-regmode", S.regionMode);
  video.pause();
  stopEditPlay();
  regLayer.hidden = !S.regionMode;
  S.subKey = "";
  if (S.regionMode) {
    S.selFx = null;
    if (!regSel || !S.D.regions[regSel]) regSel = Object.keys(S.D.regions)[0] || null;
    paintRegions();
  }
  call.paintAll();
  drawStage();
}
export function paintRegions() {
  if (!S.regionMode) return;
  place(regLayer, pictureBox());
  regLayer.textContent = "";
  for (const [name, b] of Object.entries(S.D.regions)) {
    const r = el("div", "ed-region" + (name === regSel ? " on" : ""));
    r.dataset.region = name;
    Object.assign(r.style, { left: b.x * 100 + "%", top: b.y * 100 + "%", width: b.w * 100 + "%", height: b.h * 100 + "%" });
    r.append(el("span", "", name), el("i"));
    regLayer.appendChild(r);
  }
}
export function paintRegionPanel() {
  const box = $("[data-ed-selected]");
  box.textContent = "";
  const head = el("div", "ed-sel-head");
  head.append(el("b", "", "Regions"), ` · for every stream of ${S.D.region_scope}`);
  box.appendChild(head);
  box.appendChild(el("p", "ed-why", Object.keys(S.D.regions).length
    ? "Drag a box to move it, its corner to resize it, or drag on the picture for a new one. Zooms aim at the centre of their region."
    : "Drag on the picture to draw a box around her face. Zooms aim at it, in this clip and every later one from this channel."));
  for (const name of Object.keys(S.D.regions)) {
    const row = el("div", "ed-reg-row" + (name === regSel ? " on" : ""));
    const input = el("input");
    Object.assign(input, { type: "text", value: name, maxLength: 20 });
    input.dataset.regName = name;
    input.setAttribute("aria-label", "Region name");
    const used = S.fx.filter(f => ZOOMS.includes(f.type) && f.region === name).length;
    const del = btn("Delete", "Delete this region", { regDel: name });
    del.classList.add("danger");
    row.append(input, el("small", "muted", used ? `${used} zoom${used === 1 ? "" : "s"} in this edit` : ""), del);
    box.appendChild(row);
  }
  const acts = el("div", "actions ed-sel-acts");
  acts.appendChild(Object.assign(btn("Done", "Back to editing (Esc)", { edAct: "regions" }), { className: "small primary" }));
  box.appendChild(acts);
}
function saveRegions() {
  clearTimeout(regTimer);
  regTimer = setTimeout(async () => {
    try {
      const d = await post(`/moment/${pk}/regions`, { regions: Object.entries(S.D.regions).map(([name, b]) => ({ name, ...b })), kind: S.kind });
      S.D.regions = d.regions;
      setSaved("Regions saved");
      showRender(d.render);
      if ($("[data-ed-fx-status]").textContent.includes("Draw regions")) libraryStatus();   // drop the "draw her face" hint
      paintButtons();
    } catch (e) { setSaved("Couldn't save the regions: " + e.message, true); }
  }, 350);
}
export function renameRegion(old, raw) {
  const name = raw.trim().toLowerCase().replace(/\s+/g, " ");
  if (!name || name === old) { paintRegionPanel(); return; }
  if (!/^[a-z0-9][a-z0-9 _-]{0,19}$/.test(name)) { flash("Use letters, numbers, spaces, - or _ (up to 20)."); paintRegionPanel(); return; }
  if (S.D.regions[name]) { flash(`There's already a region called ${name}.`); paintRegionPanel(); return; }
  S.D.regions = Object.fromEntries(Object.entries(S.D.regions).map(([n, b]) => [n === old ? name : n, b]));
  if (S.fx.some(f => ZOOMS.includes(f.type) && f.region === old)) {        // this edit's zooms follow it
    pushUndo();
    S.fx.forEach(f => { if (ZOOMS.includes(f.type) && f.region === old) f.region = name; });
    changed();
  }
  regSel = name;
  saveRegions();
  paintRegions();
  call.paintAll();
}
export function deleteRegion(name) {
  delete S.D.regions[name];
  if (regSel === name) regSel = Object.keys(S.D.regions)[0] || null;
  saveRegions();
  paintRegions();
  call.paintAll();
  flash(`Deleted ${name}. Zooms that used it now aim at the centre.`);
}
const frac = ev => {
  const r = regLayer.getBoundingClientRect();
  return [Math.min(Math.max((ev.clientX - r.left) / r.width, 0), 1), Math.min(Math.max((ev.clientY - r.top) / r.height, 0), 1)];
};
regLayer.addEventListener("pointerdown", ev => {
  if (ev.button !== 0) return;
  hold();
  regLayer.setPointerCapture(ev.pointerId);
  const [px, py] = frac(ev), boxEl = ev.target.closest(".ed-region");
  if (boxEl) {
    const name = boxEl.dataset.region, b = S.D.regions[name];
    regSel = name;
    regDrag = { name, mode: ev.target.tagName === "I" ? "size" : "move", px, py, b0: { ...b } };
  } else {
    if (Object.keys(S.D.regions).length >= 8) { flash("At most 8 regions."); return; }
    let name = REGION_NAMES.find(n => !S.D.regions[n]);
    for (let k = 1; !name; k++) if (!S.D.regions["region " + k]) name = "region " + k;
    S.D.regions[name] = { x: px, y: py, w: 0, h: 0 };
    regSel = name;
    regDrag = { name, mode: "new", px, py };
  }
  paintRegions();
  paintRegionPanel();
});
regLayer.addEventListener("pointermove", ev => {
  if (!regDrag) return;
  const [px, py] = frac(ev), b = S.D.regions[regDrag.name], d = regDrag;
  if (!b) return;
  if (d.mode === "new") Object.assign(b, { x: Math.min(px, d.px), y: Math.min(py, d.py), w: Math.abs(px - d.px), h: Math.abs(py - d.py) });
  else if (d.mode === "move") {
    b.x = Math.min(Math.max(d.b0.x + px - d.px, 0), 1 - d.b0.w);
    b.y = Math.min(Math.max(d.b0.y + py - d.py, 0), 1 - d.b0.h);
  } else {
    b.w = Math.min(Math.max(d.b0.w + px - d.px, 0.03), 1 - d.b0.x);
    b.h = Math.min(Math.max(d.b0.h + py - d.py, 0.03), 1 - d.b0.y);
  }
  paintRegions();
});
function endRegionDrag() {
  if (!regDrag) return;
  const d = regDrag, b = S.D.regions[d.name];
  regDrag = null;
  if (d.mode === "new" && (!b || b.w < 0.03 || b.h < 0.03)) {      // a click, not a box
    delete S.D.regions[d.name];
    regSel = Object.keys(S.D.regions)[0] || null;
    paintRegions();
    paintRegionPanel();
    return;
  }
  for (const k of ["x", "y", "w", "h"]) b[k] = Math.round(b[k] * 10000) / 10000;
  saveRegions();
  paintRegions();
  call.paintAll();
}
regLayer.addEventListener("pointerup", endRegionDrag);
regLayer.addEventListener("pointercancel", endRegionDrag);
