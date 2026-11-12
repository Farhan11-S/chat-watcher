// Things you place on the picture: overlay clips, text callouts and spotlights, each with
// a handle to drag and resize it in the player.
import { $, ASS_TO_CSS, S, subLayer, video } from "./state.js";
import { curFx, el, flash, hold, keptFx, ovPicture, overlayOf, r3, runsOf, wrapSub } from "./util.js";
import { camera, isShort, mapPoint, place, shortPanels, spotBox, viewBox } from "./preview.js";
import { buffer } from "./audio.js";
import { changed, editFx, pushUndo } from "./editing.js";

// The lines over the video, styled like the render (bold white or the voice's colour,
// black outline). Two at once stack like libass does: the later one further from the edge.
// Overlays: the transparent clip itself (VP9 with alpha) over the picture, kept in step with
// the playhead; placed like edit/overlays.py place() does for the render. The one you've
// picked shows even when the playhead isn't on it (dimmed, mid-way through), with a box you
// drag to move it, a corner to resize it, and the mouse wheel to resize it too.
const ovLayer = el("div", "ed-ovs");
subLayer.parentElement.insertBefore(ovLayer, subLayer);
const ovHandle = makeHandle(ovLayer);
const ovEls = new Map();
let ovDrag = null, ovWheelAt = 0;
function overlayAnchor(f, t) {     // where `at` puts its centre, before you moved it
  const b = S.D.regions[f.at];
  if (!b || f.at === "full" || f.at === "middle") return [0.5, 0.5];
  const c = [b.x + b.w / 2, b.y + b.h / 2];
  return isShort() ? (mapPoint(c[0], c[1], shortPanels(t)) || [0.5, 0.5]) : c;
}
function overlayCentre(f, t) {
  const [x, y] = overlayAnchor(f, t);
  return [x + (f.dx || 0), y + (f.dy || 0)];
}
function overlayEl(f) {
  let v = ovEls.get(f.id);
  if (!v || v.dataset.src !== f.overlay) {
    if (v) v.remove();
    v = document.createElement("video");
    Object.assign(v, { muted: true, playsInline: true, preload: "auto", src: `/media/overlay/${f.overlay}` });
    v.dataset.src = f.overlay;
    v.className = "ed-ov";
    v.addEventListener("loadeddata", () => { S.needsDraw = true; });    // now it can go to the right frame
    v.addEventListener("seeked", () => { S.needsDraw = true; });
    ovLayer.insertBefore(v, ovHandle);
    ovEls.set(f.id, v);
  }
  return v;
}
export function drawOverlays() {
  const t = video.currentTime, box = viewBox();
  const picked = curFx();
  const sel = picked && picked.type === "overlay" && ovPicture(picked) && overlayOf(picked.overlay) ? picked : null;
  const live = f => f.type === "overlay" && ovPicture(f) && overlayOf(f.overlay) && keptFx(f);
  const on = S.regionMode || !video.videoWidth ? []
    : S.fx.filter(f => live(f) && ((t >= f.t && t < f.t + f.d) || f === sel));
  const soon = S.regionMode || !video.videoWidth ? []     // loaded a moment early, so they start on time
    : S.fx.filter(f => live(f) && !on.includes(f) && f.t > t && f.t - t < 1.5);
  const ids = new Set([...on, ...soon].map(f => f.id));
  for (const [id, v] of ovEls) if (!ids.has(id)) { v.pause(); v.remove(); ovEls.delete(id); }
  ovLayer.hidden = !on.length;
  ovHandle.hidden = true;
  for (const f of soon) {
    const v = overlayEl(f);
    v.style.visibility = "hidden";
    if (!v.paused) v.pause();
    if (v.readyState > 0 && v.currentTime > 0.05) v.currentTime = 0;
  }
  if (!on.length) return;
  place(ovLayer, box);
  for (const f of on) {
    const ov = overlayOf(f.overlay), v = overlayEl(f);
    const inside = t >= f.t && t < f.t + f.d;
    const [cx, cy] = overlayCentre(f, t), w = (f.size || 1) * box.width, h = w / ov.aspect;
    const r = { left: cx * box.width - w / 2, top: cy * box.height - h / 2, width: w, height: h };
    Object.assign(v.style, { left: r.left + "px", top: r.top + "px", width: w + "px", height: h + "px",
                             visibility: "visible", opacity: inside ? "" : ".55" });
    const want = inside ? t - f.t : Math.min(ov.length, f.d) / 2;
    if (v.readyState > 0 && Math.abs(v.currentTime - want) > 0.12 && !v.seeking) v.currentTime = want;
    if (video.paused || !inside) { if (!v.paused) v.pause(); } else if (v.paused) v.play().catch(() => {});
    if (f === sel) {       // the handle: the part of it that's on the picture
      const l = Math.max(r.left, 0), tp = Math.max(r.top, 0);
      const rr = Math.min(r.left + w, box.width), b = Math.min(r.top + h, box.height);
      if (rr - l > 8 && b - tp > 8) {
        ovHandle.hidden = false;
        place(ovHandle, { left: l, top: tp, width: rr - l, height: b - tp });
      }
    }
  }
}
// Moving and resizing the picked overlay or text on the picture: a box over it (in its
// layer, which sits on the picture) to drag, a corner to drag, the mouse wheel.
const OV_SNAP = 0.02;          // its centre snaps to the middle of the picture within this
const movable = f => !!f && (f.type === "overlay" || f.type === "text" || f.type === "spot");
const camPoint = ([xc, yc, h], px, py) => [xc - h + px * 2 * h, yc - h + py * 2 * h];   // view → picture
function makeHandle(layer) {
  const h = el("div", "ed-ov-handle");
  h.title = "Drag to move · drag the corner or scroll to resize · double-click to centre it again";
  h.appendChild(el("i"));
  h.hidden = true;
  layer.appendChild(h);
  const frac = ev => {
    const r = layer.getBoundingClientRect();
    return [(ev.clientX - r.left) / r.width, (ev.clientY - r.top) / r.height, r];
  };
  h.addEventListener("pointerdown", ev => {
    const f = curFx();
    if (ev.button !== 0 || !movable(f)) return;
    ev.preventDefault();
    ev.stopPropagation();
    hold();
    h.setPointerCapture(ev.pointerId);
    const [px, py, r] = frac(ev), mode = ev.target.tagName === "I" ? "size" : "move";
    if (f.type === "spot") {                 // its box is in the clip's frame: drag in the picture, map back
      const cam = camera(video.currentTime), ob = spotBox(f, video.currentTime);
      if (!ob) return;
      pushUndo();
      ovDrag = { spot: true, mode, cam, ob, p0: camPoint(cam, px, py), x0: f.x, y0: f.y, w0: f.w, h0: f.h, moved: false };
      return;
    }
    const [cx, cy] = overlayCentre(f, video.currentTime);
    const dist = Math.hypot((px - cx) * r.width, (py - cy) * r.height);
    pushUndo();
    ovDrag = { mode, px, py, dx: f.dx || 0, dy: f.dy || 0,
               size: fxSize(f), dist: Math.max(dist, 4), cx, cy, moved: false };
  });
  h.addEventListener("pointermove", ev => {
    const f = curFx();
    if (!ovDrag || !movable(f)) return;
    const [px, py, r] = frac(ev), d = ovDrag;
    d.moved = true;
    if (d.spot) {
      const [qx, qy] = camPoint(d.cam, px, py), kx = d.ob[2] / d.w0, ky = d.ob[3] / d.h0;
      if (d.mode === "move") Object.assign(f, { x: r4(d.x0 + (qx - d.p0[0]) / kx), y: r4(d.y0 + (qy - d.p0[1]) / ky) });
      else {
        const lo = S.D.spot_info.min, w = Math.min(Math.max(2 * Math.abs(qx - (d.ob[0] + d.ob[2] / 2)) / kx, lo), 1.5);
        const h = Math.min(Math.max(2 * Math.abs(qy - (d.ob[1] + d.ob[3] / 2)) / ky, lo), 1.5);
        Object.assign(f, { w: r4(w), h: r4(h), x: r4(d.x0 + d.w0 / 2 - w / 2), y: r4(d.y0 + d.h0 / 2 - h / 2) });
      }
      f.by = "you";
      S.needsDraw = true;
      return;
    }
    if (d.mode === "move") {
      const [ax, ay] = overlayAnchor(f, video.currentTime);
      let x = ax + d.dx + px - d.px, y = ay + d.dy + py - d.py;
      if (Math.abs(x - 0.5) < OV_SNAP) x = 0.5;
      if (Math.abs(y - 0.5) < OV_SNAP) y = 0.5;
      const [lo, hi] = S.D.overlay_info.offset;
      f.dx = r4(Math.min(Math.max(x - ax, lo), hi));
      f.dy = r4(Math.min(Math.max(y - ay, lo), hi));
    } else {
      const dist = Math.hypot((px - d.cx) * r.width, (py - d.cy) * r.height);
      f.size = clampSize(f, d.size * dist / d.dist);
    }
    f.by = "you";
    S.needsDraw = true;
  });
  const end = () => {
    if (!ovDrag) return;
    const moved = ovDrag.moved;
    ovDrag = null;
    if (moved) changed(); else S.undo.pop();
  };
  h.addEventListener("pointerup", end);
  h.addEventListener("pointercancel", end);
  h.addEventListener("dblclick", ev => { ev.stopPropagation(); if (curFx() && curFx().type !== "spot") editFx({ dx: 0, dy: 0 }); });
  h.addEventListener("wheel", ev => {
    const f = curFx();
    if (!movable(f)) return;
    ev.preventDefault();
    if (performance.now() - ovWheelAt > 800) pushUndo();     // one undo step per burst of scrolling
    ovWheelAt = performance.now();
    const k = ev.deltaY < 0 ? 1.06 : 1 / 1.06;
    if (f.type === "spot") {
      const lo = S.D.spot_info.min, w = Math.min(Math.max(f.w * k, lo), 1.5), h = Math.min(Math.max(f.h * k, lo), 1.5);
      Object.assign(f, { x: r4(f.x + f.w / 2 - w / 2), y: r4(f.y + f.h / 2 - h / 2), w: r4(w), h: r4(h), by: "you" });
      changed();
      return;
    }
    f.size = clampSize(f, fxSize(f) * k);
    f.by = "you";
    changed();
  }, { passive: false });
  return h;
}
const r4 = v => Math.round(v * 1e4) / 1e4;
// Sizes: an overlay's width, a text's font size, both as shares of the picture's width.
export const sizeRange = f => (f.type === "text" ? S.D.text_info.size : S.D.overlay_info.size);
export const fxSize = f => f.size || (f.type === "text" ? S.D.text_info.default_size[f.style || "pop"] : 1);
export const clampSize = (f, v) => {
  const [lo, hi] = sizeRange(f), k = f.type === "text" ? 1000 : 100;
  return Math.round(Math.min(Math.max(v, lo), hi) * k) / k;
};

// Text callouts: drawn like edit/texts.py has libass draw them (the same size, outline,
// wrapping, tilt, and the spring/slam/fade in time), above the captions. The one you've
// picked shows even off its time (dimmed), with the same box as overlays to move it.
const txLayer = el("div", "ed-txs");
subLayer.parentElement.insertBefore(txLayer, subLayer.nextSibling);
const txHandle = makeHandle(txLayer);
const txEls = new Map();
export const txStyle = f => f.style || "pop";
const txLine = f => (txStyle(f) === "label" ? S.D.text_info.label_line : S.D.text_info.line);
const txShown = f => (txStyle(f) === "stamp" ? f.text.toUpperCase() : f.text);
const txColor = f => S.D.text_info.colors[f.color] || S.D.text_info.colors[S.D.text_info.default_color[txStyle(f)]];
function txAnim(f, t) {        // [scale, opacity]: the \t and \fad tags of edit/texts.py events()
  const I = S.D.text_info.intro_ms, u = (t - f.t) * 1000, left = (f.t + f.d - t) * 1000;
  const O = Math.min(S.D.text_info.outro_ms, Math.round(f.d * 400));
  let sc = 1, a = 1;
  if (txStyle(f) === "pop") {
    const h1 = Math.floor(I / 2);
    if (u < h1) sc = 0.4 + 0.78 * u / h1; else if (u < I) sc = 1.18 - 0.18 * (u - h1) / (I - h1);
  } else if (txStyle(f) === "stamp") {
    const k = Math.floor(I * 2 / 3);
    if (u < k) { sc = 2.6 - 1.6 * u / k; a = u / k; }
  } else if (u < I) a = u / I;
  if (O > 0 && left < O) a *= Math.max(0, left / O);
  return [sc, a];
}
function textEl(f, box) {
  const size = fxSize(f) * box.width, st = txStyle(f), col = txColor(f);
  const key = [f.text, st, col, Math.round(size * 10)].join("|");
  const rec = txEls.get(f.id);
  if (rec && rec.key === key) return rec.el;
  if (rec) rec.el.remove();
  const e = el("div", "ed-callout");
  Object.assign(e.style, {
    fontFamily: `"${S.D.sub_layout.font}", "Noto Sans CJK JP", "Noto Sans JP", system-ui, sans-serif`,
    fontSize: size * ASS_TO_CSS + "px", lineHeight: size + "px", color: "#" + col,
  });
  if (st === "label") Object.assign(e.style, { background: "rgba(0,0,0,.85)", padding: size * 0.28 + "px" });
  else Object.assign(e.style, { WebkitTextStroke: size * (st === "stamp" ? 0.28 : 0.22) + "px #000",
                                textShadow: `${size * 0.05}px ${size * 0.05}px 0 rgba(0,0,0,.5)` });
  const hi = "#" + (col.toUpperCase() === S.D.sub_layout.highlight.toUpperCase() ? "FFFFFF" : S.D.sub_layout.highlight);
  let on = false;
  wrapSub(txShown(f), txLine(f), 2).forEach((line, k) => {
    if (k) e.appendChild(el("br"));
    const [pieces, after] = runsOf(line, on);
    on = after;
    for (const [text, h] of pieces) {
      const sp = el("span", "", text);
      if (h) sp.style.color = hi;
      e.appendChild(sp);
    }
  });
  txLayer.insertBefore(e, txHandle);
  txEls.set(f.id, { el: e, key });
  return e;
}
export function drawTexts() {
  const t = video.currentTime, box = viewBox(), picked = curFx();
  const sel = picked && picked.type === "text" ? picked : null;
  const on = S.regionMode || !video.videoWidth ? []
    : S.fx.filter(f => f.type === "text" && f.text && keptFx(f) && ((t >= f.t && t < f.t + f.d) || f === sel));
  const ids = new Set(on.map(f => f.id));
  for (const [id, r] of txEls) if (!ids.has(id)) { r.el.remove(); txEls.delete(id); }
  txLayer.hidden = !on.length;
  txHandle.hidden = true;
  if (!on.length) return;
  place(txLayer, box);
  for (const f of on) {
    const e = textEl(f, box), inside = t >= f.t && t < f.t + f.d;
    const [cx, cy] = overlayCentre(f, t), [sc, a] = inside ? txAnim(f, t) : [1, 0.55];
    const tilt = txStyle(f) === "stamp" ? -S.D.text_info.tilt : 0;
    Object.assign(e.style, { left: cx * box.width + "px", top: cy * box.height + "px", opacity: a,
                             transform: `translate(-50%, -50%) rotate(${tilt}deg) scale(${sc})` });
    if (f === sel) {
      const w = e.offsetWidth, h = e.offsetHeight;
      place(txHandle, { left: cx * box.width - w / 2, top: cy * box.height - h / 2, width: w, height: h });
      txHandle.hidden = false;
    }
  }
}
export function addText() {
  const t = video.currentTime, st = "pop", [d] = S.D.visual.types.text;
  const f = { id: "u" + Math.random().toString(36).slice(2, 8), type: "text", t: r3(t), d: r3(Math.min(d, S.D.duration - t)),
              text: "TEXT", style: st, color: S.D.text_info.default_color[st], at: "middle", dx: 0, dy: 0,
              size: S.D.text_info.default_size[st], by: "you", why: "" };
  if (f.d < S.D.visual.types.text[1]) { flash("Too close to the end of the clip."); return; }
  pushUndo();
  S.fx.push(f);
  S.selFx = f.id;
  changed();
  const input = $("[data-text-input]");
  if (input) { input.focus(); input.select(); }
}

// Spotlights: the rest of the picture dimmed round an ellipse, a ring round it, drawn on
// the stage after the zoom like the render lays edit/spots.py's mask() before it. The box
// is in the clip's frame (x, y, w, h), mapped through the Short's panels (spotBox).
const spLayer = el("div", "ed-ovs");
subLayer.parentElement.insertBefore(spLayer, ovLayer);
const spHandle = makeHandle(spLayer);
spHandle.classList.add("round");
spHandle.title = "Drag to move · drag the corner to make it wider or taller · scroll to resize";
export function drawSpotHandle() {
  const f = curFx(), t = video.currentTime;
  const b = f && f.type === "spot" && keptFx(f) && !S.regionMode && video.videoWidth ? spotBox(f, t) : null;
  spLayer.hidden = !b;
  spHandle.hidden = !b;
  if (!b) return;
  const box = viewBox(), [xc, yc, h] = camera(t);
  place(spLayer, box);
  const l = (b[0] - (xc - h)) / (2 * h) * box.width, tp = (b[1] - (yc - h)) / (2 * h) * box.height;
  const r = l + b[2] / (2 * h) * box.width, bt = tp + b[3] / (2 * h) * box.height;
  const cl = Math.max(l, 0), ct = Math.max(tp, 0), cr = Math.min(r, box.width), cb = Math.min(bt, box.height);
  spHandle.classList.toggle("round", cl === l && ct === tp && cr === r && cb === bt);   // cut by the edge: a box
  if (cr - cl < 8 || cb - ct < 8) { spHandle.hidden = true; return; }                // so its corner stays on screen
  place(spHandle, { left: cl, top: ct, width: cr - cl, height: cb - ct });
}
export function addSpot() {
  const t = video.currentTime, [d] = S.D.visual.types.spot, st = "spotlight";
  let box;
  if (isShort()) {              // a circle 40% as wide as the Short, on its main (sharp) panel
    const p = shortPanels(t).filter(q => !q.blur).sort((a, b) => b.dst[2] * b.dst[3] - a.dst[2] * a.dst[3])[0];
    const [OW, OH] = S.D.layout_info.out, [sx, sy, sw, sh] = p.src, [, , dw, dh] = p.dst;
    const w = 0.4 * sw / dw, h = 0.4 * OW / OH * sh / dh;
    box = { x: sx + sw / 2 - w / 2, y: sy + sh / 2 - h / 2, w, h };
  } else {
    const h = 0.3, w = h * (video.videoHeight || 9) / (video.videoWidth || 16);
    box = { x: 0.5 - w / 2, y: 0.5 - h / 2, w, h };
  }
  const f = { id: "u" + Math.random().toString(36).slice(2, 8), type: "spot", t: r3(t), d: r3(Math.min(d, S.D.duration - t)),
              style: st, color: S.D.spot_info.default_color[st], ...Object.fromEntries(Object.entries(box).map(([k, v]) => [k, r4(v)])),
              by: "you", why: "" };
  if (f.d < S.D.visual.types.spot[1]) { flash("Too close to the end of the clip."); return; }
  pushUndo();
  S.fx.push(f);
  S.selFx = f.id;
  changed();
}
export function setSpotRegion(name) {   // round a drawn region, with some room
  const b = S.D.regions[name], pad = 0.15;
  if (!b) return;
  editFx({ x: r4(b.x - b.w * pad), y: r4(b.y - b.h * pad), w: r4(b.w * (1 + 2 * pad)), h: r4(b.h * (1 + 2 * pad)) });
}
export function ovSize(at, ov) {     // default_size in edit/overlays.py: "full" covers the picture
  if (at !== "full") return 0.5;
  const [W, H] = isShort() ? [9, 16] : [video.videoWidth || 16, video.videoHeight || 9];
  return Math.round(Math.min(Math.max(1, H / W * ov.aspect), 2.5) * 100) / 100;
}
let lastOverlay = "";
try { lastOverlay = localStorage.getItem("cw-ed-overlay") || ""; } catch (e) {}
export function addOverlay() {
  const list = S.D.overlays || [];
  if (!list.length) { flash("No overlays yet: add green-screen clips on the Library page (Overlays)."); return; }
  const ov = overlayOf(lastOverlay) || list[0], t = video.currentTime;
  const f = { id: "u" + Math.random().toString(36).slice(2, 8), type: "overlay", overlay: ov.id, t: r3(t),
              d: r3(Math.min(ov.length, S.D.duration - t)), at: "full", dx: 0, dy: 0, size: ovSize("full", ov), gain: 0,
              mode: "both", by: "you", why: "" };
  if (f.d < 0.1) { flash("Too close to the end of the clip."); return; }
  pushUndo();
  S.fx.push(f);
  S.selFx = f.id;
  if (ov.sound) buffer("ov:" + ov.id).catch(() => {});
  changed();
}
export function setOverlay(id) {
  const ov = overlayOf(id), f = curFx();
  if (!ov || !f) return;
  lastOverlay = id;
  try { localStorage.setItem("cw-ed-overlay", id); } catch (e) {}
  editFx({ overlay: id, d: r3(Math.min(ov.length, S.D.duration - f.t)), size: ovSize(f.at || "full", ov) });
  if (ov.sound) buffer("ov:" + id).catch(() => {});
}
