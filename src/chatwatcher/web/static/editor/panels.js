// The side panel: its tabs and menus, and the Selected panel for a segment, a sound, a
// picture effect or a subtitle line; the people (voices) panel and the subtitle bar.
import { $, DUCKS, GAINS, HEIGHT, LANES, LANG, LENGTHS, S, VLABEL, ZOOMS, ZOOM_STEPS, call, pk, root, video } from "./state.js";
import {
  btn, curFx, curSub, cutInto, el, flash, fmt, isVis, kept, keptFx, nice, ovPicture, ovSound,
  overlayOf, partLen, post, r3, select, setSpeaker, sound, subStatus, subWidth, timeParts, total, track, transOf,
} from "./util.js";
import { musicBed } from "./audio.js";
import { FULL, fitZoom, isShort, spotBox } from "./preview.js";
import { playEdit, seek, stopEditPlay } from "./playback.js";
import { CAPI, V, cueArts, subsAt, voiceColor, voiceName, voiceText } from "./captions.js";
import { artCue, artWords, changed, pushUndo, selectSub, subIndex, subsChanged } from "./editing.js";
import { fxSize, sizeRange, txStyle } from "./overlays.js";
import { regionCol } from "./timeline.js";
import { giveTicked } from "./transcript.js";

// ── the side panel's tabs, the lane labels, the ⋯ menus ──────────────────
export function showTab(name) {
  root.querySelectorAll("[data-ed-tab]").forEach(b => {
    b.classList.toggle("on", b.dataset.edTab === name);
    b.setAttribute("aria-selected", b.dataset.edTab === name ? "true" : "false");
  });
  root.querySelectorAll("[data-ed-pane]").forEach(p => { p.hidden = p.dataset.edPane !== name; });
}
export function paintLanes() {
  const box = $("[data-ed-lanes]");
  if (box.childElementCount) return;
  for (const [name, top, h] of LANES) {
    const l = el("span", "ed-lane", name);
    l.style.top = top + "px";
    l.style.height = h + "px";
    box.appendChild(l);
  }
  box.style.height = HEIGHT + "px";
}
let picked = "";   // what the Selected tab shows; the tab opens when that changes
export function followSelection() {
  const now = S.regionMode ? "regions" : S.selSub ? "sub:" + S.selSub : S.selFx ? "fx:" + S.selFx : "seg:" + S.sel;
  if (picked && now !== picked) showTab("sel");
  picked = now;
}
root.addEventListener("click", ev => {
  const tab = ev.target.closest("[data-ed-tab]");
  if (tab) { showTab(tab.dataset.edTab); return; }
  // A pick inside a ⋯ menu closes it; a click outside closes any that is open.
  for (const m of root.querySelectorAll("details[data-ed-menu][open], details.ed-help[open]"))
    if (!m.contains(ev.target) || ev.target.closest(".ed-menu-list button")) m.open = false;
}, true);
export function paintTotal() {
  const n = S.segs.length;
  let text = `Your edit: ${fmt(total(), 1)} in ${n} segment${n === 1 ? "" : "s"}`;
  const sounds = S.fx.filter(f => f.type === "sfx" && kept(f.t)).length, pics = S.fx.filter(f => isVis(f) && keptFx(f)).length;
  if (sounds) text += ` · ${sounds} sound${sounds === 1 ? "" : "s"}`;
  if (pics) text += ` · ${pics} zoom/shake/flash`;
  if (S.subs.length && S.subStyle.on) text += ` · ${S.subs.length} subtitle line${S.subs.length === 1 ? "" : "s"}`;
  $("[data-ed-total]").textContent = text;
}
export function paintChips() {
  const box = $("[data-ed-chips]");
  box.textContent = "";
  S.segs.forEach((s, i) => {
    const b = btn("", "Select segment " + (i + 1), { seg: i });
    b.className = "chip ed-chip" + (i === S.sel && !S.selFx ? " current" : "");
    b.append(el("b", "", String(i + 1)), " " + fmt(s[0], 1) + "–" + fmt(s[1], 1) + " ", el("span", "", (s[1] - s[0]).toFixed(1) + "s"));
    if (s[2]) b.append(" ", el("span", "ed-gain", (s[2] > 0 ? "+" : "") + s[2] + " dB"));
    box.appendChild(b);
  });
}
function edgeRow(label, value, data, title, frame) {
  const row = el("div", "ed-edge");
  row.append(el("span", "ed-lbl", label));
  row.appendChild(btn("‹", "One frame earlier (hold Shift: 0.1 s)", { [data]: -frame, step: 0.1 }));
  row.appendChild(Object.assign(btn(fmt(value), "Jump here", { goT: value }), { className: "small mono ed-val" }));
  row.appendChild(btn("›", "One frame later (hold Shift: 0.1 s)", { [data]: frame, step: 0.1 }));
  row.appendChild(btn("to playhead", title, { [data]: "here" }));
  return row;
}
export function paintSelected() {
  const box = $("[data-ed-selected]");
  box.textContent = "";
  const s = S.segs[S.sel];
  if (!s) return;
  const head = el("div", "ed-sel-head");
  head.append(el("b", "", `Segment ${S.sel + 1}`), ` of ${S.segs.length} · ${(s[1] - s[0]).toFixed(2)} s`);
  box.appendChild(head);
  const f = 1 / (S.D.fps || 30);
  box.appendChild(edgeRow("Start", s[0], "nudge0", "Start at the playhead (I)", f));
  box.appendChild(edgeRow("End", s[1], "nudge1", "End at the playhead (O)", f));
  const acts = el("div", "actions ed-sel-acts");
  const gain = select(GAINS.map(g => [g, (g > 0 ? "+" : "") + g + " dB" + (g === 0 ? " (as is)" : "")]), s[2] || 0,
                      { gain: "" }, "Volume of this segment");
  const gl = el("label", "muted");
  gl.append(el("small", "", "volume "), gain);
  if (!cutInto(S.sel)) {
    box.appendChild(el("small", "muted", "Into this segment"));
    box.appendChild(el("small", "faint", S.sel === 0
      ? "This is where the edit starts, so there's no cut before it to make a transition on. Pick segment 2 or later."
      : `It plays straight on from segment ${S.sel} (no cut), so it can't have a transition.`));
  } else {
    const tr = transOf(s);
    box.appendChild(el("small", "muted", "Into this segment"));
    const row = el("div", "ed-region-chips");
    for (const [style, label, tip] of [["", "cut", "A straight cut"]].concat(
      Object.entries(S.D.visual.transitions).map(([k, [, desc]]) => [k, k, desc]))) {
      const b = btn(label, tip, { transStyle: style });
      b.className = "chip ed-rchip" + ((tr ? tr.style : "") === style ? " current" : "");
      row.appendChild(b);
    }
    box.appendChild(row);
  }
  const shown = segShots(s)[0];
  box.appendChild(el("small", "muted", "On screen in this segment"));
  box.appendChild(regionChips(shown ? shown.region : "", { chipFor: "seg" }, S.kind === "short" ? "the layout" : "whole frame",
                              S.kind === "short" ? [[FULL, "whole picture (fit + blur)"]] : []));
  acts.append(gl, btn("▶ Play", "Play this segment (P)", { edAct: "play-seg" }),
    btn("↑ Earlier", "Play this segment earlier in the edit", { edAct: "up" }),
    btn("↓ Later", "Play this segment later in the edit", { edAct: "down" }));
  const d = btn("Delete", "Delete this segment (Del)", { edAct: "delete" });
  d.classList.add("danger");
  acts.appendChild(d);
  box.appendChild(acts);
}
// "show" on a segment: frame shots (Short) or fill zooms (long) covering exactly it,
// marked by their why, so choosing again replaces them.
const SEG_WHY = "segment shows ";
const segShotType = () => (S.kind === "short" ? "frame" : "punch");
function segShots(s) {      // in a Short every frame shot on the segment (yours or Claude's); long: your fill zooms
  return S.fx.filter(f => f.type === segShotType() && (S.kind === "short" || (f.why || "").startsWith(SEG_WHY))
                        && f.t < s[1] - 0.05 && f.t + f.d > s[0] + 0.05);
}
export function setTransition(style) {
  const s = S.segs[S.sel];
  if (!s || !cutInto(S.sel)) return;
  pushUndo();
  S.fx = S.fx.filter(f => !(f.type === "transition" && Math.abs(f.t - s[0]) < 0.05));
  if (style) S.fx.push({ id: "u" + Math.random().toString(36).slice(2, 8), type: "transition", t: r3(s[0]), style,
                       d: S.D.visual.transitions[style][0], by: "you", why: "" });
  changed();
  if (style) playEdit(S.sel - 1, false, Math.max(S.segs[S.sel - 1][0], S.segs[S.sel - 1][1] - 1.2));   // show it
}
export function setSegRegion(name) {
  const s = S.segs[S.sel];
  if (!s) return;
  pushUndo();
  const old = new Set(segShots(s));
  S.fx = S.fx.filter(f => !old.has(f));
  if (name) {
    const type = segShotType(), longest = S.D.visual.types[type][2];
    for (let t = s[0]; t < s[1] - 0.05; t += longest) {        // longer than one shot may last: a chain of them
      const f = { id: "u" + Math.random().toString(36).slice(2, 8), type, t: r3(t), d: r3(Math.min(longest, s[1] - t)),
                  region: name, by: "you", why: SEG_WHY + name };
      if (type === "punch") f.zoom = Math.min(fitZoom(name), 3);
      S.fx.push(f);
    }
  }
  S.selFx = null;
  changed();
}
function regionChips(current, data, none, extra = []) {
  const box = el("div", "ed-region-chips");
  const opts = (none ? [["", none]] : []).concat(extra, Object.keys(S.D.regions).map(n => [n, n]));
  for (const [val, text] of opts) {
    const b = btn(text, val ? `Show ${val}` : none, { ...data, chipRegion: val });
    b.className = "chip ed-rchip" + (String(current || "") === val ? " current" : "");
    if (val) b.style.setProperty("--rc", regionCol(val));
    box.appendChild(b);
  }
  if (!Object.keys(S.D.regions).length)
    box.appendChild(btn("▢ Draw regions first", "Draw the boxes the camera can show, once per channel", { edAct: "regions" }));
  return box;
}
export function paintFxPanel() {
  const box = $("[data-ed-selected]");
  box.textContent = "";
  const f = curFx(), snd = sound(f.sound);
  const head = el("div", "ed-sel-head");
  head.append(el("b", "", "Sound effect"), f.by === "claude" ? " · placed by Claude" : " · yours");
  if (!kept(f.t)) head.append(el("span", "ed-warn", " · outside your cut, so it won't play"));
  box.appendChild(head);
  if (f.why) box.appendChild(el("p", "ed-why", f.why));
  const pick = el("div", "actions");
  const options = S.D.sounds.map(s => [s.id, s.name + " · " + s.length.toFixed(1) + " s"]);
  if (!snd) options.unshift([f.sound, f.sound + " (missing)"]);
  pick.append(select(options, f.sound, { fxSound: "" }, "Sound"),
              btn("▶ Hear it", "Play just this sound", { fxHear: "" }));
  box.appendChild(pick);
  if (snd && snd.description) box.appendChild(el("p", "faint ed-why", snd.description));
  box.appendChild(edgeRow("At", f.t, "fxNudge", "Move it to the playhead", 1 / (S.D.fps || 30)));
  const acts = el("div", "actions ed-sel-acts");
  const vol = el("label", "muted");
  vol.append(el("small", "", "volume "), select(GAINS.map(g => [g, (g > 0 ? "+" : "") + g + " dB"]), f.gain, { fxGain: "" }, "Volume of the sound"));
  const duck = el("label", "muted");
  duck.title = "Lower the clip's sound while this plays, so it cuts through";
  duck.append(el("small", "", "her voice under it "),
              select(DUCKS.map(g => [g, g ? g + " dB" : "as is"]), f.duck, { fxDuck: "" }, "Lower her voice under the sound"));
  const d = btn("Delete", "Delete this sound (Del)", { edAct: "delete" });
  d.classList.add("danger");
  acts.append(vol, duck, d);
  box.appendChild(acts);
}
// The music bed: which track, where its drop lands (the marker), how much of it plays
// before the drop, its volume and how far it dips under her words.
export function paintMusicPanel() {
  const box = $("[data-ed-selected]");
  box.textContent = "";
  const f = curFx(), tr = track(f.track), MI = S.D.music_info;
  const head = el("div", "ed-sel-head");
  head.append(el("b", "", "Music"), f.by === "claude" ? " · placed by Claude" : " · yours");
  if (!kept(f.t)) head.append(el("span", "ed-warn", " · its drop is outside your cut, so it won't play"));
  box.appendChild(head);
  if (f.why) box.appendChild(el("p", "ed-why", f.why));
  const pick = el("div", "actions");
  const options = (S.D.music || []).map(m => [m.id, m.name + (m.mood ? " · " + m.mood : "") + " · " + Math.round(m.bpm) + " BPM"]);
  if (!tr) options.unshift([f.track, f.track + " (missing)"]);
  pick.append(select(options, f.track, { muTrack: "" }, "Track"));
  box.appendChild(pick);
  if (tr && tr.description) box.appendChild(el("p", "faint ed-why", tr.description));
  box.appendChild(edgeRow("Drop", f.t, "fxNudge", "Land the drop on the playhead", 1 / (S.D.fps || 30)));
  const acts = el("div", "actions ed-sel-acts");
  const labelled = (text, control, title) => {
    const l = el("label", "muted");
    if (title) l.title = title;
    l.append(el("small", "", text + " "), control);
    return l;
  };
  acts.appendChild(labelled("starts", select([[-1, "with the edit"], [0, "on the drop"], [1, "1 s before it"], [2, "2 s before"],
                                              [4, "4 s before"], [8, "8 s before"], [16, "16 s before"]],
                                             f.lead ?? -1, { muLead: "" }, "How much music plays before the drop",
                                             v => nice(v) + " s before"),
                                      "With the edit: the track plays from the start of the edit (as much of it as there is before its drop). On the drop: silence, then the drop hits."));
  acts.appendChild(labelled("volume", select([3, 0, -3, -6, -10, -15, -20].map(g => [g, (g > 0 ? "+" : "") + g + " dB"]),
                                             f.gain ?? MI.default_gain, { muGain: "" }, "Music volume"),
                            "0 = as loud as her talking. Hype edits sit near 0, a soft bed at -10 or lower."));
  acts.appendChild(labelled("under her words", select([[0, "no dip"], [-6, "-6 dB"], [-10, "-10 dB"], [-14, "-14 dB"], [-20, "-20 dB"]],
                                                      f.duck ?? MI.default_duck, { muDuck: "" }, "How far the music dips while she talks")));
  box.appendChild(acts);
  const b = musicBed();
  if (b) box.appendChild(el("p", "faint ed-why",
    `Plays ${fmt(b.start, 1)}–${fmt(b.start + b.length, 1)} of the edit, from ${fmt(b.offset, 1)} into the track; the drop (${fmt(tr.drop, 1)} in the track) hits at ${fmt(b.drop, 1)}.`
    + (b.fadeOut ? " It fades out at the end." : " The track ends before the edit does.")));
  const more = el("div", "actions ed-sel-acts");
  const d = btn("Delete", "Remove the music (Del)", { edAct: "delete" });
  d.classList.add("danger");
  more.append(btn("▶ Play edit", "Hear it with the cut (music plays only in ▶ Play edit)", { edAct: "play-edit" }), d);
  box.appendChild(more);
}
export function paintVisPanel() {
  const box = $("[data-ed-selected]");
  box.textContent = "";
  const f = curFx();
  const head = el("div", "ed-sel-head");
  head.append(el("b", "", VLABEL[f.type]), f.by === "claude" ? " · placed by Claude" : " · yours");
  if (!keptFx(f)) head.append(el("span", "ed-warn", " · outside your cut, so it won't show"));
  box.appendChild(head);
  if (f.why) box.appendChild(el("p", "ed-why", f.why));
  box.appendChild(edgeRow("At", f.t, "fxNudge", "Move it to the playhead", 1 / (S.D.fps || 30)));
  const acts = el("div", "actions ed-sel-acts");
  const labelled = (text, control, title) => {
    const l = el("label", "muted");
    if (title) l.title = title;
    l.append(el("small", "", text + " "), control);
    return l;
  };
  if (f.type !== "word")       // word art lasts as long as its caption
    acts.appendChild(labelled(f.type === "time" && f.style === "freeze" ? "holds" : "length",
                              select(LENGTHS[f.type].map(v => [v, v + " s"]), f.d, { visD: "" },
                                     f.type === "time" && f.style === "freeze" ? "How long the frame holds" : "How long it lasts",
                                     v => nice(v) + " s")));
  if (ZOOMS.includes(f.type)) {
    const names = Object.keys(S.D.regions);
    const opts = names.map(n => [n, n]).concat([["", "centre of the frame"]]);
    if (f.region && !names.includes(f.region)) opts.unshift([f.region, f.region + " (no such region: centre)"]);
    box.appendChild(regionChips(f.region || "", { chipFor: "vis" }, "centre"));
    if (f.type === "push")                       // 1×: from the whole frame; above: carries on from a punch-in
      acts.appendChild(labelled("starts at", select([[1, "whole frame"]].concat(ZOOM_STEPS.punch.map(v => [v, v + "×"])),
                                                    f.from_zoom || 1, { visFrom: "" }, "Where the push starts", v => nice(v) + "×")));
    acts.appendChild(labelled(f.type === "push" ? "ends at" : "zoom",
                              select(ZOOM_STEPS[f.type].map(v => [v, v + "×"]), f.zoom, { visZoom: "" }, "How far it zooms", v => nice(v) + "×")));
    if (f.region && S.D.regions[f.region])
      acts.appendChild(btn("⤢ fill", `Zoom so ${f.region} fills the frame (${nice(fitZoom(f.region))}×)`, { visFit: "" }));
  } else if (f.type === "frame") {
    const stacked = !!(f.top || f.bottom);
    const mode = el("div", "ed-region-chips");
    for (const [v, label, tip] of [["one", "one region", "Show one region full-screen"],
                                   ["stack", "two stacked", "Stack two regions, one above the other (when the scene changed)"]]) {
      const b = btn(label, tip, { shotMode: v });
      b.className = "chip ed-rchip" + ((stacked ? "stack" : "one") === v ? " current" : "");
      mode.appendChild(b);
    }
    box.appendChild(mode);
    if (stacked) {
      box.appendChild(el("small", "muted", "Top"));
      box.appendChild(regionChips(f.top || "", { chipFor: "top" }, "whole frame"));
      box.appendChild(el("small", "muted", "Bottom"));
      box.appendChild(regionChips(f.bottom || "", { chipFor: "bottom" }, "whole frame"));
      acts.appendChild(labelled("top gets", select([0.3, 0.4, 0.5, 0.55, 0.6, 0.65, 0.7].map(v => [v, Math.round(v * 100) + "%"]),
                                                   f.split || 0.6, { shotSplit: "" }, "The top picture's share of the height",
                                                   v => Math.round(v * 100) + "%")));
    } else box.appendChild(regionChips(f.region || "", { chipFor: "vis" }, "", [[FULL, "whole picture (fit + blur)"]]));
  } else if (f.type === "overlay") {
    const ov = overlayOf(f.overlay);
    acts.appendChild(labelled("overlay", select((S.D.overlays || []).map(o => [o.id, (o.insert ? "⧉ " : "") + o.name]),
                                                f.overlay, { visOverlay: "" }, "Which overlay (⧉ = brought in for this clip)",
                                                v => v + " (gone)")));
    acts.appendChild(labelled("plays", select([["both", "picture + sound"], ["sound", "sound only"], ["picture", "picture only"]],
                                              f.mode || "both", { visMode: "" }, "Show its picture, play its sound, or both")));
    if (ov && ov.sound && ovSound(f))
      acts.appendChild(labelled("volume", select([6, 3, 0, -3, -6, -10, -15, -20].map(v => [v, (v > 0 ? "+" : "") + v + " dB"]),
                                                 f.gain || 0, { visGain: "" }, "Its sound's volume")));
    if (ovPicture(f) && f.at === "fill") {
      box.appendChild(el("small", "muted", "Shows"));
      box.appendChild(regionChips("fill", { chipFor: "ovat" }, "", [["fill", "full screen (cutaway)"], ["full", "whole picture"],
                                                                     ["middle", "middle"]]));
      box.appendChild(el("p", "faint ed-why", "A cutaway: it fills the screen over a blurred picture, and her voice goes down while its own sound plays."));
    } else if (ovPicture(f)) placeControls(box, acts, f, [["full", "whole picture"], ["middle", "middle"], ["fill", "full screen (cutaway)"]],
                                           "How wide, as a share of the picture (over 100% spills off the sides)");
    if (ov && ov.description) box.appendChild(el("p", "faint ed-why", ov.description));
    const link = el("div", "ed-insert");
    const url = el("input");
    Object.assign(url, { type: "text", placeholder: "X post, picture or video link, or clip 52 / clip 40-44…" });
    url.dataset.insertUrl = "";
    url.setAttribute("aria-label", "Link to bring in");
    link.append(url, btn("⤓ bring in", "Download it for this clip and show it full screen here", { insertGo: "" }));
    box.appendChild(link);
  } else if (f.type === "layer") {
    box.appendChild(el("small", "muted", "Shows"));
    box.appendChild(regionChips(f.region || "", { chipFor: "lysrc" }, ""));
    acts.appendChild(labelled("opacity", select([0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1].map(v => [v, Math.round(v * 100) + "%"]),
                                                f.opacity || S.D.layer_info.default_opacity, { layerOpacity: "" },
                                                "How solid it is (100% hides what's under it)", v => Math.round(v * 100) + "%")));
    placeControls(box, acts, f, [["middle", "middle"], ["full", "whole picture"]], "How wide, as a share of the picture");
  } else if (f.type === "spot") {
    acts.appendChild(labelled("style", select([["spotlight", "spotlight: dim the rest + ring"], ["circle", "circle: just a ring"],
                                               ["dim", "dim the rest"]], f.style || "spotlight", { spotStyle: "" }, "How it points")));
    if (f.style !== "dim")
      acts.appendChild(labelled("ring", select(Object.keys(S.D.spot_info.colors).map(c => [c, c]), f.color || "",
                                               { spotColor: "" }, "The ring's colour", v => v)));
    if (Object.keys(S.D.regions).length) {
      box.appendChild(el("small", "muted", "Put it round"));
      box.appendChild(regionChips("", { chipFor: "spot" }, ""));
    }
    box.appendChild(el("p", "faint ed-why", "Drag it on the picture to move it; drag its corner to make it wider or taller; "
                       + "scroll over it to resize."));
    if (isShort() && !spotBox(f, f.t + f.d / 2))
      box.appendChild(el("p", "ed-warn", "What it's round isn't in the Short's picture here, so it won't show: move it, "
                         + "or add a frame shot on that region."));
  } else if (f.type === "card") {
    const CI = S.D.card_info;
    const input = el("input");
    Object.assign(input, { type: "text", value: f.text || "", maxLength: 80, className: "ed-sub-input" });
    input.dataset.cardText = "";
    input.setAttribute("aria-label", "What it says");
    input.title = f.style === "poster" ? "*stars* round words give them the colour (else the last line gets it)" : "No emoji (the render can't draw them)";
    box.appendChild(input);
    const STT = { died: "died: dark band, red serif", poster: "poster: words appear one by one", flat: "flat: one line on a plain screen",
                  comment: "comment: a reply-to-comment sticker" };
    acts.appendChild(labelled("style", select(Object.keys(CI.styles).map(k => [k, STT[k] || k]), f.style, { cardStyle: "" }, "Which card", v => v)));
    acts.appendChild(labelled(f.style === "poster" ? "highlight" : "colour",
                              select(Object.keys(CI.colors).map(c => [c, c]), f.color, { cardColor: "" }, "Its colour", v => v)));
    if (f.style === "comment") {
      const who = el("input");
      Object.assign(who, { type: "text", value: f.who || "", maxLength: 24, placeholder: "who wrote it (optional)" });
      who.dataset.cardWho = "";
      who.setAttribute("aria-label", "Who wrote the comment");
      acts.appendChild(labelled("by", who));
      acts.appendChild(labelled("where", select([[-0.3, "top"], [-0.15, "upper"], [0, "middle"], [0.2, "lower"]], f.dy ?? -0.3,
                                                { cardPlace: "" }, "Where the sticker sits", v => nice(v))));
    } else
      acts.appendChild(labelled("screen", select([["black", "black"], ["white", "white"], ["dim", "the picture, darkened"]],
                                                 f.bg || CI.styles[f.style].bg || "black", { cardBg: "" }, "What's behind the words", v => v)));
    box.appendChild(el("p", "faint ed-why", {
      died: "The ironic ending: right after she says she's fine. Pair it with a sound (a low boom) and end the edit on it.",
      poster: "A black screen of words, revealed one by one: the line that sums up the clip, hopecore or demotivational.",
      flat: "A pause in the story: “3 HOURS LATER”, “MEANWHILE…”, “HE DID NOT TAKE IT WELL”.",
      comment: "Open on the comment that the clip answers, like a TikTok reply.",
    }[f.style] || ""));
  } else if (f.type === "text") {
    const input = el("input");
    Object.assign(input, { type: "text", value: f.text, maxLength: S.D.text_info.max, className: "ed-sub-input" });
    input.dataset.textInput = "";
    input.setAttribute("aria-label", "What it says");
    input.title = "*stars* around a word make it yellow · ← → ↑ ↓ point at things · no emoji (the render can't draw them)";
    box.appendChild(input);
    acts.appendChild(labelled("style", select([["pop", "pop: springs in"], ["stamp", "stamp: slams in, CAPS"],
                                               ["label", "label: small, on a box"]], txStyle(f), { textStyle: "" }, "How it shows")));
    acts.appendChild(labelled("colour", select(Object.keys(S.D.text_info.colors).map(c => [c, c]), f.color || "",
                                               { textColor: "" }, "Its colour", v => v)));
    placeControls(box, acts, f, [["middle", "middle of the picture"]], "Font size, as a share of the picture's width");
  } else if (f.type === "caption") {
    const CI = CAPI() || { fx: ["pop"], fonts: { round: "" } };
    const FXT = { pop: "pop: springs in", shake: "shake: anger, screams", wave: "wave: letters bob", wobble: "wobble: rocks",
                  bounce: "bounce: drops in", grow: "grow: swells", none: "none: just appears" };
    const FONTT = { round: "round", angry: "angry: dripping paint", scary: "scary: horror", loud: "loud: comic capitals" };
    box.appendChild(el("p", "faint ed-why", "The captions that start while this lasts get this effect, font and place."));
    acts.appendChild(labelled("effect", select(CI.fx.map(v => [v, FXT[v] || v]), f.fx || "pop", { capFx: "" }, "How they move", v => v)));
    acts.appendChild(labelled("font", select(Object.keys(CI.fonts).map(v => [v === "round" ? "" : v, FONTT[v] || v]), f.font || "",
                                             { capFont: "" }, "Their font", v => v)));
    const SIZET = { small: "small: an aside, a whisper", big: "big: a shout, the punchline", huge: "huge: a scream, one word" };
    acts.appendChild(labelled("size", select([["", "usual"]].concat(Object.keys(CI.sizes || {}).map(v => [v, SIZET[v] || v])),
                                             f.size || "", { capSize: "" }, "Their size", v => v)));
    box.appendChild(el("small", "muted", "Where"));
    box.appendChild(regionChips(f.at || "", { chipFor: "capat" }, "their usual place",
                                [["top", "top"], ["bottom", "bottom"], ["middle", "middle"], ["seam", "between the pictures"]]));
  } else if (f.type === "word") {
    const W = S.D.word_info, c = artCue(f);
    const STT = { box: "box: white on a colour block", slime: "slime: dripping green", glow: "glow: soft neon",
                  fire: "fire: hot comic capitals", scary: "scary: horror red", big: "big: much bigger" };
    if (c) {
      box.appendChild(el("small", "muted", "Which word of “" + c.text.replace(/\*/g, "") + "”"));
      const chips = el("div", "ed-region-chips");
      for (const w of artWords(c.text).slice(0, 20)) {
        const b = btn(w, "Dress this word instead", { artWord: w });
        b.className = "chip ed-rchip" + (w.toLowerCase() === f.word.toLowerCase() ? " current" : "");
        chips.appendChild(b);
      }
      box.appendChild(chips);
    }
    const input = el("input");
    Object.assign(input, { type: "text", value: f.word, maxLength: W.max_chars, className: "ed-sub-input" });
    input.dataset.artInput = "";
    input.setAttribute("aria-label", "The word it dresses");
    input.title = "A word or a 2-3 word phrase of the caption, as it's written there (Enter)";
    box.appendChild(input);
    acts.appendChild(labelled("look", select(Object.keys(W.styles).map(k => [k, STT[k] || k]), f.style, { artStyle: "" },
                                             "How the word looks", v => v)));
    acts.appendChild(labelled("colour", select([["", "its own"]].concat(Object.keys(W.colors).map(k => [k, k])), f.color || "",
                                               { artColor: "" }, "Its colour: the box's, the glow's, the slime's…", v => v)));
    box.appendChild(el("p", "faint ed-why", "The word that carries the line, dressed like clip channels do it: it springs "
                       + "bigger the moment she says it (At is that moment). One or two a clip; never two in one caption."));
    if (!c || !cueArts(c).some(([, , a]) => a.id === f.id))
      box.appendChild(el("p", "ed-warn", c ? `“${f.word}” isn't in the caption here, so it won't show.`
                                           : "No caption shows here, so it won't show: move it onto a caption."));
  } else if (f.type === "time") {
    const TI = S.D.time_info;
    const STT = { slow: "slow motion", ramp: "ramp: fast, then slams into slow", fast: "fast forward", freeze: "freeze frame (stare)" };
    acts.appendChild(labelled("what", select(TI.styles.map(v => [v, STT[v] || v]), f.style, { timeStyle: "" }, "Which time effect", v => v)));
    if (f.style === "freeze") {
      acts.appendChild(labelled("push in", select([1, 1.1, 1.2, 1.25, 1.35, 1.5, 1.75, 2, 2.5].map(v => [v, v === 1 ? "none" : v + "×"]),
                                                  f.zoom ?? TI.zoom[0], { timeZoom: "" }, "How far it pushes in while it holds",
                                                  v => nice(v) + "×")));
      acts.appendChild(labelled("colour", select([["", "as it is"], ["1", "black and white"]], f.mono ? "1" : "",
                                                 { timeMono: "" }, "Black and white while it holds", v => v)));
      if (Object.keys(S.D.regions).length) {
        box.appendChild(el("small", "muted", "Pushes in on"));
        box.appendChild(regionChips(f.region || "", { chipFor: "time" }, "the middle"));
      }
      box.appendChild(el("p", "faint ed-why", "The stare: the picture stops on her face while the music plays on (her voice stops). "
        + "Right after the fail, ending the edit; or a record-scratch freeze before the twist."));
    } else {
      const [, lo, hi] = TI.rates[f.style];
      const steps = [0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.75, 0.9, 1.25, 1.5, 2, 3, 4].filter(v => v >= lo && v <= hi);
      acts.appendChild(labelled(f.style === "ramp" ? "lands at" : "speed", select(steps.map(v => [v, v + "×"]), f.rate,
                                                  { timeRate: "" }, f.style === "ramp" ? "The slow speed it slams into" : "How fast it plays",
                                                  v => nice(v) + "×")));
      acts.appendChild(labelled("her voice", select([["tempo", "same pitch"], ["deep", "deep (pitch follows)"], ["mute", "muted"]],
                                                    f.sound || "tempo", { timeSound: "" }, "What her voice does", v => v)));
      box.appendChild(el("p", "faint ed-why", {
        slow: "The payoff again, slower, so it lands. Deep voice for the slow-motion “noooo”.",
        ramp: `Plays at ${TI.ramp_from}× and slows step by step into the moment at its end: the tuff edit's move, on the music's drop.`,
        fast: "A boring stretch at speed instead of a cut, so the viewer sees time pass.",
      }[f.style]));
      const ps = timeParts().filter(p => p.f === f);
      if (ps.length) box.appendChild(el("p", "faint ed-why", `${nice(ps.reduce((n, p) => n + p.e - p.s, 0))} s of the clip `
        + `play in ${nice(ps.reduce((n, p) => n + partLen(p), 0))} s.`));
      else box.appendChild(el("p", "ed-warn", "It isn't inside your cut, so it does nothing."));
    }
  } else if (f.type === "hush") {
    acts.appendChild(labelled("what", select([["silence", "silence"], ["black", "silence + black picture"]], f.style || "silence",
                                             { hushStyle: "" }, "Silence only, or the picture goes black too", v => v)));
    box.appendChild(el("p", "faint ed-why", "Everything goes quiet, music too: the beat before a drop or a punchline. "
      + "Black + short (0.15–0.4 s) on the beat is the stutter cut of tuff edits."));
  } else if (f.type === "shake") {
    acts.appendChild(labelled("strength", select([[1, "light"], [2, "normal"], [3, "heavy"]], f.strength, { visStrength: "" }, "How strong")));
  } else {
    acts.appendChild(labelled("colour", select(Object.keys(S.D.visual.flash).map(c => [c, c]), f.color, { visColor: "" }, "Flash colour", v => v)));
  }
  box.appendChild(acts);
  const more = el("div", "actions ed-sel-acts");
  const d = btn("Delete", "Delete this effect (Del)", { edAct: "delete" });
  d.classList.add("danger");
  more.append(btn("▶ Play it", "Play from just before it to just after it", { edAct: "fx-play" }), d);
  box.appendChild(more);
  if (ZOOMS.includes(f.type) && !Object.keys(S.D.regions).length)
    box.appendChild(el("p", "faint ed-why", "No regions for this channel yet, so it zooms on the centre. ⋯ More → ▢ Draw regions lets you draw her face once."));
}

// Where an overlay or text sits: the region it follows, its size, how to move it.
function placeControls(box, acts, f, extra, sizeTitle) {
  box.appendChild(el("small", "muted", "Follows"));
  box.appendChild(regionChips(f.at || "middle", { chipFor: "ovat" }, "", extra));
  const sz = el("label", "muted ed-ov-size");
  sz.title = sizeTitle;
  const range = el("input"), [lo, hi] = sizeRange(f), text = f.type === "text";
  Object.assign(range, { type: "range", min: lo * 100, max: hi * 100, step: text ? 0.5 : 1, value: fxSize(f) * 100 });
  range.dataset.visSize = "";
  range.setAttribute("aria-label", text ? "Text size" : "Overlay size");
  sz.append(el("small", "", "size "), range, el("small", "ed-ov-pct", Math.round(fxSize(f) * 100) + "%"));
  box.appendChild(sz);
  const moved = f.dx || f.dy;
  box.appendChild(el("p", "faint ed-why", "Drag it on the picture to move it; drag its corner or scroll over it to resize."
                     + (moved ? " Double-click it to put it back in the middle of what it follows." : "")));
  if (moved) acts.appendChild(btn("⌖ re-centre", "Put it back in the middle of what it follows", { visRecentre: "" }));
}

export function paintSubPanel() {
  const box = $("[data-ed-selected]");
  box.textContent = "";
  const c = curSub(), k = subIndex(c.id);
  const head = el("div", "ed-sel-head");
  head.append(el("b", "", `Subtitle ${k + 1}`), ` of ${S.subs.length} · ${(c.t1 - c.t0).toFixed(2)} s · `
              + ({ you: "yours", claude: "Claude's", auto: "from Whisper's words" })[c.by]);
  box.appendChild(head);
  const input = el("input");
  Object.assign(input, { type: "text", value: c.text, maxLength: 200, className: "ed-sub-input" });
  input.dataset.subInput = c.id;
  input.setAttribute("aria-label", "Subtitle text");
  input.title = "*stars* around a word make it yellow";
  box.appendChild(input);
  const who = el("label", "muted ed-sub-who");
  const opts = [["", "nobody (white)"], ...[...Array(V().count).keys()].map(k => [k, `${k + 1} · ${voiceName(k)}`])];
  const pick = select(opts, c.speaker ?? "", { subVoice: c.id }, "Who says it", v => v);
  if (c.speaker != null) pick.style.color = "#" + voiceText(c.speaker);
  who.append(el("small", "", "said by "), pick);
  if (!V().count) who.append(el("small", "faint", " · add people under Captions first"));
  box.appendChild(who);
  const words = S.D.word_info ? artWords(c.text) : [];
  if (words.length) {             // word art: dress one word of the line
    const row = el("div", "ed-region-chips ed-art-chips"), dressed = cueArts(c).map(([, , a]) => a.word.toLowerCase());
    row.appendChild(el("small", "muted", "✦ word art "));
    for (const w of words.slice(0, 20)) {
      const b = btn(w, `Dress “${w}”: a colour box, slime, a glow…`, { subArt: w });
      b.className = "chip ed-rchip" + (dressed.includes(w.toLowerCase()) ? " current" : "");
      row.appendChild(b);
    }
    box.appendChild(row);
  }
  const f = 1 / (S.D.fps || 30);
  box.appendChild(edgeRow("Start", c.t0, "subNudge0", "Start it at the playhead", f));
  box.appendChild(edgeRow("End", c.t1, "subNudge1", "End it at the playhead", f));
  const acts = el("div", "actions ed-sel-acts");
  const d = btn("Delete", "Delete this line", { subDel: c.id });
  d.classList.add("danger");
  acts.append(btn("✂ Split", "Split it at the playhead (or at a word in the middle)", { subSplit: c.id }),
              btn("⤓ Join next", "Join it with the next line", { subJoin: c.id }), d);
  box.appendChild(acts);
  if (subWidth(c.text) > 2 * S.D.sub_layout.max_line)
    box.appendChild(el("p", "ed-warn ed-why", "Long for two lines on screen: split it, or shorten it."));
}
// The people panel: who's in this clip, a name (a datalist of people already named on
// this channel) and a colour each; ▶ plays their first line. Lines get a person with
// "said by", or with the number keys (see tagKey).
export function paintVoices() {
  const box = $("[data-ed-voices]"), v = V();
  box.hidden = false;
  const want = JSON.stringify([v.count, v.map, v.names || [], S.subs.map(c => c.speaker ?? "").join()]);
  if (box.dataset.painted === want || box.contains(document.activeElement)) return;   // not while you type
  box.dataset.painted = want;
  box.textContent = "";
  box.appendChild(el("b", "", "People in this clip"));
  box.appendChild(el("p", "faint", ""));
  box.lastChild.appendChild(el("small", "", (v.count
    ? "Who says a line: select it and press their number (it moves on to the next line), or press the number "
      + "while the video plays to tag the line on screen. 0 = nobody (white)."
    : "In a collab, add each person to colour their lines.") + " Colours come from the VTubers page."));
  const list = el("datalist");
  list.id = "ed-people";
  for (const n of v.names || []) { const o = el("option"); o.value = n; list.appendChild(o); }
  box.appendChild(list);
  for (let k = 0; k < v.count; k++) {
    const row = el("div", "ed-voice");
    row.appendChild(el("kbd", "", String(k + 1)));
    const color = el("input");
    Object.assign(color, { type: "color", value: "#" + voiceColor(k).toLowerCase(), title: "Colour of their lines" });
    color.dataset.voiceColor = k;
    const name = el("input");
    Object.assign(name, { type: "text", value: ((v.map || {})[k] || {}).name || "", placeholder: `Person ${k + 1}`, maxLength: 24 });
    name.setAttribute("list", "ed-people");
    name.setAttribute("aria-label", `Name of person ${k + 1}`);
    name.dataset.voiceName = k;
    const n = S.subs.filter(c => c.speaker === k).length;
    row.append(color, name, btn("▶", "Play their first line", { voicePlay: k }), el("small", "faint", `${n} line${n === 1 ? "" : "s"}`));
    box.appendChild(row);
  }
  const add = btn("+ Add person", "Someone else talks in this clip", { voiceAdd: "" });
  add.disabled = v.count >= (v.max || 8);
  box.appendChild(add);
}
export function addPerson() {
  const v = V();
  if (v.count >= (v.max || 8)) return;
  v.map = v.map || {};
  const k = v.count;
  v.map[k] = { name: "", color: (v.palette || [])[k % Math.max(1, (v.palette || []).length)] || "FFFFFF" };
  v.count += 1;
  S.D.voices = v;
  paintVoices();
  const inp = $(`[data-voice-name="${k}"]`);
  if (inp) inp.focus();
}
// A number key: that person says the selected line (then on to the next one), or, while
// the video plays, the line on screen. 0: nobody.
export function tagKey(n) {
  const k = n === 0 ? null : n - 1;
  if (giveTicked(k)) return;
  if (k !== null && k >= V().count) { flash(`Add person ${n} under Captions first.`); return; }
  if (!video.paused) {
    const here = subsAt(video.currentTime);
    if (!here.length) return;
    pushUndo();
    for (const c of here) setSpeaker(c, k);
    subsChanged();
    return;
  }
  const c = curSub();
  if (!c) { flash("Select a line first (or press the number while the video plays)."); return; }
  pushUndo();
  setSpeaker(c, k);
  subsChanged();
  const next = S.subs.slice().sort((a, b) => a.t0 - b.t0).find(x => x.t0 > c.t0);
  if (next) selectSub(next.id); else call.paintAll();
}
export async function setVoice(k, change) {
  const v = V();
  v.map = v.map || {};
  const next = { ...(v.map[k] || {}), ...change };
  if (change.name && !("color" in change)) {          // someone named before on this channel: her colour
    const known = (v.people || {})[change.name.toLowerCase()];
    if (known) next.color = known;
  }
  v.map[k] = next;
  S.subKey = "";
  S.needsDraw = true;
  try {
    const d = await post(`/moment/${pk}/voices`, { voices: v.map, recolor: "color" in change ? k : null });
    v.map = { ...v.map, ...d.voices };
    v.people = d.people;
    v.names = d.names;
  } catch (e) { flash(e.message); }
  delete $("[data-ed-voices]").dataset.painted;
  S.subKey = "";
  call.paintAll();
}
export function playVoice(k) {
  const c = S.subs.filter(x => x.speaker === k).sort((a, b) => a.t0 - b.t0)[0];
  if (!c) return;
  stopEditPlay();
  seek(Math.max(0, c.t0 - 0.2));
  S.previewUntil = Math.min(S.D.duration, c.t1 + 0.3);
  video.play();
}
export function paintSubBar() {
  $("[data-sub-on]").checked = S.subStyle.on;
  $("[data-sub-size]").value = S.subStyle.size;
  $("[data-sub-pos]").value = S.subStyle.position;
  if ($("[data-sub-look]")) $("[data-sub-look]").value = S.subStyle.look || "classic";
  $("[data-sub-mode]").value = S.subStyle.mode || "lines";
  $('[data-ed-act="sub-rebuild"]').disabled = !S.D.subs_auto.length;
  const pb = $('[data-ed-act="sub-polish"]');
  pb.disabled = !S.subs.length || pb.dataset.busy === "1";
  pb.textContent = S.D.sub_language && S.D.sub_language !== "en" ? "✨ Fix & translate" : "✨ Clean up";
  if (!S.subs.length) {
    subStatus(S.D.transcript.source === "whisper" ? "No subtitle lines: + Line adds one, ↻ From transcript brings Whisper's back."
      : "Subtitles are made from Whisper's words: press 🎙 Transcribe above the transcript first.");
    return;
  }
  const lang = S.D.sub_language && S.D.sub_language !== "en" ? ` · in ${LANG[S.D.sub_language] || S.D.sub_language}: Claude's translation comes in the next step` : "";
  subStatus(`${S.subs.length} line${S.subs.length === 1 ? "" : "s"}` + (S.subsMine ? " · edited by you" : " from Whisper's words")
    + lang + (S.subStyle.on ? "" : " · not burned in (the .srt is still made)")
    + " · click a line on the right to retype it.");
}
