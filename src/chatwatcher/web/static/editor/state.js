// The editor's shared ground: the page's elements, the constants, and S, the edit being
// worked on. Imports nothing, so every other module can import it.
export const root = document.querySelector("[data-editor]");
export const pk = root.dataset.editor;
export const video = root.querySelector("video");
export const $ = s => root.querySelector(s);
export const scroller = $("[data-ed-scroll]"), spacer = $("[data-ed-spacer]"), canvas = $("[data-ed-canvas]");
export const ctx = canvas.getContext("2d");
export const stage = $("[data-ed-stage]"), sctx = stage.getContext("2d"), regLayer = $("[data-ed-regions]");
export const subLayer = $("[data-ed-subs]");
export const SUB_MIN = 0.2, SUB_SNAP_S = 0.35;
export const ASS_TO_CSS = 0.69;     // libass's font size is the line's height, CSS's the em: measured on a render
export const MIN = 0.2, EDGE_PX = 7, FX_PX = 8, DRAG_SNAP_S = 0.35, LINE_SNAP_S = 0.8;
export const ROW = { beats: 18, wave: 96, fx: 26, cam: 26, vfx: 22, subs: 22, ruler: 18 };
export const Y_FX = ROW.beats + ROW.wave, Y_CAM = Y_FX + ROW.fx, Y_VFX = Y_CAM + ROW.cam, Y_SUBS = Y_VFX + ROW.vfx,
      Y_RULER = Y_SUBS + ROW.subs;
// The lanes, top to bottom, for the labels at the timeline's left edge.
export const LANES = [["Beats", 0, ROW.beats], ["Cut", ROW.beats, ROW.wave], ["Sounds", Y_FX, ROW.fx], ["Camera", Y_CAM, ROW.cam],
               ["Effects", Y_VFX, ROW.vfx], ["Captions", Y_SUBS, ROW.subs]];
const CAM = ["punch", "push", "frame"];             // the Camera lane; shakes and flashes are on the Effects lane
export const laneOf = f => (CAM.includes(f.type) ? "cam" : "vfx");
export const HEIGHT = Y_RULER + ROW.ruler;
export const VIS = ["punch", "push", "shake", "flash", "frame", "overlay", "layer", "text", "spot", "caption"], ZOOMS = ["punch", "push"];
export const VLABEL = { punch: "Punch-in", push: "Push-in", shake: "Shake", flash: "Flash", frame: "Show a region",
                 overlay: "Overlay", layer: "Layer", text: "Text", spot: "Spotlight", caption: "Caption style" };
export const STRENGTH = { 1: "light", 2: "normal", 3: "heavy" };
export const LENGTHS = { punch: [0.5, 0.8, 1, 1.5, 2, 3, 5], push: [1, 2, 3, 4, 6, 8, 12],
                  shake: [0.2, 0.4, 0.6, 1, 1.5, 2], flash: [0.1, 0.2, 0.3, 0.5, 0.8, 1.2],
                  frame: [0.5, 1, 1.5, 2, 3, 4, 6, 10], overlay: [0.5, 1, 1.5, 2, 3, 5, 8],
                  layer: [1, 2, 3, 5, 8, 12, 20, 30],
                  text: [0.5, 0.8, 1, 1.5, 2, 3, 4, 6], spot: [0.5, 1, 1.5, 2, 3, 4, 6],
                  caption: [0.5, 1, 2, 3, 5, 8, 12, 20, 30] };
export const ZOOM_STEPS = { punch: [1.2, 1.4, 1.6, 2, 2.5, 3], push: [1.1, 1.2, 1.3, 1.5, 2] };
export const REGION_NAMES = ["face", "game", "chat"];
export const GAINS = [6, 3, 0, -3, -6, -10, -15, -20, -30];
export const DUCKS = [0, -6, -10, -14, -20];
export const SOUND_LEVEL = -14;     // the library's level (edit/sounds.py)
export const ticked = new Set();     // lines ticked in the list, to give them a person at once
export const LANG = { en: "English", id: "Indonesian", ja: "Japanese", ko: "Korean", zh: "Chinese", es: "Spanish" };

// The edit being worked on. Only what more than one module reads or writes is here; the
// rest is a `let` in the module that owns it.
export const S = {
  D: null,                 // /api/moment/<pk>/edit
  segs: [], sel: 0, source: null, zoom: 1,
  fx: [], selFx: null,     // sounds and picture effects; selFx = id of the selected one
  undo: [], drag: null, editPlay: null,
  armFrom: null,           // where ▶ Play edit jumped to: sounds fire when playback crosses them
  saveTimer: null, saving: false, dirty: false,
  needsDraw: true,
  snapOn: true, lastSound: null,
  regionMode: false, previewUntil: null,
  subs: [], subStyle: { on: true, size: "normal", position: "bottom", mode: "lines", look: "classic" }, selSub: null,
  subsMine: false,         // false: the lines follow Whisper's words (saved as "auto"); true: you changed them
  subKey: "", listFor: null,
  kind: "long",            // which edit: long (edit.mp4) | short (short.mp4, 9:16)
  layout: null,            // the Short's layout (edit/layout.py)
  layEditing: false,       // a slider or the title is being changed (one undo step for the whole drag)
  C: {},                   // the theme's colours (colors() in util.js)
  ovSizing: false,         // an overlay or spotlight is being resized with its handle
  txEditing: false,        // a text callout is being typed in the player
};
// Functions of later modules that earlier ones call; main.js fills it in.
export const call = {};
try { S.snapOn = localStorage.getItem("cw-ed-snap") !== "0"; S.lastSound = localStorage.getItem("cw-ed-sound"); } catch (e) {}
try { if (localStorage.getItem("cw-ed-kind") === "short") S.kind = "short"; } catch (e) {}
$("[data-ed-snap]").checked = S.snapOn;
