// Pointer and keyboard: dragging on the timeline, every button and select, the shortcuts.
import {
  $, DRAG_SNAP_S, LINE_SNAP_S, MIN, S, SUB_MIN, SUB_SNAP_S, Y_CAM, Y_FX, Y_RULER, Y_SUBS, Y_VFX,
  call, canvas, root, scroller, ticked, video,
} from "./state.js";
import { curFx, curSub, hold, overlayOf, r3, snap, sound } from "./util.js";
import { fitZoom } from "./preview.js";
import { audio, playSound } from "./audio.js";
import { playEdit, playFromHere, seek, stopEditPlay, toggle } from "./playback.js";
import { render, toggleWatch } from "./io.js";
import {
  add, addFx, addSub, addVis, changed, del, deleteSub, doUndo, editFx, joinSub, move, nudge,
  polishSubs, previewFx, pushUndo, rebuildSubs, saveNames, selectSeg, selectSub, setEdge, setGain,
  setSubMode, setSubStyle, setSubText, setSubTime, setSubVoice, split, splitSub, startOver, subIndex,
  subsChanged,
} from "./editing.js";
import { addOverlay, addSpot, addText, ovSize, setOverlay, setSpotRegion, txStyle } from "./overlays.js";
import { T, X, gx, gy, hitFx, hitSeg, hitSubAt, hitVfx, resize } from "./timeline.js";
import { addSubAfter, giveTicked, lineEnd, linesToSubs, paintList, transcribe } from "./transcript.js";
import { addPerson, playVoice, setSegRegion, setTransition, setVoice, tagKey } from "./panels.js";
import { deleteRegion, renameRegion, toggleRegions } from "./regions.js";
import { setLayout, switchKind } from "./layout.js";
import { autoEdit, clearClaude, newChat } from "./ai.js";

canvas.addEventListener("pointerdown", ev => {
  if (!S.D || ev.button !== 0) return;
  hold();
  audio();
  const x = gx(ev), y = gy(ev);
  canvas.setPointerCapture(ev.pointerId);
  if (y >= Y_SUBS && y < Y_RULER) {                     // the subtitle lane
    const h = hitSubAt(x);
    if (h) {
      pushUndo();
      const k = subIndex(h.c.id);
      S.drag = { sub: h.c.id, edge: h.edge, moved: false, x0: x, dx: x - X(h.c.t0), len: h.c.t1 - h.c.t0,
               lo: S.subs[k - 1] ? S.subs[k - 1].t1 : 0, hi: S.subs[k + 1] ? S.subs[k + 1].t0 : S.D.duration };
      S.selSub = h.c.id;
      S.selFx = null;
      video.pause();
      stopEditPlay();
      call.paintAll();
      return;
    }
    if (S.selSub) { S.selSub = null; call.paintAll(); }
    S.drag = { scrub: true };
    seek(T(x));
    return;
  }
  if (y >= Y_CAM && y < Y_SUBS) {                       // the camera and effects lanes
    const h = hitVfx(x, y < Y_VFX ? "cam" : "vfx");
    if (h) {
      pushUndo();
      S.drag = { vis: h.f.id, edge: h.edge, moved: false, dx: x - X(h.f.t), x0: x };
      S.selFx = h.f.id;
      video.pause();
      stopEditPlay();
      call.paintAll();
      return;
    }
    if (S.selFx) { S.selFx = null; call.paintAll(); }
    S.drag = { scrub: true };
    seek(T(x));
    return;
  }
  if (y >= Y_FX && y < Y_CAM) {                         // the sound lane
    const f = hitFx(x);
    if (f) {
      pushUndo();
      S.drag = { fx: f.id, moved: false, dx: x - X(f.t), x0: x };
      S.selFx = f.id;
      video.pause();
      stopEditPlay();
      call.paintAll();
      return;
    }
    if (S.selFx) { S.selFx = null; call.paintAll(); }
    S.drag = { scrub: true };
    seek(T(x));
    return;
  }
  const h = hitSeg(x);
  if (h && h.edge !== undefined) {
    pushUndo();
    S.sel = h.i;
    S.selFx = null;
    S.drag = { i: h.i, edge: h.edge, moved: false, x0: x };
    video.pause();
    stopEditPlay();
    call.paintAll();
    return;
  }
  if (h && (h.i !== S.sel || S.selFx)) { S.sel = h.i; S.selFx = null; call.paintAll(); }
  S.drag = { scrub: true };
  seek(T(x));
});
canvas.addEventListener("pointermove", ev => {
  if (!S.D) return;
  const x = gx(ev), y = gy(ev);
  if (!S.drag) {
    if (y >= Y_SUBS && y < Y_RULER) {
      const h = hitSubAt(x);
      canvas.style.cursor = h ? (h.edge ? "ew-resize" : "grab") : "pointer";
      return;
    }
    if (y >= Y_CAM && y < Y_SUBS) {
      const h = hitVfx(x, y < Y_VFX ? "cam" : "vfx");
      canvas.style.cursor = h ? (h.edge ? "ew-resize" : "grab") : "pointer";
      return;
    }
    const overFx = y >= Y_FX && y < Y_CAM;
    const h = overFx ? hitFx(x) : hitSeg(x);
    canvas.style.cursor = overFx ? (h ? "grab" : "pointer") : (h && h.edge !== undefined ? "ew-resize" : "pointer");
    return;
  }
  if (S.drag.scrub) { seek(T(x)); return; }
  if (!S.drag.moved && Math.abs(x - S.drag.x0) < 3) return;   // a click, not a drag
  if (S.drag.sub) {
    const c = curSub();
    if (!c) return;
    if (S.drag.edge === "t0") c.t0 = Math.min(Math.max(T(x), S.drag.lo), c.t1 - SUB_MIN);
    else if (S.drag.edge === "t1") c.t1 = Math.min(Math.max(T(x), c.t0 + SUB_MIN), S.drag.hi);
    else {
      const t0 = Math.min(Math.max(T(x - S.drag.dx), S.drag.lo), S.drag.hi - S.drag.len);
      c.t0 = t0;
      c.t1 = t0 + S.drag.len;
    }
    S.drag.moved = true;
    S.subKey = "";
    if (!video.seeking) video.currentTime = S.drag.edge === "t1" ? Math.max(c.t0, c.t1 - 0.05) : c.t0;
    S.needsDraw = true;
    return;
  }
  if (S.drag.vis) {
    const f = curFx();
    if (!f) return;
    const [, shortest, longest] = S.D.visual.types[f.type];
    if (S.drag.edge === "end") f.d = Math.min(Math.max(T(x) - f.t, shortest), longest, S.D.duration - f.t);
    else f.t = Math.min(T(x - S.drag.dx), S.D.duration - shortest);
    S.drag.moved = true;
    if (!video.seeking) video.currentTime = S.drag.edge === "end" ? Math.max(f.t, f.t + f.d - 1 / (S.D.fps || 30)) : f.t;
    S.needsDraw = true;
    return;
  }
  if (S.drag.fx) {
    const f = curFx();
    if (!f) return;
    f.t = T(x - S.drag.dx);
    S.drag.moved = true;
    if (!video.seeking) video.currentTime = f.t;
    S.needsDraw = true;
    return;
  }
  const s = S.segs[S.drag.i];
  const t = T(x);
  s[S.drag.edge] = S.drag.edge === 0 ? Math.min(t, s[1] - MIN) : Math.max(t, s[0] + MIN);
  S.drag.moved = true;
  if (!video.seeking) video.currentTime = s[S.drag.edge];
  S.needsDraw = true;
});
function endDrag(ev) {
  if (!S.drag) return;
  const d = S.drag;
  S.drag = null;
  S.needsDraw = true;
  if (d.scrub) return;
  if (!d.moved) {
    S.undo.pop();
    $('[data-ed-act="undo"]').disabled = !S.undo.length;
    if (d.sub) selectSub(d.sub);                        // a click on a line: jump to it
    return;
  }
  if (d.sub) {
    const c = curSub();
    if (!c) return;
    if (S.snapOn && !ev.shiftKey && S.D.sub_words.length) {    // edges land on word starts and ends
      const starts = S.D.sub_words.map(w => [w[0], 0]), ends = S.D.sub_words.map(w => [w[1], 0]);
      if (d.edge !== "t1") {
        const t0 = Math.min(Math.max(snap(c.t0, SUB_SNAP_S, starts), d.lo), c.t1 - SUB_MIN);
        if (!d.edge) c.t1 += t0 - c.t0;
        c.t0 = t0;
      } else c.t1 = Math.min(Math.max(snap(c.t1, SUB_SNAP_S, ends), c.t0 + SUB_MIN), d.hi);
    }
    c.t0 = r3(c.t0);
    c.t1 = r3(Math.min(c.t1, S.D.duration));
    c.by = "you";
    seek(d.edge === "t1" ? Math.max(c.t0, c.t1 - 0.05) : c.t0);
    subsChanged();
    return;
  }
  if (d.vis) {
    const f = curFx();
    if (!f) return;
    if (S.snapOn && !ev.shiftKey && !d.edge && f.type !== "push") f.t = snap(f.t, DRAG_SNAP_S, S.D.hits);
    f.t = r3(Math.max(0, f.t));
    f.d = r3(Math.min(f.d, S.D.duration - f.t));
    f.by = "you";
    seek(f.t);
    changed();
    return;
  }
  if (d.fx) {
    const f = curFx();
    if (S.snapOn && !ev.shiftKey) f.t = snap(f.t, DRAG_SNAP_S, S.D.hits);
    f.t = r3(f.t);
    f.by = "you";
    seek(f.t);
    changed();
    return;
  }
  const s = S.segs[d.i];
  if (S.snapOn && !ev.shiftKey) {
    const v = snap(s[d.edge], DRAG_SNAP_S);
    if (d.edge === 0 ? v <= s[1] - MIN : v >= s[0] + MIN) s[d.edge] = v;
  }
  s[d.edge] = r3(s[d.edge]);
  seek(s[d.edge]);
  changed();
}
canvas.addEventListener("pointerup", endDrag);
canvas.addEventListener("pointercancel", endDrag);
scroller.addEventListener("scroll", () => { S.needsDraw = true; });
window.addEventListener("resize", resize);

root.addEventListener("click", ev => {
  const b = ev.target.closest("button, [data-line]");
  if (!b || !S.D) return;
  hold();
  audio();
  if (b.tagName === "BUTTON") b.blur();   // so Space/Enter go to the editor, not the button
  const ds = b.dataset;
  const nudgeVal = v => (v === "here" ? null : ev.shiftKey && ds.step ? Math.sign(+v) * +ds.step : +v);
  if (S.regionMode && !ds.regDel && ds.edAct !== "regions" && !ds.regName) toggleRegions(false);   // anything else: back to editing
  if (ds.transStyle !== undefined) setTransition(ds.transStyle);
  else if (ds.chipRegion !== undefined) {
    if (ds.chipFor === "seg") setSegRegion(ds.chipRegion);
    else if (ds.chipFor === "spot") setSpotRegion(ds.chipRegion);
    else if (ds.chipFor === "capat") editFx({ at: ds.chipRegion });
    else if (ds.chipFor === "ovat") {
      const f = curFx(), ov = f && overlayOf(f.overlay);
      if (f) editFx(ov && (ds.chipRegion === "full") !== (f.at === "full")
        ? { at: ds.chipRegion, size: ovSize(ds.chipRegion, ov), dx: 0, dy: 0 } : { at: ds.chipRegion, dx: 0, dy: 0 });
    } else {
      const f = curFx(), change = { region: ds.chipRegion };
      if (f && f.type === "punch" && ds.chipRegion && S.D.regions[ds.chipRegion]) change.zoom = Math.min(fitZoom(ds.chipRegion), 2.5);
      if (f) editFx(change);
    }
  } else if (ds.visRecentre !== undefined) editFx({ dx: 0, dy: 0 });
  else if (ds.edKind) switchKind(ds.edKind);
  else if (ds.layType) { if (S.layout && S.layout.type !== ds.layType) setLayout({ type: ds.layType }); }
  else if (ds.layCentre) {
    const b = S.D.regions[ds.layCentre];
    if (b && S.layout) setLayout({ type: "crop", x: Math.round((b.x + b.w / 2) * 1e4) / 1e4, y: Math.round((b.y + b.h / 2) * 1e4) / 1e4 });
  } else if (ds.layTitle !== undefined) { if (S.layout) setLayout({ title: S.D.titles[+ds.layTitle] || "" }); }
  else if (ds.edAdd) { if (S.regionMode) toggleRegions(false); addVis(ds.edAdd); }
  else if (ds.subGo) selectSub(ds.subGo);
  else if (ds.subStart || ds.subEnd) {
    const c = S.subs.find(x => x.id === (ds.subStart || ds.subEnd));
    if (c) setEdge(ds.subStart ? 0 : 1, ds.subStart ? c.t0 : c.t1);
  } else if (ds.subAfter) addSubAfter(ds.subAfter);
  else if (ds.subSplit) splitSub(ds.subSplit);
  else if (ds.subJoin) joinSub(ds.subJoin);
  else if (ds.subDel) deleteSub(ds.subDel);
  else if (ds.voicePlay !== undefined) playVoice(+ds.voicePlay);
  else if (ds.voiceAdd !== undefined) addPerson();
  else if (ds.bulkVoice !== undefined) { b.blur(); giveTicked(ds.bulkVoice === "" ? null : +ds.bulkVoice); }
  else if (ds.bulkAll !== undefined) { b.blur(); S.subs.forEach(c => ticked.add(c.id)); S.listFor = null; paintList(); }
  else if (ds.bulkClear !== undefined) { b.blur(); ticked.clear(); S.listFor = null; paintList(); }
  else if (ds.subNudge0 !== undefined || ds.subNudge1 !== undefined) {
    const edge = ds.subNudge0 !== undefined ? "t0" : "t1", v = nudgeVal(edge === "t0" ? ds.subNudge0 : ds.subNudge1), c = curSub();
    if (c) setSubTime(c.id, edge, v === null ? video.currentTime : c[edge] + v);
  }
  else if (ds.regDel) deleteRegion(ds.regDel);
  else if (ds.visFit !== undefined) { const f = curFx(); if (f) editFx({ zoom: fitZoom(f.region) }); }
  else if (ds.seg !== undefined) selectSeg(+ds.seg);
  else if (ds.nudge0 !== undefined || ds.nudge1 !== undefined) {
    const edge = ds.nudge0 !== undefined ? 0 : 1, v = nudgeVal(edge ? ds.nudge1 : ds.nudge0);
    v === null ? setEdge(edge, video.currentTime) : nudge(edge, v);
  } else if (ds.fxNudge !== undefined) {
    const f = curFx(), v = nudgeVal(ds.fxNudge);
    if (f) { const t = r3(Math.min(Math.max(0, v === null ? video.currentTime : f.t + v), S.D.duration)); editFx({ t }); seek(t); }
  } else if (ds.goT !== undefined) seek(+ds.goT);
  else if (ds.fxHear !== undefined) { const f = curFx(); if (f) playSound(f.sound, f.gain); }
  else if (ds.lineStart !== undefined) {
    const t = S.D.lines[+ds.lineStart].t;
    setEdge(0, S.snapOn ? snap(t, LINE_SNAP_S) : t);
  } else if (ds.lineEnd !== undefined) {
    const t = lineEnd(+ds.lineEnd);
    setEdge(1, S.snapOn ? snap(t, LINE_SNAP_S) : t);
  } else if (ds.line !== undefined) seek(S.D.lines[+ds.line].t);
  else if (ds.edAct) {
    ({
      "play-edit": () => (S.editPlay && !S.editPlay.only ? (video.pause(), stopEditPlay())
                                                       : ev.shiftKey ? playEdit(0) : playFromHere()),
      toggle, add, split, undo: doUndo, render, watch: toggleWatch,
      "play-seg": () => playEdit(S.sel, true),
      up: () => move(-1), down: () => move(1), delete: del,
      "add-fx": addFx, "add-overlay": addOverlay, "add-text": addText, "add-spot": addSpot, autoedit: autoEdit, "clear-claude": clearClaude, whole: startOver, "chat-new": newChat,
      "fx-play": previewFx, regions: () => toggleRegions(), transcribe,
      "sub-add": addSub, "sub-rebuild": rebuildSubs, "lines-edit": linesToSubs, "sub-polish": polishSubs, "names-save": saveNames,
    })[ds.edAct]?.();
  }
});
root.addEventListener("change", ev => {
  const t = ev.target;
  if (t.matches("[data-lay]") && S.layout) {
    if (t.tagName === "SELECT") { setLayout({ [t.dataset.lay]: t.value }); return; }
    S.layout[t.dataset.lay] = t.type === "range" ? +t.value : t.value;
    if (!S.layEditing) pushUndo();
    S.layEditing = false;
    changed();
    return;
  }
  if (t.matches("[data-gain]")) setGain(+t.value);
  else if (t.matches("[data-seg-region]")) setSegRegion(t.value);
  else if (t.matches("[data-fx-sound]")) editFx({ sound: t.value, gain: (sound(t.value) || {}).gain || 0 });
  else if (t.matches("[data-fx-gain]")) editFx({ gain: +t.value });
  else if (t.matches("[data-fx-duck]")) editFx({ duck: +t.value });
  else if (t.matches("[data-vis-d]")) editFx({ d: Math.min(+t.value, S.D.duration - curFx().t) });
  else if (t.matches("[data-vis-region]")) {
    const f = curFx(), change = { region: t.value };
    if (f.type === "punch" && t.value && S.D.regions[t.value]) change.zoom = Math.min(fitZoom(t.value), 2.5);
    editFx(change);
  } else if (t.matches("[data-vis-zoom]")) editFx({ zoom: +t.value });
  else if (t.matches("[data-vis-strength]")) editFx({ strength: +t.value });
  else if (t.matches("[data-vis-color]")) editFx({ color: t.value });
  else if (t.matches("[data-vis-overlay]")) setOverlay(t.value);
  else if (t.matches("[data-vis-size]")) { S.ovSizing = false; changed(); }
  else if (t.matches("[data-vis-mode]")) editFx({ mode: t.value });
  else if (t.matches("[data-text-input]")) {
    const f = curFx();
    S.txEditing = false;
    if (f && !f.text.trim()) { f.text = "TEXT"; t.value = f.text; }
    changed();
  } else if (t.matches("[data-text-style]")) {
    const f = curFx(), was = txStyle(f), TI = S.D.text_info, change = { style: t.value };
    if ((f.color || TI.default_color[was]) === TI.default_color[was]) change.color = TI.default_color[t.value];
    if (!f.size || f.size === TI.default_size[was]) change.size = TI.default_size[t.value];
    editFx(change);
  } else if (t.matches("[data-text-color]")) editFx({ color: t.value });
  else if (t.matches("[data-cap-fx]")) editFx({ fx: t.value });
  else if (t.matches("[data-cap-font]")) editFx({ font: t.value });
  else if (t.matches("[data-cap-size]")) editFx({ size: t.value });
  else if (t.matches("[data-spot-style]")) {
    const f = curFx(), S = S.D.spot_info, was = f.style || "spotlight", change = { style: t.value };
    if ((f.color || S.default_color[was]) === S.default_color[was]) change.color = S.default_color[t.value];
    editFx(change);
  } else if (t.matches("[data-spot-color]")) editFx({ color: t.value });
  else if (t.matches("[data-vis-gain]")) editFx({ gain: +t.value });
  else if (t.matches("[data-reg-name]")) renameRegion(t.dataset.regName, t.value);
  else if (t.matches("[data-sub-input]")) setSubText(t.dataset.subInput, t.value);
  else if (t.matches("[data-sub-voice]")) setSubVoice(t.dataset.subVoice, t.value === "" ? null : +t.value);
  else if (t.matches("[data-voice-name]")) setVoice(+t.dataset.voiceName, { name: t.value.trim() });
  else if (t.matches("[data-voice-color]")) setVoice(+t.dataset.voiceColor, { color: t.value.replace("#", "").toUpperCase() });
  else if (t.matches("[data-sub-on]")) setSubStyle({ on: t.checked });
  else if (t.matches("[data-sub-size]")) setSubStyle({ size: t.value });
  else if (t.matches("[data-sub-pos]")) setSubStyle({ position: t.value });
  else if (t.matches("[data-sub-look]")) setSubStyle({ look: t.value });
  else if (t.matches("[data-sub-mode]")) setSubMode(t.value);
  else if (t.matches("[data-ed-snap]")) {
    S.snapOn = t.checked;
    try { localStorage.setItem("cw-ed-snap", S.snapOn ? "1" : "0"); } catch (e) {}
  } else if (t.matches("[data-ed-zoom]")) setZoom(+t.value);
});
function setZoom(z) {
  const w = scroller.clientWidth, centre = T(scroller.scrollLeft + w / 2);
  S.zoom = z;
  $("[data-ed-zoom]").value = String(z);
  resize();
  scroller.scrollLeft = X(centre) - w / 2;
}

document.addEventListener("keydown", ev => {
  if (!S.D || ev.altKey || ev.isComposing) return;
  const a = document.activeElement;
  if (a && (["INPUT", "SELECT", "TEXTAREA"].includes(a.tagName) || a.isContentEditable)) return;
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "z") { doUndo(); ev.preventDefault(); return; }
  if (ev.ctrlKey || ev.metaKey) return;
  const r = root.getBoundingClientRect();
  if (r.bottom < 0 || r.top > window.innerHeight) return;   // only while the editor is on screen
  if (S.regionMode) {
    if (ev.key === "Escape") { ev.preventDefault(); toggleRegions(false); }
    return;
  }
  const f = 1 / (S.D.fps || 30);
  const keys = {
    " ": toggle,
    ArrowLeft: () => seek(video.currentTime - (ev.shiftKey ? 1 : 0.1)),
    ArrowRight: () => seek(video.currentTime + (ev.shiftKey ? 1 : 0.1)),
    ",": () => { video.pause(); seek(video.currentTime - f); },
    ".": () => { video.pause(); seek(video.currentTime + f); },
    i: () => setEdge(0, video.currentTime), o: () => setEdge(1, video.currentTime),
    s: split, p: () => playEdit(S.sel, true), f: addFx, z: () => addVis("punch"), x: () => addVis("shake"), o: addOverlay, t: addText, c: addSpot,
    "[": () => selectSeg(S.sel - 1), "]": () => selectSeg(S.sel + 1),
    Delete: del, Backspace: del,
    Enter: () => (S.editPlay ? (video.pause(), stopEditPlay()) : ev.shiftKey ? playEdit(0) : playFromHere()),
    Escape: () => {
      if (ticked.size) { ticked.clear(); S.listFor = null; paintList(); return; }
      if (S.selFx || S.selSub) { S.selFx = null; S.selSub = null; call.paintAll(); }
    },
    "+": () => setZoom(Math.min(16, S.zoom * 2)), "=": () => setZoom(Math.min(16, S.zoom * 2)),
    "-": () => setZoom(Math.max(1, S.zoom / 2)),
  };
  for (let n = 0; n <= 9; n++) keys[String(n)] = () => tagKey(n);
  const fn = keys[ev.key] || keys[ev.key.toLowerCase()];
  if (!fn) return;
  ev.preventDefault();
  ev.stopPropagation();
  hold();
  audio();
  fn();
}, true);
