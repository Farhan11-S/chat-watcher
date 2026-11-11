// Moment page editor: Claude's cut on a waveform timeline. Drag or nudge the edges,
// split, reorder and set volume per segment, place sound effects and punch-ins,
// push-ins, shakes and flashes (or let Claude place them), preview it all in the
// browser, edit the subtitles Whisper's words give, then render edit.mp4 on the server.
// The Short tab is a second edit of the same clip in 9:16 (its own cut, effects and
// subtitles, plus a layout: crop / stack / fit), previewed on a canvas and rendered to short.mp4.
// Everything here is clip time (seconds into clip.mp4).
(function () {
  const root = document.querySelector("[data-editor]");
  if (!root) return;
  const pk = root.dataset.editor;
  const video = root.querySelector("video");
  const $ = s => root.querySelector(s);
  const scroller = $("[data-ed-scroll]"), spacer = $("[data-ed-spacer]"), canvas = $("[data-ed-canvas]");
  const ctx = canvas.getContext("2d");
  const stage = $("[data-ed-stage]"), sctx = stage.getContext("2d"), regLayer = $("[data-ed-regions]");
  const subLayer = $("[data-ed-subs]");
  const SUB_MIN = 0.2, SUB_SNAP_S = 0.35;
  const ASS_TO_CSS = 0.69;     // libass's font size is the line's height, CSS's the em: measured on a render
  const MIN = 0.2, EDGE_PX = 7, FX_PX = 8, DRAG_SNAP_S = 0.35, LINE_SNAP_S = 0.8;
  const ROW = { beats: 18, wave: 96, fx: 26, cam: 26, vfx: 22, subs: 22, ruler: 18 };
  const Y_FX = ROW.beats + ROW.wave, Y_CAM = Y_FX + ROW.fx, Y_VFX = Y_CAM + ROW.cam, Y_SUBS = Y_VFX + ROW.vfx,
        Y_RULER = Y_SUBS + ROW.subs;
  // The lanes, top to bottom, for the labels at the timeline's left edge.
  const LANES = [["Beats", 0, ROW.beats], ["Cut", ROW.beats, ROW.wave], ["Sounds", Y_FX, ROW.fx], ["Camera", Y_CAM, ROW.cam],
                 ["Effects", Y_VFX, ROW.vfx], ["Captions", Y_SUBS, ROW.subs]];
  const CAM = ["punch", "push", "frame"];             // the Camera lane; shakes and flashes are on the Effects lane
  const laneOf = f => (CAM.includes(f.type) ? "cam" : "vfx");
  const HEIGHT = Y_RULER + ROW.ruler;
  const VIS = ["punch", "push", "shake", "flash", "frame"], ZOOMS = ["punch", "push"];
  const VLABEL = { punch: "Punch-in", push: "Push-in", shake: "Shake", flash: "Flash", frame: "Show a region" };
  const STRENGTH = { 1: "light", 2: "normal", 3: "heavy" };
  const LENGTHS = { punch: [0.5, 0.8, 1, 1.5, 2, 3, 5], push: [1, 2, 3, 4, 6, 8, 12],
                    shake: [0.2, 0.4, 0.6, 1, 1.5, 2], flash: [0.1, 0.2, 0.3, 0.5, 0.8, 1.2],
                    frame: [0.5, 1, 1.5, 2, 3, 4, 6, 10] };
  const ZOOM_STEPS = { punch: [1.2, 1.4, 1.6, 2, 2.5, 3], push: [1.1, 1.2, 1.3, 1.5, 2] };
  const REGION_NAMES = ["face", "game", "chat"];
  const GAINS = [6, 3, 0, -3, -6, -10, -15, -20, -30];
  const DUCKS = [0, -6, -10, -14, -20];
  const SOUND_LEVEL = -14;     // the library's level (edit/sounds.py)

  let D = null;                // /api/moment/<pk>/edit
  let segs = [], sel = 0, source = null, zoom = 1;
  let fx = [], selFx = null;   // sound effects; selFx = id of the selected one
  let undo = [], drag = null, editPlay = null;
  let saveTimer = null, saving = false, dirty = false, renderPoll = null, watchVideo = null;
  let needsDraw = true, lastLine = -1;
  let snapOn = true, lastSound = null;
  let regionMode = false, regSel = null, regDrag = null, regTimer = null, previewUntil = null, stageOn = false;
  let txPoll = null;
  let subs = [], subStyle = { on: true, size: "normal", position: "bottom", mode: "lines" }, selSub = null;
  let subsMine = false;        // false: the lines follow Whisper's words (saved as "auto"); true: you changed them
  let subKey = "", listFor = null;
  let kind = "long";           // which edit: long (edit.mp4) | short (short.mp4, 9:16)
  let layout = null;           // the Short's layout (edit/layout.py)
  let layEditing = false;      // a slider or the title is being changed (one undo step for the whole drag)
  let comp = null, bgc = null; // the Short's preview: the picture before zoom, and the small blurred background
  try { snapOn = localStorage.getItem("cw-ed-snap") !== "0"; lastSound = localStorage.getItem("cw-ed-sound"); } catch (e) {}
  try { if (localStorage.getItem("cw-ed-kind") === "short") kind = "short"; } catch (e) {}
  $("[data-ed-snap]").checked = snapOn;

  // ── helpers ──────────────────────────────────────────────────────────────
  function fmt(t, d = 2) {
    const k = 10 ** d;
    let v = Math.round(Math.max(0, t) * k);
    const m = Math.floor(v / (60 * k));
    v -= m * 60 * k;
    let s = (v / k).toFixed(d);
    if (v < 10 * k) s = "0" + s;
    return m + ":" + s;
  }
  const r3 = v => Math.round(v * 1000) / 1000;
  const clone = a => a.map(s => s.slice());
  const cloneFx = a => a.map(f => ({ ...f }));
  const total = () => segs.reduce((n, s) => n + s[1] - s[0], 0);
  const hold = () => { window.cwHold = true; };   // don't auto-reload the page under the editor
  const sound = id => D.sounds.find(s => s.id === id);
  const curFx = () => fx.find(f => f.id === selFx);
  const kept = t => segs.some(s => t >= s[0] && t < s[1]);
  const isVis = f => VIS.includes(f.type);
  const keptFx = f => (isVis(f) ? segs.some(s => f.t < s[1] && f.t + f.d > s[0]) : kept(f.t));
  const nice = v => String(Math.round(v * 100) / 100);
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function btn(text, title, data) {
    const b = el("button", "small", text);
    b.type = "button";
    if (title) b.title = title;
    for (const [k, v] of Object.entries(data || {})) b.dataset[k] = v;
    return b;
  }
  function select(options, value, data, label, custom = v => (v > 0 ? "+" : "") + v + " dB") {
    const s = el("select");
    for (const [k, v] of Object.entries(data)) s.dataset[k] = v;
    s.setAttribute("aria-label", label);
    for (const [val, text] of options) {
      const o = el("option", "", text);
      o.value = val;
      if (String(val) === String(value)) o.selected = true;
      s.appendChild(o);
    }
    if (!options.some(([val]) => String(val) === String(value))) {   // a value from Claude or an older edit
      const o = el("option", "", custom(value));
      o.value = value;
      o.selected = true;
      s.appendChild(o);
    }
    return s;
  }
  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    let d = {};
    try { d = await r.json(); } catch (e) {}
    if (!r.ok || d.ok === false) throw new Error(d.error || ("HTTP " + r.status));
    return d;
  }
  function snap(t, win, points) {
    let best = t, cost = Infinity;
    for (const [p, depth] of points || D.pauses) {
      const dt = Math.abs(p - t);
      if (dt > win) continue;
      const c = dt / (1 + depth / 12);
      if (c < cost) { cost = c; best = p; }
    }
    return best;
  }

  // ── picture effects: the same maths as camera() in edit/motion.py, so the preview
  // shows what the render will. A view is a centre and a half-size, as fractions of the frame.
  function centre(name) {
    const b = D.regions[name];
    if (!b) return [0.5, 0.5];
    if (kind !== "short" || !layout) return [b.x + b.w / 2, b.y + b.h / 2];
    return mapPoint(b.x + b.w / 2, b.y + b.h / 2, shortPanels(video.currentTime)) || [0.5, 0.5];     // where it lands on the Short
  }

  // ── the Short's layout: the same maths as panels() in edit/layout.py ──────
  const isShort = () => kind === "short" && !!layout && !regionMode;
  function fitBox([bx, by, bw, bh], aspect, sw, sh) {
    let pw = bw * sw, ph = bh * sh;
    if (pw / ph < aspect) pw = ph * aspect; else ph = pw / aspect;
    if (pw > sw) { pw = sw; ph = sw / aspect; }
    if (ph > sh) { pw = sh * aspect; ph = sh; }
    const cx = (bx + bw / 2) * sw, cy = (by + bh / 2) * sh;
    const x0 = Math.min(Math.max(cx - pw / 2, 0), sw - pw), y0 = Math.min(Math.max(cy - ph / 2, 0), sh - ph);
    return [x0 / sw, y0 / sh, pw / sw, ph / sh];
  }
  const boxOf = name => { const b = name && D.regions[name]; return b ? [b.x, b.y, b.w, b.h] : [0, 0, 1, 1]; };
  function panels() {
    const [OW, OH] = D.layout_info.out;
    const sw = video.videoWidth || D.layout_info.src[0], sh = video.videoHeight || D.layout_info.src[1];
    const L = layout;
    if (L.type === "stack") {
      const s = L.split;
      return [{ src: fitBox(boxOf(L.top), OW / (OH * s), sw, sh), dst: [0, 0, 1, s] },
              { src: fitBox(boxOf(L.bottom), OW / (OH * (1 - s)), sw, sh), dst: [0, s, 1, 1 - s] }];
    }
    if (L.type === "fit") {
      const h = OW * sh / sw / OH;
      return [{ src: fitBox([0, 0, 1, 1], OW / OH, sw, sh), dst: [0, 0, 1, 1], blur: true },
              { src: [0, 0, 1, 1], dst: [0, (1 - h) / 2, 1, h] }];
    }
    let ph = sh / L.scale, pw = ph * OW / OH;
    if (pw > sw) { pw = sw; ph = sw * OH / OW; }
    const x0 = Math.min(Math.max(L.x * sw - pw / 2, 0), sw - pw), y0 = Math.min(Math.max(L.y * sh - ph / 2, 0), sh - ph);
    return [{ src: [x0 / sw, y0 / sh, pw / sw, ph / sh], dst: [0, 0, 1, 1] }];
  }
  // A frame shot: the Short shows one region full-screen (frame_panels in edit/layout.py).
  function framePanels(name) {
    const b = D.regions[name], [OW, OH] = D.layout_info.out, F = D.layout_info.frame;
    const sw = video.videoWidth || D.layout_info.src[0], sh = video.videoHeight || D.layout_info.src[1];
    const w = Math.min(b.w * F.margin, 1), h = Math.min(b.h * F.margin, 1), cx = b.x + b.w / 2, cy = b.y + b.h / 2;
    const grown = [Math.min(Math.max(cx - w / 2, 0), 1 - w), Math.min(Math.max(cy - h / 2, 0), 1 - h), w, h];
    const aspect = OW / OH;
    if ((w * sw) / (h * sh) <= aspect * F.wide) return [{ src: fitBox(grown, aspect, sw, sh), dst: [0, 0, 1, 1] }];
    const src = fitBox(grown, OW / (OH * 0.6), sw, sh);
    const dh = Math.min(0.6, OW / (src[2] * sw) * (src[3] * sh) / OH);
    return [{ src: fitBox(grown, aspect, sw, sh), dst: [0, 0, 1, 1], blur: true }, { src, dst: [0, (1 - dh) / 2, 1, dh] }];
  }
  function shortPanels(t) {
    let on = null;
    for (const f of fx) if (f.type === "frame" && active(f, t) && D.regions[f.region] && (!on || f.t >= on.t)) on = f;
    return on ? framePanels(on.region) : panels();
  }
  function mapPoint(x, y, ps) {
    for (const p of [...ps].reverse()) {
      if (p.blur) continue;
      const [sx, sy, sw, sh] = p.src, [dx, dy, dw, dh] = p.dst;
      if (sx <= x && x <= sx + sw && sy <= y && y <= sy + sh) return [dx + (x - sx) / sw * dw, dy + (y - sy) / sh * dh];
    }
    return null;
  }
  function fitZoom(name) {
    const b = D.regions[name], [lo, hi] = D.visual.zoom_range;
    if (!b) return 1.5;
    return Math.round(Math.min(Math.max(Math.min(1 / Math.max(b.w, 1e-3), 1 / Math.max(b.h, 1e-3)), lo), hi) * 100) / 100;
  }
  const active = (f, t) => f.t <= t && t < f.t + f.d;
  // Transitions on real cuts → camera / flash / blur pieces in clip time (motion.transitions).
  function transVis() {
    const out = [], T = D.visual.transitions, [dlo, dhi] = D.visual.transition_d;
    for (let i = 1; i < segs.length; i++) {
      const s = segs[i][0], prev = segs[i - 1], e = prev[1];
      if (Math.abs(s - e) < 0.002) continue;
      const tr = fx.find(f => f.type === "transition" && T[f.style] && Math.abs(f.t - s) < 0.05);
      if (!tr) continue;
      const d = Math.min(Math.max(+tr.d || T[tr.style][0], dlo), dhi);
      const h = Math.min(d / 2, (e - prev[0]) / 3, (segs[i][1] - s) / 3);
      if (tr.style === "flash") out.push({ type: "flash", t: s, d: Math.max(h * 2, 0.1), color: "white" });
      else if (tr.style === "fade") out.push({ type: "fadeout", t: e - h, d: h, color: "black" }, { type: "flash", t: s, d: h, color: "black" });
      else if (tr.style === "zoom") out.push({ type: "push", t: e - h, d: h, zoom: 1.8, cx: 0.5, cy: 0.5 },
                                             { type: "pull", t: s, d: h, zoom: 1.8, cx: 0.5, cy: 0.5 });
      else if (tr.style === "whip") out.push({ type: "push", t: e - h, d: h, zoom: 1.35, cx: 1, cy: 0.5 }, { type: "blur", t: e - h, d: h },
                                             { type: "pull", t: s, d: h, zoom: 1.35, cx: 0, cy: 0.5 }, { type: "blur", t: s, d: h });
    }
    return out;
  }
  const blurAt = t => transVis().some(v => v.type === "blur" && active(v, t));
  const wave = (tau, ws) => ws.reduce((n, [w, hz, ph]) => n + w * Math.sin(2 * Math.PI * hz * tau + ph), 0);
  function camera(t) {
    let z = 1, cx = 0.5, cy = 0.5, zf = null;
    const zs = fx.filter(f => ZOOMS.includes(f.type)).concat(transVis().filter(v => v.type === "push" || v.type === "pull"));
    for (const f of zs.sort((a, b) => a.t - b.t)) if (active(f, t)) zf = f;
    if (zf) {
      const q = Math.min(Math.max((t - zf.t) / zf.d, 0), 1), sq = q * q * (3 - 2 * q);
      const p = zf.type === "punch" ? 1 : zf.type === "pull" ? 1 - sq : sq;
      const [rx, ry] = zf.cx !== undefined ? [zf.cx, zf.cy] : centre(zf.region);
      z = 1 + (zf.zoom - 1) * p; cx = 0.5 + (rx - 0.5) * p; cy = 0.5 + (ry - 0.5) * p;
    }
    let a = 0, dx = 0, dy = 0;
    for (const f of fx) {
      if (f.type !== "shake" || !active(f, t)) continue;
      const e = (D.visual.shake[f.strength] || D.visual.shake[2]) * (1 - (t - f.t) / f.d);
      a += e;
      dx += e * wave(t - f.t, D.visual.waves[0]);
      dy += e * wave(t - f.t, D.visual.waves[1]);
    }
    const h = 0.5 / (z * (1 + D.visual.margin * a)), lo = h + a, hi = 1 - h - a;
    if (lo > hi) { cx = 0.5; cy = 0.5; } else { cx = Math.min(Math.max(cx, lo), hi); cy = Math.min(Math.max(cy, lo), hi); }
    return [cx + dx, cy + dy, h];
  }
  function flashesAt(t) {
    return fx.filter(f => f.type === "flash").concat(transVis().filter(v => v.type === "flash" || v.type === "fadeout"))
      .filter(f => active(f, t)).map(f => {
        const [rgb, peak] = D.visual.flash[f.color] || D.visual.flash.white;
        const q = (t - f.t) / f.d;
        return [rgb, peak * (f.type === "fadeout" ? q : 1 - q)];
      });
  }
  // The picture box inside the <video> element (it can be letterboxed), relative to the player.
  function pictureBox() {
    const vw = video.videoWidth || 16, vh = video.videoHeight || 9, bw = video.clientWidth, bh = video.clientHeight;
    const k = Math.min(bw / vw, bh / vh), w = vw * k, h = vh * k;
    return { left: video.offsetLeft + (bw - w) / 2, top: video.offsetTop + (bh - h) / 2, width: w, height: h };
  }
  // Where the picture shows: the video's box, or the Short's 9:16 box in the player.
  function viewBox() {
    if (!isShort()) return pictureBox();
    const pl = video.parentElement, bw = pl.clientWidth, bh = pl.clientHeight;
    const k = Math.min(bw / 9, bh / 16), w = 9 * k, h = 16 * k;
    return { left: (bw - w) / 2, top: (bh - h) / 2, width: w, height: h };
  }
  function place(elm, box) {
    for (const k of ["left", "top", "width", "height"]) elm.style[k] = box[k] + "px";
  }
  // The Short: its panels drawn from the video, then the zoom/shake view of that, flashes
  // and the title (subtitles are the HTML layer on top, as for the long edit).
  function drawShort() {
    if (stage.hidden) { stage.hidden = false; stageOn = true; }
    const box = viewBox(), dpr = window.devicePixelRatio || 1;
    place(stage, box);
    const cw = Math.round(box.width * dpr), ch = Math.round(box.height * dpr);
    if (stage.width !== cw || stage.height !== ch) { stage.width = cw; stage.height = ch; }
    if (!comp) { comp = document.createElement("canvas"); bgc = document.createElement("canvas"); }
    if (comp.width !== cw || comp.height !== ch) { comp.width = cw; comp.height = ch; }
    const c = comp.getContext("2d"), vw = video.videoWidth, vh = video.videoHeight;
    c.fillStyle = "#000";
    c.fillRect(0, 0, cw, ch);
    if (vw && video.readyState >= 2) {
      for (const p of shortPanels(video.currentTime)) {
        const [sx, sy, sw, sh] = p.src, [dx, dy, dw, dh] = p.dst;
        if (p.blur) {           // small, blurred, scaled up and darkened: like the render's background
          const bw = Math.max(2, Math.round(cw * dw / D.layout_info.blur_down)), bh = Math.max(2, Math.round(ch * dh / D.layout_info.blur_down));
          if (bgc.width !== bw || bgc.height !== bh) { bgc.width = bw; bgc.height = bh; }
          const b = bgc.getContext("2d");
          b.filter = "blur(2px)";
          b.drawImage(video, sx * vw, sy * vh, sw * vw, sh * vh, 0, 0, bw, bh);
          c.drawImage(bgc, 0, 0, bw, bh, dx * cw, dy * ch, dw * cw, dh * ch);
          c.fillStyle = `rgba(0,0,0,${1 - D.layout_info.dark})`;
          c.fillRect(dx * cw, dy * ch, dw * cw, dh * ch);
        } else c.drawImage(video, sx * vw, sy * vh, sw * vw, sh * vh, dx * cw, dy * ch, dw * cw, dh * ch);
      }
    }
    const [xc, yc, h] = camera(video.currentTime);
    sctx.filter = blurAt(video.currentTime) ? `blur(${Math.round(cw / 60)}px)` : "none";
    sctx.drawImage(comp, (xc - h) * cw, (yc - h) * ch, 2 * h * cw, 2 * h * ch, 0, 0, cw, ch);
    sctx.filter = "none";
    for (const [rgb, alpha] of flashesAt(video.currentTime)) {
      sctx.fillStyle = `rgba(${rgb.join(",")},${alpha})`;
      sctx.fillRect(0, 0, cw, ch);
    }
    drawTitle(cw, ch);
  }
  function drawTitle(cw, ch) {
    if (!layout.title) return;
    const L = D.sub_layout, size = L.title_size * cw;        // libass size: the line's height
    sctx.save();
    sctx.font = `700 ${size * ASS_TO_CSS}px "${L.font}", "Noto Sans CJK JP", "Noto Sans JP", system-ui, sans-serif`;
    sctx.textBaseline = "middle";
    sctx.lineJoin = "round";
    let y = ch * 0.06 + size / 2, on = false;
    for (const line of wrapSub(layout.title, L.title_line, 3)) {
      const [pieces, after] = runsOf(line, on);
      on = after;
      const widths = pieces.map(([t]) => sctx.measureText(t).width);
      let x = cw / 2 - widths.reduce((a, b) => a + b, 0) / 2;
      pieces.forEach(([t, hi], k) => {
        sctx.lineWidth = size * 0.24;
        sctx.strokeStyle = "#000";
        sctx.strokeText(t, x, y);
        sctx.fillStyle = hi ? "#" + L.highlight : "#fff";
        sctx.fillText(t, x, y);
        x += widths[k];
      });
      y += size;
    }
    sctx.restore();
  }
  // While a zoom, shake or flash shows, a canvas over the video draws the view; otherwise
  // it's hidden and you see the video (and its controls) as usual.
  function drawStage() {
    if (isShort()) return drawShort();
    const t = video.currentTime;
    const [xc, yc, h] = camera(t), fl = flashesAt(t);
    const show = !regionMode && video.videoWidth > 0 && video.readyState >= 2
      && (h < 0.4995 || Math.abs(xc - 0.5) > 1e-4 || Math.abs(yc - 0.5) > 1e-4 || fl.length > 0 || blurAt(t));
    if (show !== stageOn) { stage.hidden = !show; stageOn = show; }
    if (!show) return;
    const box = pictureBox(), dpr = window.devicePixelRatio || 1;
    place(stage, box);
    const cw = Math.round(box.width * dpr), ch = Math.round(box.height * dpr);
    if (stage.width !== cw || stage.height !== ch) { stage.width = cw; stage.height = ch; }
    const vw = video.videoWidth, vh = video.videoHeight;
    sctx.filter = blurAt(t) ? `blur(${Math.round(cw / 80)}px)` : "none";
    sctx.drawImage(video, (xc - h) * vw, (yc - h) * vh, 2 * h * vw, 2 * h * vh, 0, 0, cw, ch);
    sctx.filter = "none";
    for (const [rgb, alpha] of fl) {
      sctx.fillStyle = `rgba(${rgb.join(",")},${alpha})`;
      sctx.fillRect(0, 0, cw, ch);
    }
  }

  // ── colours (follow the page theme) ──────────────────────────────────────
  let C = {};
  function colors() {
    const cs = getComputedStyle(document.documentElement);
    const v = n => cs.getPropertyValue(n).trim();
    C = { bg: v("--surface-2"), wave: v("--text-3"), waveIn: v("--series-1"), seg: v("--series-1-soft"),
          segLine: v("--series-1"), sel: v("--series-2"), text: v("--text"), text2: v("--text-2"), text3: v("--text-3"),
          play: v("--bad"), other: v("--other"), hot: v("--series-2"), cool: v("--series-1"), border: v("--border") };
  }
  colors();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { colors(); needsDraw = true; });
  const beatColor = b => ({ hook: C.hot, payoff: C.hot, reaction: C.cool, callback: C.cool })[b] || C.other;

  // ── geometry: canvas covers the visible part; the spacer gives the scroll width ──
  const fullWidth = () => Math.max(scroller.clientWidth, 100) * zoom;
  const X = t => t / D.duration * fullWidth();                  // time → x in the full timeline
  const T = x => Math.min(D.duration, Math.max(0, x / fullWidth() * D.duration));
  const gx = ev => ev.clientX - canvas.getBoundingClientRect().left + scroller.scrollLeft;
  const gy = ev => ev.clientY - canvas.getBoundingClientRect().top;

  function resize() {
    if (!D) return;
    const w = scroller.clientWidth, dpr = window.devicePixelRatio || 1;
    spacer.style.width = fullWidth() + "px";
    spacer.style.height = HEIGHT + "px";
    canvas.style.width = w + "px";
    canvas.style.height = HEIGHT + "px";
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(HEIGHT * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    needsDraw = true;
    paintRegions();
  }

  function draw() {
    if (!D) return;
    const w = scroller.clientWidth, off = scroller.scrollLeft, W = fullWidth(), dur = D.duration;
    const x = t => t / dur * W - off;
    const t0 = off / W * dur, t1 = (off + w) / W * dur;
    ctx.clearRect(0, 0, w, HEIGHT);
    ctx.fillStyle = C.bg;
    ctx.fillRect(0, ROW.beats, w, ROW.wave);
    ctx.font = "11px system-ui, sans-serif";
    ctx.textBaseline = "alphabetic";

    // Claude's storyboard beats along the top
    for (const b of D.beats) {
      if (b.end < t0 || b.start > t1) continue;
      const a = x(b.start), z = x(b.end);
      ctx.globalAlpha = 0.3;
      ctx.fillStyle = beatColor(b.beat);
      ctx.fillRect(a, 2, Math.max(1, z - a - 1), ROW.beats - 4);
      ctx.globalAlpha = 1;
      if (z - a > 34) { ctx.fillStyle = C.text2; ctx.fillText(b.beat, a + 4, ROW.beats - 6, z - a - 8); }
    }
    // segment backgrounds
    for (const s of segs) { const a = x(s[0]); ctx.fillStyle = C.seg; ctx.fillRect(a, ROW.beats, x(s[1]) - a, ROW.wave); }
    // waveform: kept parts in colour, the rest grey
    const mid = ROW.beats + ROW.wave / 2, amp = ROW.wave / 2 - 5, P = D.peaks, rate = D.rate;
    for (let px = 0; px < w; px++) {
      const ta = (px + off) / W * dur, tb = (px + 1 + off) / W * dur;
      const i0 = Math.floor(ta * rate), i1 = Math.max(i0 + 1, Math.ceil(tb * rate));
      let m = 0;
      for (let i = i0; i < i1 && i < P.length; i++) if (P[i] > m) m = P[i];
      const h = Math.max(0.5, m / 100 * amp);
      ctx.fillStyle = kept(ta) ? C.waveIn : C.wave;
      ctx.fillRect(px, mid - h, 1, h * 2);
    }
    // pause points: where cuts snap
    ctx.fillStyle = C.text3;
    for (const [p] of D.pauses) {
      if (p < t0 || p > t1) continue;
      ctx.fillRect(Math.round(x(p)), Y_FX - 4, 1, 4);
    }
    // segment edges and numbers (selected one last, on top)
    const order = segs.map((_, i) => i).filter(i => i !== sel).concat(segs[sel] ? [sel] : []);
    ctx.font = "600 11px system-ui, sans-serif";
    for (const i of order) {
      const s = segs[i], a = x(s[0]), z = x(s[1]), on = i === sel && !selFx, col = on ? C.sel : C.segLine;
      if (z < -20 || a > w + 20) continue;
      ctx.strokeStyle = col;
      ctx.lineWidth = on ? 2 : 1;
      ctx.strokeRect(a + 0.5, ROW.beats + 0.5, Math.max(1, z - a - 1), ROW.wave - 1);
      ctx.fillStyle = col;
      ctx.fillRect(a - 1, ROW.beats, 3, ROW.wave);
      ctx.fillRect(z - 2, ROW.beats, 3, ROW.wave);
      const label = String(i + 1) + (s[2] ? " · " + (s[2] > 0 ? "+" : "") + s[2] + " dB" : "");
      const lw = ctx.measureText(label).width + 8;
      ctx.fillRect(a, ROW.beats, lw, 15);
      ctx.fillStyle = "#ffffff";
      ctx.fillText(label, a + 4, ROW.beats + 11);
    }
    drawFx(x, t0, t1, w);
    for (let i = 1; i < segs.length; i++) {          // transitions: a tag at the cut
      const tr = cutInto(i) && transOf(segs[i]);
      if (!tr) continue;
      const a = x(segs[i][0]);
      if (a < -40 || a > w + 40) continue;
      ctx.font = "600 10px system-ui, sans-serif";
      const lw = ctx.measureText(tr.style).width + 10;
      ctx.fillStyle = C.hot;
      ctx.fillRect(a - lw / 2, ROW.beats + ROW.wave - 15, lw, 14);
      ctx.fillStyle = "#fff";
      ctx.fillText(tr.style, a - lw / 2 + 5, ROW.beats + ROW.wave - 4);
    }
    drawVfx(x, t0, t1, w, "cam");
    drawVfx(x, t0, t1, w, "vfx");
    drawSubs(x, t0, t1, w);
    // ruler
    const pxPerS = W / dur;
    const step = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300].find(s => s * pxPerS >= 70) || 600;
    ctx.font = "11px system-ui, sans-serif";
    ctx.fillStyle = C.text2;
    for (let t = Math.floor(t0 / step) * step; t <= t1 + step; t += step) {
      const px = x(t);
      ctx.fillRect(px, Y_RULER, 1, 4);
      ctx.fillText(fmt(t, step < 1 ? 1 : 0), px + 3, Y_RULER + 14);
    }
    // playhead
    const ph = x(video.currentTime);
    ctx.fillStyle = C.play;
    ctx.fillRect(ph - 1, 0, 2, Y_RULER);
    // time of the edge, sound or effect being dragged
    const df = drag && (drag.fx || drag.vis) ? curFx() : null, ds = drag && drag.sub ? curSub() : null;
    const dragT = df ? (drag.edge === "end" ? df.t + df.d : df.t) : ds ? (drag.edge === "t1" ? ds.t1 : ds.t0)
      : drag && drag.edge !== undefined && segs[drag.i] ? segs[drag.i][drag.edge] : null;
    if (dragT !== null && dragT !== undefined) {
      const label = fmt(dragT), lw = ctx.measureText(label).width + 8, px = Math.min(Math.max(x(dragT) - lw / 2, 0), w - lw);
      ctx.fillStyle = C.sel;
      ctx.fillRect(px, ROW.beats + ROW.wave / 2 - 9, lw, 18);
      ctx.fillStyle = "#ffffff";
      ctx.fillText(label, px + 4, ROW.beats + ROW.wave / 2 + 4);
    }
  }

  // The sound lane: hits (where sounds snap) as ticks, each sound as a diamond with its length.
  function drawFx(x, t0, t1, w) {
    const mid = Y_FX + ROW.fx / 2;
    ctx.fillStyle = C.border;
    ctx.fillRect(0, Y_FX, w, 1);
    ctx.globalAlpha = 0.55;
    ctx.fillStyle = C.hot;
    for (const [t, s] of D.hits) {
      if (t < t0 || t > t1) continue;
      const h = Math.max(3, Math.min(9, 3 + s / 4));
      ctx.fillRect(Math.round(x(t)), Y_FX + 1, 1, h);
    }
    ctx.globalAlpha = 1;
    const list = fx.filter(f => f.type === "sfx").sort((a, b) => a.t - b.t);
    list.forEach((f, k) => {
      const snd = sound(f.sound), len = snd ? snd.length : 0.5;
      const a = x(f.t), z = x(f.t + len), on = f.id === selFx;
      if (z < -10 || a > w + 10) return;
      const col = on ? C.sel : f.by === "claude" ? C.cool : C.hot;
      ctx.globalAlpha = kept(f.t) ? 1 : 0.35;
      // where it plays, through the waveform
      ctx.fillStyle = col;
      ctx.globalAlpha *= 0.5;
      ctx.fillRect(Math.round(a), ROW.beats, 1, ROW.wave);
      ctx.globalAlpha = kept(f.t) ? 0.35 : 0.15;
      ctx.fillRect(a, mid - 2, Math.max(2, z - a), 4);
      ctx.globalAlpha = kept(f.t) ? 1 : 0.35;
      ctx.beginPath();
      ctx.moveTo(a, mid - 7); ctx.lineTo(a + 7, mid); ctx.lineTo(a, mid + 7); ctx.lineTo(a - 7, mid); ctx.closePath();
      ctx.fill();
      if (on) { ctx.strokeStyle = C.text; ctx.lineWidth = 1.5; ctx.stroke(); }
      const next = list[k + 1] ? x(list[k + 1].t) : w;
      const room = next - a - 14;
      if (room > 24) {
        ctx.fillStyle = C.text2;
        ctx.font = (on ? "600 " : "") + "11px system-ui, sans-serif";
        ctx.fillText(snd ? snd.name : f.sound + " (missing)", a + 10, mid + 4, room);
      }
      ctx.globalAlpha = 1;
    });
  }

  // The picture lane: punch-ins as blocks, push-ins as ramps, shakes as zigzags, flashes as
  // fading bars. Drag one to move it, drag its right end to change its length.
  function visLabel(f) {
    if (ZOOMS.includes(f.type)) return `${f.type === "punch" ? "punch" : "push"} ${f.region || "centre"} ${nice(f.zoom)}×`;
    if (f.type === "shake") return "shake " + (STRENGTH[f.strength] || "");
    if (f.type === "frame") return "show " + (f.region || "?");
    return "flash " + f.color;
  }
  // A colour per region, so the Camera lane shows at a glance what's on screen when.
  const REGION_COLS = ["#2a78d6", "#0f9d74", "#c2410c", "#8b5cf6", "#d4a106", "#db2777", "#0891b2", "#65a30d"];
  function regionCol(name) {
    const k = Object.keys(D.regions).indexOf(name);
    return k < 0 ? C.text3 : REGION_COLS[k % REGION_COLS.length];
  }
  function drawVfx(x, t0, t1, w, lane) {
    const Y = lane === "cam" ? Y_CAM : Y_VFX, H = lane === "cam" ? ROW.cam : ROW.vfx;
    const top = Y + 3, bot = Y + H - 3, mid = (top + bot) / 2;
    ctx.fillStyle = C.border;
    ctx.fillRect(0, Y, w, 1);
    const list = fx.filter(f => isVis(f) && laneOf(f) === lane).sort((a, b) => a.t - b.t);
    list.forEach((f, k) => {
      const a = x(f.t), z = Math.max(x(f.t + f.d), a + 4), on = f.id === selFx;
      if (z < -10 || a > w + 10) return;
      const col = on ? C.sel : lane === "cam" ? regionCol(f.region) : f.by === "claude" ? C.cool : C.hot,
            alpha = keptFx(f) ? 1 : 0.35;
      ctx.fillStyle = col;
      ctx.strokeStyle = col;
      if (f.type === "push") {
        ctx.globalAlpha = alpha * 0.6;
        ctx.beginPath(); ctx.moveTo(a, bot); ctx.lineTo(z, top); ctx.lineTo(z, bot); ctx.closePath(); ctx.fill();
      } else if (f.type === "shake") {
        ctx.globalAlpha = alpha;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        for (let px = a, up = true; px <= z; px += 4, up = !up) {
          const amp = (bot - top) / 2 * (1 - (px - a) / Math.max(1, z - a) * 0.7);
          px === a ? ctx.moveTo(px, mid) : ctx.lineTo(px, mid + (up ? -amp : amp));
        }
        ctx.stroke();
      } else if (f.type === "flash") {
        const g = ctx.createLinearGradient(a, 0, z, 0);
        g.addColorStop(0, col);
        g.addColorStop(1, "transparent");
        ctx.globalAlpha = alpha * 0.8;
        ctx.fillStyle = g;
        ctx.fillRect(a, top, z - a, bot - top);
      } else if (f.type === "frame") {                       // a shot showing another region: solid
        ctx.globalAlpha = alpha * 0.85;
        ctx.fillRect(a, top, z - a, bot - top);
      } else {
        ctx.globalAlpha = alpha * 0.45;
        ctx.fillRect(a, top, z - a, bot - top);
      }
      ctx.globalAlpha = alpha;
      ctx.fillStyle = col;
      ctx.fillRect(a, top, 2, bot - top);                       // where it starts
      if (on) { ctx.strokeStyle = C.text; ctx.lineWidth = 1.5; ctx.strokeRect(a + 0.5, top + 0.5, z - a - 1, bot - top - 1); }
      if (on || z - a > 14) { ctx.fillStyle = on ? C.text : col; ctx.fillRect(z - 2, top, 2, bot - top); }   // drag to resize
      const room = (list[k + 1] ? x(list[k + 1].t) : w) - a - 8;
      ctx.font = (on ? "600 " : "") + "11px system-ui, sans-serif";
      const label = [visLabel(f), f.type].find(s => ctx.measureText(s).width <= room);   // never squashed
      if (label) {
        ctx.fillStyle = C.text;
        ctx.fillText(label, a + 5, mid + 4);
      }
      ctx.globalAlpha = 1;
    });
  }

  // The subtitle lane: one block per line, its text inside. Drag an edge to retime it, the
  // middle to move it; edges snap to Whisper's word times.
  function drawSubs(x, t0, t1, w) {
    const top = Y_SUBS + 3, bot = Y_SUBS + ROW.subs - 3;
    ctx.fillStyle = C.border;
    ctx.fillRect(0, Y_SUBS, w, 1);
    ctx.font = "11px system-ui, sans-serif";
    for (const c of subs) {
      if (c.t1 < t0 || c.t0 > t1) continue;
      const a = x(c.t0), z = Math.max(x(c.t1), a + 3), on = c.id === selSub;
      const col = on ? C.sel : c.by === "you" ? C.hot : c.by === "claude" ? C.cool : C.other;
      ctx.globalAlpha = (subStyle.on ? 1 : 0.5) * (segs.some(s => c.t0 < s[1] && c.t1 > s[0]) ? 1 : 0.35);
      ctx.fillStyle = col;
      ctx.globalAlpha *= on ? 0.55 : 0.3;
      ctx.fillRect(a, top, z - a - 1, bot - top);
      ctx.globalAlpha /= on ? 0.55 : 0.3;
      ctx.fillRect(a, top, 2, bot - top);
      if (on) { ctx.strokeStyle = C.text; ctx.lineWidth = 1.5; ctx.strokeRect(a + 0.5, top + 0.5, z - a - 2, bot - top - 1); }
      if (z - a > 24) {
        ctx.fillStyle = C.text;
        ctx.fillText(fitText(c.text.replace(/\*/g, ""), z - a - 9), a + 5, bot - 5);
      }
      ctx.globalAlpha = 1;
    }
  }

  function fitText(text, room) {          // cut with "…" rather than squash
    if (ctx.measureText(text).width <= room) return text;
    let lo = 0, hi = text.length;
    while (lo < hi) { const m = (lo + hi + 1) >> 1; if (ctx.measureText(text.slice(0, m) + "…").width <= room) lo = m; else hi = m - 1; }
    return lo ? text.slice(0, lo) + "…" : "";
  }

  // ── subtitle layout: the same rules as subs/lines.py (wrap, runs), so the preview matches ──
  const WIDE = /[\u1100-\u115f\u2e80-\ua4cf\uac00-\ud7a3\uf900-\ufaff\ufe30-\ufe4f\uff00-\uff60\uffe0-\uffe6]/g;
  const subWidth = t => { const p = t.replace(/\*/g, ""); return p.length + (p.match(WIDE) || []).length; };
  function wrapSub(text, max = D.sub_layout.max_line, lines = 2) {
    if (subWidth(text) <= max || lines < 2) return [text];
    const half = subWidth(text) / 2, chars = [...text];
    let cuts = chars.map((ch, i) => (ch === " " ? i : -1)).filter(i => i >= 0);
    if (!cuts.length) cuts = chars.map((_, i) => i).slice(1);
    let best = cuts[0], cost = Infinity;
    for (const i of cuts) {
      const c = Math.abs(subWidth(chars.slice(0, i).join("")) - half);
      if (c < cost) { cost = c; best = i; }
    }
    const a = chars.slice(0, best).join("").trimEnd(), b = chars.slice(best).join("").trimStart();
    if (!(a && b)) return [text];
    if (lines === 2) return [a, b];
    return [...wrapSub(a, max, Math.floor(lines / 2)), ...wrapSub(b, max, lines - Math.floor(lines / 2))];
  }
  function runsOf(line, on) {
    const out = [];
    line.split("*").forEach((part, i) => { if (i) on = !on; if (part) out.push([part, on]); });
    return [out, on];
  }
  const curSub = () => subs.find(c => c.id === selSub);
  const subAt = t => subs.find(c => c.t0 <= t && t < c.t1);
  // The current line over the video, styled like the render (bold white, black outline).
  function drawSubPreview() {
    const c = !regionMode && subStyle.on && video.videoWidth ? subAt(video.currentTime) : null;
    const box = viewBox(), L = D.sub_layout, titled = isShort() && !!layout.title;
    const key = c ? [c.id, c.text, subStyle.size, subStyle.position, Math.round(box.width), Math.round(box.height),
                     box.left, titled].join("|") : "";
    if (key === subKey) return;
    subKey = key;
    subLayer.hidden = !c;
    if (!c) return;
    place(subLayer, box);
    const size = L.sizes[subStyle.size] * (L.vertical ? box.width : box.height);      // libass size in screen pixels
    const inner = el("div", "ed-subs-text");
    Object.assign(inner.style, {
      fontFamily: `"${D.sub_layout.font}", "Noto Sans CJK JP", "Noto Sans JP", system-ui, sans-serif`,
      fontSize: size * ASS_TO_CSS + "px", lineHeight: size + "px", WebkitTextStroke: size * 0.2 + "px #000",
      textShadow: `${size * 0.045}px ${size * 0.045}px 0 rgba(0,0,0,.5)`,
      [subStyle.position === "top" ? "top" : "bottom"]:
        box.height * (subStyle.position === "top" ? (titled ? L.top_title : L.top) : L.bottom) + "px",
    });
    let on = false;
    wrapSub(c.text, L.max_line, L.max_lines).forEach((line, k) => {
      if (k) inner.appendChild(el("br"));
      const [pieces, after] = runsOf(line, on);
      on = after;
      for (const [text, hi] of pieces) {
        const sp = el("span", "", text);
        if (hi) sp.style.color = "#" + D.sub_layout.highlight;
        inner.appendChild(sp);
      }
    });
    subLayer.textContent = "";
    subLayer.appendChild(inner);
  }

  // ── sound playback for the preview (Web Audio) ───────────────────────────
  let actx = null;
  const buffers = new Map(), playing = new Set();
  let duckUntil = 0, duckF = 1;
  function audio() {
    if (!actx) { try { actx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) { return null; } }
    if (actx.state === "suspended") actx.resume();
    return actx;
  }
  function buffer(id) {
    if (!buffers.has(id)) {
      const a = audio();
      if (!a) return Promise.reject(new Error("no audio"));
      buffers.set(id, fetch(`/media/sound/${id}`).then(r => { if (!r.ok) throw new Error(r.status); return r.arrayBuffer(); })
        .then(b => a.decodeAudioData(b)).catch(e => { buffers.delete(id); throw e; }));
    }
    return buffers.get(id);
  }
  const preload = () => fx.forEach(f => { if (f.type === "sfx") buffer(f.sound).catch(() => {}); });
  async function playSound(id, gainDb) {
    const a = audio();
    if (!a) return;
    let b;
    try { b = await buffer(id); } catch (e) { return; }
    const src = a.createBufferSource(), g = a.createGain();
    // gain 0 = about as loud as her talking: move the library level to this clip's level
    g.gain.value = userVol * 10 ** ((gainDb + (D.loudness - SOUND_LEVEL)) / 20);
    src.buffer = b;
    src.connect(g).connect(a.destination);
    src.onended = () => playing.delete(src);
    playing.add(src);
    src.start();
  }
  function stopSounds() { for (const s of playing) { try { s.stop(); } catch (e) {} } playing.clear(); duckUntil = 0; }
  function fire(f) {
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
  function mixVolume() {
    const segG = editPlay && segs[editPlay.i] ? Math.min(1, 10 ** ((segs[editPlay.i][2] || 0) / 20)) : 1;
    const duck = duckUntil > performance.now() && !video.paused ? duckF : 1;
    const v = Math.max(0, Math.min(1, userVol * segG * duck));
    if (Math.abs(video.volume - v) > 0.002) { lastSetVol = v; video.volume = v; }
  }

  // ── playback ─────────────────────────────────────────────────────────────
  let prevT = null, armFrom = null;   // sounds fire when playback crosses them
  function seek(t) {
    video.currentTime = Math.min(Math.max(0, t), D.duration);
    needsDraw = true;
  }
  function playEdit(from = 0, only = false, at = null) {
    if (!segs.length) return;
    audio();
    editPlay = { i: from, only };
    const t = at === null ? segs[from][0] : at;
    armFrom = t - 0.0005;
    seek(t);
    video.play();
    paintButtons();
  }
  // Play the edit from the segment closest to the playhead: from the playhead itself when
  // it's inside one, else from the start of the nearest segment. Then on to the end.
  function playFromHere() {
    if (!segs.length) return;
    const t = video.currentTime;
    const inside = segs.findIndex(s => s[0] <= t && t < s[1] - 0.1);
    if (inside >= 0) { playEdit(inside, false, t); return; }
    const gap = s => (t < s[0] ? s[0] - t : t - s[1]);
    let best = 0;
    segs.forEach((s, i) => { if (gap(s) < gap(segs[best])) best = i; });
    playEdit(best);
  }
  function stopEditPlay() {
    if (!editPlay) return;
    editPlay = null;
    paintButtons();
  }
  function toggle() {
    audio();
    if (video.paused) video.play(); else { video.pause(); stopEditPlay(); }
  }
  video.addEventListener("pause", () => { if (editPlay && !video.seeking) stopEditPlay(); stopSounds(); previewUntil = null; needsDraw = true; });
  video.addEventListener("play", () => audio());
  video.addEventListener("seeked", () => { needsDraw = true; });

  function tick() {
    if (D) {
      const t = video.currentTime;
      if (editPlay && !video.paused && !video.seeking) {
        let s = segs[editPlay.i];
        if (!s || t < s[0] - 0.25 || t > s[1] + 0.25) {      // you jumped elsewhere in the clip
          const j = segs.findIndex(q => t >= q[0] && t < q[1]);
          if (j < 0) stopEditPlay(); else { editPlay.i = j; s = segs[j]; }
        }
        if (editPlay && t >= s[1] - 0.02) {
          if (editPlay.only || editPlay.i + 1 >= segs.length) {
            video.pause(); stopEditPlay(); seek(s[1]);
          } else {
            editPlay.i++;
            armFrom = segs[editPlay.i][0] - 0.0005;
            seek(segs[editPlay.i][0]);
          }
        }
      }
      if (video.seeking) prevT = armFrom;
      else {
        if (!video.paused && prevT !== null && t > prevT && t - prevT < 0.5)
          for (const f of fx) if (f.type === "sfx" && f.t > prevT && f.t <= t) fire(f);
        prevT = t;
        armFrom = null;
      }
      if (previewUntil !== null && !video.paused && t >= previewUntil) { previewUntil = null; video.pause(); }
      mixVolume();
      if (!video.paused || video.seeking || needsDraw || drag) {
        if (!video.paused && !drag) follow(t);
        drawStage();
        drawSubPreview();
        draw();
        paintTime(t);
        paintLine(t);
        needsDraw = false;
      }
    }
    requestAnimationFrame(tick);
  }
  function follow(t) {           // keep the playhead on screen while playing zoomed in
    const px = X(t), l = scroller.scrollLeft, w = scroller.clientWidth;
    if (px < l || px > l + w - 16) scroller.scrollLeft = px - w * 0.15;
  }
  function paintTime(t) {
    let text = fmt(t);
    if (editPlay && segs[editPlay.i]) {
      const before = segs.slice(0, editPlay.i).reduce((n, s) => n + s[1] - s[0], 0);
      text += "  ·  edit " + fmt(before + Math.max(0, t - segs[editPlay.i][0]), 1) + " / " + fmt(total(), 1);
    }
    $("[data-ed-time]").textContent = text;
  }

  // ── editing ──────────────────────────────────────────────────────────────
  function pushUndo() {
    undo.push({ segs: clone(segs), sel, source, fx: cloneFx(fx), selFx, subs: cloneFx(subs), subStyle: { ...subStyle },
                selSub, subsMine, layout: layout && { ...layout } });
    if (undo.length > 200) undo.shift();
    $('[data-ed-act="undo"]').disabled = false;
  }
  function doUndo() {
    const u = undo.pop();
    if (!u) return;
    segs = u.segs; sel = u.sel; source = u.source; fx = u.fx; selFx = u.selFx;
    subs = u.subs; subStyle = u.subStyle; selSub = u.selSub; subsMine = u.subsMine;
    if (u.layout) layout = u.layout;
    listFor = null;
    subKey = "";
    $('[data-ed-act="undo"]').disabled = !undo.length;
    changed();
  }
  function changed() {
    stopEditPlay();
    dirty = true;
    paintAll();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, 600);
    setSaved("Saving…");
  }
  function selectSeg(i, jump = true) {
    if (!segs.length) return;
    sel = (i + segs.length) % segs.length;
    selFx = null;
    if (jump) seek(segs[sel][0]);
    paintAll();
  }
  function selectFx(id, jump = true) {
    selFx = id;
    const f = curFx();
    if (f && jump) seek(f.t);
    paintAll();
  }
  function setEdge(edge, t) {
    const s = segs[sel];
    if (!s) return;
    if (edge === 0 ? t > s[1] - MIN : t < s[0] + MIN) {
      flash(edge === 0 ? "The start must be before the end." : "The end must be after the start.");
      return;
    }
    pushUndo();
    const tr = edge === 0 ? transOf(s) : null;
    s[edge] = r3(Math.min(Math.max(0, t), D.duration));
    if (tr) tr.t = s[0];
    seek(s[edge]);
    changed();
  }
  function nudge(edge, dt) {
    const s = segs[sel];
    if (!s) return;
    setEdge(edge, s[edge] + dt);
  }
  function split() {
    const t = video.currentTime;
    let j = segs[sel] && t - segs[sel][0] >= MIN && segs[sel][1] - t >= MIN ? sel
      : segs.findIndex(s => t - s[0] >= MIN && s[1] - t >= MIN);
    if (j < 0) { flash("Put the playhead inside a segment to split it."); return; }
    pushUndo();
    const [a, b, g] = segs[j];
    segs.splice(j, 1, [a, r3(t), g], [r3(t), b, g]);
    sel = j + 1;
    selFx = null;
    changed();
  }
  function add() {
    let a = video.currentTime, b = Math.min(D.duration, a + 5);
    if (b - a < 1) a = Math.max(0, b - 5);
    if (snapOn) { a = snap(a, DRAG_SNAP_S); b = snap(b, DRAG_SNAP_S); }
    if (b - a < MIN) return;
    pushUndo();
    segs.splice(sel + 1, 0, [r3(a), r3(b), 0]);
    sel = sel + 1;
    selFx = null;
    changed();
  }
  function del() {
    if (selSub && curSub()) { deleteSub(selSub); return; }
    if (selFx) { pushUndo(); fx = fx.filter(f => f.id !== selFx); selFx = null; changed(); return; }
    if (segs.length <= 1) { flash("An edit needs at least one segment."); return; }
    pushUndo();
    segs.splice(sel, 1);
    sel = Math.min(sel, segs.length - 1);
    changed();
  }
  function move(dir) {
    const j = sel + dir;
    if (j < 0 || j >= segs.length) return;
    pushUndo();
    [segs[sel], segs[j]] = [segs[j], segs[sel]];
    sel = j;
    changed();
  }
  function setGain(g) {
    const s = segs[sel];
    if (!s || s[2] === g) return;
    pushUndo();
    s[2] = g;
    changed();
  }
  function usePreset(name) {
    const p = D.presets[name];
    if (!p) return;
    const same = JSON.stringify(p) === JSON.stringify(segs);
    const names = { long: "Claude's cut", short: "Claude's short", whole: "the whole clip" };
    if (!same && D.saved && !confirm(`Replace your ${segs.length} segment(s) with ${names[name]}? (Undo brings them back.)`)) return;
    pushUndo();
    segs = clone(p);
    source = name;
    sel = 0;
    selFx = null;
    changed();
    seek(segs[0][0]);
  }

  // sound effects
  function addFx() {
    if (!D.sounds.length) { fxStatus("Your sound library is empty: add sounds on the Sounds page first.", "err", true); return; }
    const snd = sound(lastSound) || D.sounds[0];
    let t = video.currentTime;
    if (snapOn) t = snap(t, DRAG_SNAP_S, D.hits);
    pushUndo();
    const f = { id: "u" + Math.random().toString(36).slice(2, 8), type: "sfx", t: r3(t), sound: snd.id,
                gain: snd.gain || 0, duck: 0, by: "you", why: "" };
    fx.push(f);
    selFx = f.id;
    buffer(f.sound).catch(() => {});
    changed();
  }
  function editFx(change) {
    const f = curFx();
    if (!f) return;
    pushUndo();
    Object.assign(f, change, { by: "you" });
    if (change.sound) {
      lastSound = change.sound;
      try { localStorage.setItem("cw-ed-sound", lastSound); } catch (e) {}
      buffer(change.sound).catch(() => {});
    }
    changed();
  }
  // zooms, shakes and flashes
  function addVis(type) {
    const [d] = D.visual.types[type];
    let t = video.currentTime;
    if (snapOn && type !== "push") t = snap(t, DRAG_SNAP_S, D.hits);   // these land on a moment
    const f = { id: "u" + Math.random().toString(36).slice(2, 8), type, t: r3(t), d: Math.min(d, D.duration - t), by: "you", why: "" };
    if (f.d < D.visual.types[type][1]) { flash("Too close to the end of the clip."); return; }
    if (type === "frame") {
      if (kind !== "short") { flash("Frame shots are for Shorts: switch to the Short first."); return; }
      const names = Object.keys(D.regions);
      if (!names.length) { flash("Draw a region first (⋯ More → ▢ Draw regions): a frame shot shows one full-screen."); return; }
      f.region = names.find(n => n !== (layout && layout.bottom)) || names[0];
    } else if (ZOOMS.includes(type)) {
      const names = Object.keys(D.regions);
      f.region = names.includes("face") ? "face" : names[0] || "";
      f.zoom = type === "punch" ? (f.region ? Math.min(fitZoom(f.region), 2.5) : 1.5) : 1.25;
    } else if (type === "shake") f.strength = 2;
    else f.color = "white";
    pushUndo();
    fx.push(f);
    selFx = f.id;
    changed();
    if (ZOOMS.includes(type) && !f.region)
      fxStatus("No regions yet, so this zooms on the centre. Draw her face with ⋯ More → ▢ Draw regions to aim zooms at it.", "");
  }
  function previewFx() {         // play from a little before the effect to a little after it
    const f = curFx();
    if (!f) return;
    stopEditPlay();
    seek(Math.max(0, f.t - 1));
    previewUntil = Math.min(D.duration, f.t + (f.d || (sound(f.sound) || { length: 1 }).length) + 1);
    video.play();
  }
  function clearClaude() {
    const n = fx.filter(f => f.by === "claude").length;
    if (!n) { fxStatus("None of the effects are Claude's.", ""); return; }
    pushUndo();
    fx = fx.filter(f => f.by !== "claude");
    if (selFx && !curFx()) selFx = null;
    changed();
    fxStatus(`Removed ${n} of Claude's effects. Ctrl+Z brings them back.`, "");
  }
  async function autoEdit() {
    const b = $('[data-ed-act="autoedit"]');
    if (segs.length > 1 && !confirm("✨ AI edit replaces your cut, Claude's effects and the subtitles with a new edit (your own effects stay; Ctrl+Z brings everything back). Go on?")) return;
    b.disabled = true;
    clearTimeout(saveTimer);
    fxStatus("Claude is editing the whole clip: cuts, hook, camera and effects… (usually 30–90 s)", "busy");
    try {
      const d = await post(`/moment/${pk}/autoedit`, { ...body(), intensity: $("[data-ed-intensity]").value });
      const fresh = await fetchEdit(kind);
      if (fresh.ok === false) throw new Error(fresh.error || "Couldn't reload the edit.");
      pushUndo();
      D = fresh;
      segs = clone(fresh.segments);
      fx = cloneFx(fresh.effects || []);
      source = fresh.source;
      subs = cloneFx(fresh.subs || []);
      subStyle = { ...fresh.subs_style };
      subsMine = fresh.subs_source === "saved";
      if (fresh.layout) layout = { ...fresh.layout };
      sel = 0; selFx = null; selSub = null; subKey = ""; listFor = null; dirty = false;
      D.saved = true;
      setSaved("Saved");
      paintKind();
      paintAll();
      showRender(d.render);
      preload();
      const c = d.counts || {};
      const bits = [`cut ${d.cut_s.toFixed(0)} s (kept ${d.kept} of ${d.pieces} pieces)`,
                    d.cold_open && "opens on a teaser", `${c.camera || 0} camera moves`,
                    c.sounds && `${c.sounds} sounds`, c.hits && `${c.hits} shakes/flashes`].filter(Boolean);
      fxStatus(`Claude's edit: ${bits.concat(c.volume ? [`${c.volume} volume change${c.volume > 1 ? "s" : ""}`] : []).join(", ")}. ${d.note || ""} ${(d.warnings || []).join(" ")} Ctrl+Z undoes.`, "ok");
    } catch (e) {
      fxStatus(e.message, "err");
    } finally {
      b.disabled = false;
    }
  }
  async function suggestFx() {
    const b = $('[data-ed-act="suggest"]');
    b.disabled = true;
    clearTimeout(saveTimer);
    fxStatus("Claude is picking effects for your cut… (usually 15–60 s)", "busy");
    try {
      const d = await post(`/moment/${pk}/effects/suggest`, { ...body(), intensity: $("[data-ed-intensity]").value });
      pushUndo();
      fx = d.effects;
      selFx = null;
      dirty = false;
      D.saved = true;
      setSaved("Saved");
      paintAll();
      showRender(d.render);
      preload();
      const skipped = d.dropped ? ` (${d.dropped} outside your cut skipped)` : "";
      const n = (k, one, many) => `${k} ${k === 1 ? one : many}`;
      const what = [d.added_sounds && n(d.added_sounds, "sound", "sounds"),
                    d.added_visual && n(d.added_visual, "zoom, shake or flash", "zooms, shakes and flashes")].filter(Boolean);
      fxStatus((what.length ? `Claude added ${what.join(" and ")}${skipped}. ` : `Claude added nothing${skipped}. `)
               + (d.note || "") + " Ctrl+Z undoes.", d.added ? "ok" : "");
    } catch (e) {
      fxStatus(e.message, "err", /library is empty/.test(e.message));
    } finally {
      b.disabled = false;
    }
  }

  // subtitles
  const subIndex = id => subs.findIndex(c => c.id === id);
  function subsChanged() { subsMine = true; listFor = null; subKey = ""; changed(); }
  function selectSub(id, jump = true) {
    selSub = id;
    selFx = null;
    const c = curSub();
    if (c && jump) seek(c.t0);
    paintAll();
    const row = c && $(`.ed-line[data-sub="${c.id}"]`);
    if (row && !$("[data-ed-lines]").matches(":hover")) row.scrollIntoView({ block: "nearest" });
  }
  function addSub() {
    const t = video.currentTime, next = subs.find(c => c.t0 > t), inside = subAt(t);
    if (inside) { selectSub(inside.id, false); flash("There's a line here already: edit it, or split it."); return; }
    const t1 = Math.min(t + 2, next ? next.t0 - 0.04 : D.duration);
    if (t1 - t < 0.3) { flash("No room for a new line here."); return; }
    pushUndo();
    const c = { id: "u" + Math.random().toString(36).slice(2, 8), t0: r3(t), t1: r3(t1), text: "…", by: "you" };
    subs.push(c);
    subs.sort((a, b) => a.t0 - b.t0);
    selSub = c.id;
    subsChanged();
    paintList();
    const span = $(`[data-sub-text="${c.id}"]`);
    if (span) { span.focus(); document.getSelection().selectAllChildren(span); }
  }
  function splitSub(id) {
    const k = subIndex(id), c = subs[k];
    if (!c) return;
    let t = video.currentTime;
    if (!(t - c.t0 >= SUB_MIN && c.t1 - t >= SUB_MIN)) {         // playhead not inside: at a word near the middle
      const mid = (c.t0 + c.t1) / 2, starts = (D.sub_words || []).map(w => w[0]).filter(w => w - c.t0 >= SUB_MIN && c.t1 - w >= SUB_MIN);
      t = starts.length ? starts.reduce((a, b) => (Math.abs(b - mid) < Math.abs(a - mid) ? b : a)) : mid;
    }
    if (t - c.t0 < SUB_MIN || c.t1 - t < SUB_MIN) { flash("This line is too short to split."); return; }
    const words = c.text.split(" "), f = (t - c.t0) / (c.t1 - c.t0);
    let cut = Math.min(Math.max(1, Math.round(words.length * f)), words.length - 1);
    if (words.length < 2) cut = 1;
    pushUndo();
    const a = words.slice(0, cut).join(" ") || c.text, b = words.slice(cut).join(" ") || "…";
    subs.splice(k, 1, { ...c, t1: r3(t - 0.02), text: a, by: "you" },
                { id: "u" + Math.random().toString(36).slice(2, 8), t0: r3(t), t1: c.t1, text: b, by: "you" });
    selSub = subs[k + 1].id;
    subsChanged();
  }
  function joinSub(id) {
    const k = subIndex(id), c = subs[k], n = subs[k + 1];
    if (!c || !n) { flash("There's no next line to join."); return; }
    pushUndo();
    subs.splice(k, 2, { ...c, t1: n.t1, text: (c.text + " " + n.text).trim(), by: "you" });
    selSub = c.id;
    subsChanged();
  }
  function deleteSub(id) {
    const k = subIndex(id);
    if (k < 0) return;
    pushUndo();
    subs.splice(k, 1);
    selSub = null;
    subsChanged();
  }
  function setSubText(id, text) {
    const c = subs.find(x => x.id === id);
    text = text.replace(/\s+/g, " ").trim();
    if (!c || text === c.text) return;
    if (!text) { deleteSub(id); return; }
    const learnt = newNames(c.text, text);
    pushUndo();
    Object.assign(c, { text, by: "you" });
    subsChanged();
    if (learnt.length) addNames(learnt);
  }
  // Names you fix by retyping a line (a capitalised word that isn't a sentence's first, or
  // one like holo1GP or CeCe) are remembered for the channel, so Claude spells them right.
  const NOT_NAMES = new Set(["i'm", "i'll", "i've", "i'd", "okay", "oh", "yeah", "hey", "wait", "what", "why", "god"]);
  function newNames(before, after) {
    const bare = w => w.replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, "");
    const had = new Set(before.replace(/\*/g, "").split(/\s+/).map(w => bare(w).toLowerCase()));
    const ws = after.replace(/\*/g, "").split(/\s+/), out = [];
    ws.forEach((raw, i) => {
      const w = bare(raw);
      if (w.length < 3 || had.has(w.toLowerCase()) || NOT_NAMES.has(w.toLowerCase())) return;
      const special = (/\d/.test(w) && /\p{L}/u.test(w)) || /\p{Ll}\p{Lu}/u.test(w);
      const first = i === 0 || /[.!?…]$/.test(ws[i - 1]);
      if (special || (/^\p{Lu}/u.test(w) && !first)) out.push(w);
    });
    return out;
  }
  async function addNames(terms) {
    try {
      const d = await post(`/moment/${pk}/names`, { add: terms });
      D.names = d.names;
      paintNames();
      if (d.added.length) subStatus(`Remembered for this channel: ${d.added.join(", ")}. Claude will spell ${d.added.length === 1 ? "it" : "them"} like that.`);
    } catch (e) { /* not worth interrupting your typing */ }
  }
  async function saveNames() {
    try {
      const d = await post(`/moment/${pk}/names`, { terms: $("[data-ed-names-text]").value });
      D.names = d.names;
      paintNames();
      subStatus(`Saved ${d.names.length} name${d.names.length === 1 ? "" : "s"} for this channel.`);
    } catch (e) { subStatus("Couldn't save the names: " + e.message); }
  }
  function paintNames() {
    $("[data-ed-names-count]").textContent = D.names.length;
    const ta = $("[data-ed-names-text]");
    if (document.activeElement !== ta) ta.value = D.names.join("\n");
  }
  async function polishSubs() {
    const b = $('[data-ed-act="sub-polish"]');
    b.disabled = true;
    b.dataset.busy = "1";
    clearTimeout(saveTimer);
    subStatus("Claude is going over the lines… (usually 15–60 s)");
    try {
      const d = await post(`/moment/${pk}/subs/polish`, body());
      pushUndo();
      subs = d.subs;
      subsMine = true;
      selSub = null;
      listFor = null;
      subKey = "";
      D.names = d.names;
      dirty = false;
      D.saved = true;
      setSaved("Saved");
      paintAll();
      paintNames();
      showRender(d.render);
      const bits = [`Claude changed ${d.changed} line${d.changed === 1 ? "" : "s"}`];
      if (d.joined) bits.push(`joined ${d.joined}`);
      if (d.dropped) bits.push(`dropped ${d.dropped}`);
      subStatus(bits.join(", ") + ". " + (d.note || "") + (d.names_added.length ? ` New names remembered: ${d.names_added.join(", ")}.` : "")
                + " Ctrl+Z undoes.");
    } catch (e) {
      subStatus(e.message);
    } finally {
      b.dataset.busy = "";
      b.disabled = false;
    }
  }
  function setSubTime(id, edge, t) {
    const k = subIndex(id), c = subs[k];
    if (!c) return;
    const lo = edge === "t0" ? (subs[k - 1] ? subs[k - 1].t1 : 0) : c.t0 + SUB_MIN;
    const hi = edge === "t0" ? c.t1 - SUB_MIN : (subs[k + 1] ? subs[k + 1].t0 : D.duration);
    pushUndo();
    c[edge] = r3(Math.min(Math.max(t, lo), hi));
    c.by = "you";
    seek(edge === "t0" ? c.t0 : Math.max(c.t0, c.t1 - 0.05));
    subsChanged();
  }
  function rebuildSubs() {
    if (!D.subs_auto.length) { subStatus("No Whisper words for this clip yet: press 🎙 Transcribe first."); return; }
    if (subsMine && !confirm(`Replace your ${subs.length} subtitle line(s) with fresh ones from Whisper's words? (Undo brings yours back.)`)) return;
    pushUndo();
    subs = cloneFx(D.subs_auto);
    selSub = null;
    subsMine = false;           // they follow the transcript again
    listFor = null;
    subKey = "";
    changed();
  }
  function setSubMode(mode) {
    // Lines that follow Whisper switch to the other way of cutting them; your own lines stay.
    const auto = (D.subs_auto_by_mode || {})[mode];
    pushUndo();
    subStyle.mode = mode;
    if (!subsMine && auto) { subs = cloneFx(auto); D.subs_auto = auto; selSub = null; listFor = null; }
    else if (auto) { D.subs_auto = auto; subStatus("Your own lines stay as they are; ↻ From transcript rebuilds them " + mode + " at a time."); }
    subKey = "";
    changed();
  }
  function setSubStyle(change) {
    pushUndo();
    Object.assign(subStyle, change);
    subKey = "";
    changed();
  }

  // ── the side panel's tabs, the lane labels, the ⋯ menus ──────────────────
  function showTab(name) {
    root.querySelectorAll("[data-ed-tab]").forEach(b => {
      b.classList.toggle("on", b.dataset.edTab === name);
      b.setAttribute("aria-selected", b.dataset.edTab === name ? "true" : "false");
    });
    root.querySelectorAll("[data-ed-pane]").forEach(p => { p.hidden = p.dataset.edPane !== name; });
  }
  function paintLanes() {
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
  let picked = "";            // what the Selected tab shows; the tab opens when that changes
  function followSelection() {
    const now = regionMode ? "regions" : selSub ? "sub:" + selSub : selFx ? "fx:" + selFx : "seg:" + sel;
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

  // ── panels ───────────────────────────────────────────────────────────────
  function paintAll() {
    needsDraw = true;
    paintTotal();
    paintChips();
    if (regionMode) paintRegionPanel();
    else if (selSub && curSub()) paintSubPanel();
    else if (selFx && curFx()) (isVis(curFx()) ? paintVisPanel() : paintFxPanel());
    else { selFx = null; selSub = null; paintSelected(); }
    paintButtons();
    paintList();
    paintSubBar();
    paintLayout();
    followSelection();
  }
  function paintTotal() {
    const n = segs.length;
    let text = `Your edit: ${fmt(total(), 1)} in ${n} segment${n === 1 ? "" : "s"}`;
    const sounds = fx.filter(f => f.type === "sfx" && kept(f.t)).length, pics = fx.filter(f => isVis(f) && keptFx(f)).length;
    if (sounds) text += ` · ${sounds} sound${sounds === 1 ? "" : "s"}`;
    if (pics) text += ` · ${pics} zoom/shake/flash`;
    if (subs.length && subStyle.on) text += ` · ${subs.length} subtitle line${subs.length === 1 ? "" : "s"}`;
    const p = D.presets.long;
    if (p && source !== "long") text += ` · Claude's cut ${fmt(p[0][1] - p[0][0], 1)}`;
    $("[data-ed-total]").textContent = text;
  }
  function paintChips() {
    const box = $("[data-ed-chips]");
    box.textContent = "";
    segs.forEach((s, i) => {
      const b = btn("", "Select segment " + (i + 1), { seg: i });
      b.className = "chip ed-chip" + (i === sel && !selFx ? " current" : "");
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
  function paintSelected() {
    const box = $("[data-ed-selected]");
    box.textContent = "";
    const s = segs[sel];
    if (!s) return;
    const head = el("div", "ed-sel-head");
    head.append(el("b", "", `Segment ${sel + 1}`), ` of ${segs.length} · ${(s[1] - s[0]).toFixed(2)} s`);
    box.appendChild(head);
    const f = 1 / (D.fps || 30);
    box.appendChild(edgeRow("Start", s[0], "nudge0", "Start at the playhead (I)", f));
    box.appendChild(edgeRow("End", s[1], "nudge1", "End at the playhead (O)", f));
    const acts = el("div", "actions ed-sel-acts");
    const gain = select(GAINS.map(g => [g, (g > 0 ? "+" : "") + g + " dB" + (g === 0 ? " (as is)" : "")]), s[2] || 0,
                        { gain: "" }, "Volume of this segment");
    const gl = el("label", "muted");
    gl.append(el("small", "", "volume "), gain);
    if (!cutInto(sel)) {
      box.appendChild(el("small", "muted", "Into this segment"));
      box.appendChild(el("small", "faint", sel === 0
        ? "This is where the edit starts, so there's no cut before it to make a transition on. Pick segment 2 or later."
        : `It plays straight on from segment ${sel} (no cut), so it can't have a transition.`));
    } else {
      const tr = transOf(s);
      box.appendChild(el("small", "muted", "Into this segment"));
      const row = el("div", "ed-region-chips");
      for (const [style, label, tip] of [["", "cut", "A straight cut"]].concat(
        Object.entries(D.visual.transitions).map(([k, [, desc]]) => [k, k, desc]))) {
        const b = btn(label, tip, { transStyle: style });
        b.className = "chip ed-rchip" + ((tr ? tr.style : "") === style ? " current" : "");
        row.appendChild(b);
      }
      box.appendChild(row);
    }
    const shown = segShots(s)[0];
    box.appendChild(el("small", "muted", "On screen in this segment"));
    box.appendChild(regionChips(shown ? shown.region : "", { chipFor: "seg" }, kind === "short" ? "the layout" : "whole frame"));
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
  const segShotType = () => (kind === "short" ? "frame" : "punch");
  function segShots(s) {      // in a Short every frame shot on the segment (yours or Claude's); long: your fill zooms
    return fx.filter(f => f.type === segShotType() && (kind === "short" || (f.why || "").startsWith(SEG_WHY))
                          && f.t < s[1] - 0.05 && f.t + f.d > s[0] + 0.05);
  }
  // A segment's transition: the effect at its start (clip time), only on a real cut.
  const cutInto = i => i > 0 && segs[i] && Math.abs(segs[i][0] - segs[i - 1][1]) >= 0.002;
  const transOf = s => fx.find(f => f.type === "transition" && Math.abs(f.t - s[0]) < 0.05);
  function setTransition(style) {
    const s = segs[sel];
    if (!s || !cutInto(sel)) return;
    pushUndo();
    fx = fx.filter(f => !(f.type === "transition" && Math.abs(f.t - s[0]) < 0.05));
    if (style) fx.push({ id: "u" + Math.random().toString(36).slice(2, 8), type: "transition", t: r3(s[0]), style,
                         d: D.visual.transitions[style][0], by: "you", why: "" });
    changed();
    if (style) playEdit(sel - 1, false, Math.max(segs[sel - 1][0], segs[sel - 1][1] - 1.2));   // show it
  }
  function setSegRegion(name) {
    const s = segs[sel];
    if (!s) return;
    pushUndo();
    const old = new Set(segShots(s));
    fx = fx.filter(f => !old.has(f));
    if (name) {
      const type = segShotType(), longest = D.visual.types[type][2];
      for (let t = s[0]; t < s[1] - 0.05; t += longest) {        // longer than one shot may last: a chain of them
        const f = { id: "u" + Math.random().toString(36).slice(2, 8), type, t: r3(t), d: r3(Math.min(longest, s[1] - t)),
                    region: name, by: "you", why: SEG_WHY + name };
        if (type === "punch") f.zoom = Math.min(fitZoom(name), 3);
        fx.push(f);
      }
    }
    selFx = null;
    changed();
  }
  function regionChips(current, data, none) {
    const box = el("div", "ed-region-chips");
    const opts = (none ? [["", none]] : []).concat(Object.keys(D.regions).map(n => [n, n]));
    for (const [val, text] of opts) {
      const b = btn(text, val ? `Show ${val}` : none, { ...data, chipRegion: val });
      b.className = "chip ed-rchip" + (String(current || "") === val ? " current" : "");
      if (val) b.style.setProperty("--rc", regionCol(val));
      box.appendChild(b);
    }
    if (!Object.keys(D.regions).length)
      box.appendChild(btn("▢ Draw regions first", "Draw the boxes the camera can show, once per channel", { edAct: "regions" }));
    return box;
  }
  function paintFxPanel() {
    const box = $("[data-ed-selected]");
    box.textContent = "";
    const f = curFx(), snd = sound(f.sound);
    const head = el("div", "ed-sel-head");
    head.append(el("b", "", "Sound effect"), f.by === "claude" ? " · placed by Claude" : " · yours");
    if (!kept(f.t)) head.append(el("span", "ed-warn", " · outside your cut, so it won't play"));
    box.appendChild(head);
    if (f.why) box.appendChild(el("p", "ed-why", f.why));
    const pick = el("div", "actions");
    const options = D.sounds.map(s => [s.id, s.name + " · " + s.length.toFixed(1) + " s"]);
    if (!snd) options.unshift([f.sound, f.sound + " (missing)"]);
    pick.append(select(options, f.sound, { fxSound: "" }, "Sound"),
                btn("▶ Hear it", "Play just this sound", { fxHear: "" }));
    box.appendChild(pick);
    if (snd && snd.description) box.appendChild(el("p", "faint ed-why", snd.description));
    box.appendChild(edgeRow("At", f.t, "fxNudge", "Move it to the playhead", 1 / (D.fps || 30)));
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
  function paintVisPanel() {
    const box = $("[data-ed-selected]");
    box.textContent = "";
    const f = curFx();
    const head = el("div", "ed-sel-head");
    head.append(el("b", "", VLABEL[f.type]), f.by === "claude" ? " · placed by Claude" : " · yours");
    if (!keptFx(f)) head.append(el("span", "ed-warn", " · outside your cut, so it won't show"));
    box.appendChild(head);
    if (f.why) box.appendChild(el("p", "ed-why", f.why));
    box.appendChild(edgeRow("At", f.t, "fxNudge", "Move it to the playhead", 1 / (D.fps || 30)));
    const acts = el("div", "actions ed-sel-acts");
    const labelled = (text, control, title) => {
      const l = el("label", "muted");
      if (title) l.title = title;
      l.append(el("small", "", text + " "), control);
      return l;
    };
    acts.appendChild(labelled("length", select(LENGTHS[f.type].map(v => [v, v + " s"]), f.d, { visD: "" }, "How long it lasts",
                                                v => nice(v) + " s")));
    if (ZOOMS.includes(f.type)) {
      const names = Object.keys(D.regions);
      const opts = names.map(n => [n, n]).concat([["", "centre of the frame"]]);
      if (f.region && !names.includes(f.region)) opts.unshift([f.region, f.region + " (no such region: centre)"]);
      box.appendChild(regionChips(f.region || "", { chipFor: "vis" }, "centre"));
      acts.appendChild(labelled(f.type === "push" ? "ends at" : "zoom",
                                select(ZOOM_STEPS[f.type].map(v => [v, v + "×"]), f.zoom, { visZoom: "" }, "How far it zooms", v => nice(v) + "×")));
      if (f.region && D.regions[f.region])
        acts.appendChild(btn("⤢ fill", `Zoom so ${f.region} fills the frame (${nice(fitZoom(f.region))}×)`, { visFit: "" }));
    } else if (f.type === "frame") {
      const names = Object.keys(D.regions);
      box.appendChild(regionChips(f.region || "", { chipFor: "vis" }, ""));
    } else if (f.type === "shake") {
      acts.appendChild(labelled("strength", select([[1, "light"], [2, "normal"], [3, "heavy"]], f.strength, { visStrength: "" }, "How strong")));
    } else {
      acts.appendChild(labelled("colour", select(Object.keys(D.visual.flash).map(c => [c, c]), f.color, { visColor: "" }, "Flash colour", v => v)));
    }
    box.appendChild(acts);
    const more = el("div", "actions ed-sel-acts");
    const d = btn("Delete", "Delete this effect (Del)", { edAct: "delete" });
    d.classList.add("danger");
    more.append(btn("▶ Play it", "Play from just before it to just after it", { edAct: "fx-play" }), d);
    box.appendChild(more);
    if (ZOOMS.includes(f.type) && !Object.keys(D.regions).length)
      box.appendChild(el("p", "faint ed-why", "No regions for this channel yet, so it zooms on the centre. ⋯ More → ▢ Draw regions lets you draw her face once."));
  }

  function paintSubPanel() {
    const box = $("[data-ed-selected]");
    box.textContent = "";
    const c = curSub(), k = subIndex(c.id);
    const head = el("div", "ed-sel-head");
    head.append(el("b", "", `Subtitle ${k + 1}`), ` of ${subs.length} · ${(c.t1 - c.t0).toFixed(2)} s · `
                + ({ you: "yours", claude: "Claude's", auto: "from Whisper's words" })[c.by]);
    box.appendChild(head);
    const input = el("input");
    Object.assign(input, { type: "text", value: c.text, maxLength: 200, className: "ed-sub-input" });
    input.dataset.subInput = c.id;
    input.setAttribute("aria-label", "Subtitle text");
    input.title = "*stars* around a word make it yellow";
    box.appendChild(input);
    const f = 1 / (D.fps || 30);
    box.appendChild(edgeRow("Start", c.t0, "subNudge0", "Start it at the playhead", f));
    box.appendChild(edgeRow("End", c.t1, "subNudge1", "End it at the playhead", f));
    const acts = el("div", "actions ed-sel-acts");
    const d = btn("Delete", "Delete this line", { subDel: c.id });
    d.classList.add("danger");
    acts.append(btn("✂ Split", "Split it at the playhead (or at a word in the middle)", { subSplit: c.id }),
                btn("⤓ Join next", "Join it with the next line", { subJoin: c.id }), d);
    box.appendChild(acts);
    if (subWidth(c.text) > 2 * D.sub_layout.max_line)
      box.appendChild(el("p", "ed-warn ed-why", "Long for two lines on screen: split it, or shorten it."));
  }
  function subStatus(text) { $("[data-ed-sub-status]").textContent = text; }
  function paintSubBar() {
    $("[data-sub-on]").checked = subStyle.on;
    $("[data-sub-size]").value = subStyle.size;
    $("[data-sub-pos]").value = subStyle.position;
    $("[data-sub-mode]").value = subStyle.mode || "lines";
    $('[data-ed-act="sub-rebuild"]').disabled = !D.subs_auto.length;
    const pb = $('[data-ed-act="sub-polish"]');
    pb.disabled = !subs.length || pb.dataset.busy === "1";
    pb.textContent = D.sub_language && D.sub_language !== "en" ? "✨ Fix & translate" : "✨ Clean up";
    if (!subs.length) {
      subStatus(D.transcript.source === "whisper" ? "No subtitle lines: + Line adds one, ↻ From transcript brings Whisper's back."
        : "Subtitles are made from Whisper's words: press 🎙 Transcribe above the transcript first.");
      return;
    }
    const lang = D.sub_language && D.sub_language !== "en" ? ` · in ${LANG[D.sub_language] || D.sub_language}: Claude's translation comes in the next step` : "";
    subStatus(`${subs.length} line${subs.length === 1 ? "" : "s"}` + (subsMine ? " · edited by you" : " from Whisper's words")
      + lang + (subStyle.on ? "" : " · not burned in (the .srt is still made)")
      + " · click a line on the right to retype it.");
  }

  // ── the Short's layout panel, and the Long / Short switch ────────────────
  function paintLayout() {
    const box = $("[data-ed-layout]");
    const tab = $("[data-ed-tab-short]");
    tab.hidden = kind !== "short" || !layout;
    if (tab.hidden && tab.classList.contains("on")) showTab("sel");
    box.hidden = kind !== "short" || !layout;
    if (box.hidden) return;
    root.querySelectorAll("[data-lay-type]").forEach(b => b.classList.toggle("on", b.dataset.layType === layout.type));
    root.querySelectorAll("[data-lay-for]").forEach(r => { r.hidden = r.dataset.layFor !== layout.type; });
    for (const inp of box.querySelectorAll("[data-lay]")) {
      const k = inp.dataset.lay;
      if (inp.tagName === "SELECT") {
        const names = ["", ...Object.keys(D.regions)];
        if (layout[k] && !names.includes(layout[k])) names.push(layout[k]);
        const want = names.join("|") + "#" + Object.keys(D.regions).join("|");
        if (inp.dataset.names !== want) {
          inp.textContent = "";
          for (const n of names) {
            const o = el("option", "", n ? n + (D.regions[n] ? "" : " (not drawn)") : "whole frame");
            o.value = n;
            inp.appendChild(o);
          }
          inp.dataset.names = want;
        }
        inp.value = layout[k] || "";
      } else if (document.activeElement !== inp) inp.value = layout[k] ?? "";
    }
    const cen = $("[data-lay-centre]");
    cen.textContent = "";
    for (const n of Object.keys(D.regions)) cen.appendChild(btn("◎ " + n, `Centre the crop on the ${n} box`, { layCentre: n }));
    const tt = $("[data-lay-titles]");
    tt.textContent = "";
    (D.titles || []).forEach((t, i) => tt.appendChild(btn(`Claude's #${i + 1}`, t, { layTitle: i })));
    const hint = [];
    if (layout.type === "stack" && !Object.keys(D.regions).length)
      hint.push("Draw a face box (and a game box) with ⋯ More → ▢ Draw regions: until then each half shows the whole frame.");
    if (layout.type !== "stack" && D.vertical_crop) hint.push("Claude: " + D.vertical_crop);
    $("[data-lay-hint]").textContent = hint.join(" ");
  }
  function setLayout(change) {
    pushUndo();
    Object.assign(layout, change);
    subKey = "";
    changed();
  }
  function paintKind() {
    root.classList.toggle("ed-is-short", kind === "short");
    root.querySelectorAll("[data-ed-kind]").forEach(b => {
      b.classList.toggle("on", b.dataset.edKind === kind);
      b.setAttribute("aria-selected", b.dataset.edKind === kind ? "true" : "false");
    });
    $('[data-ed-act="render"]').textContent = kind === "short" ? "Render Short" : "Render edit";
    const other = D.other || {}, made = s => s === "done" || s === "stale";
    $("[data-ed-kind-note]").textContent = kind === "short"
      ? (made(other.long) ? "edit.mp4 is rendered too" : "")
      : (made(other.short) ? "short.mp4 is rendered too" : "");
  }
  async function fetchEdit(k) {
    try {
      return await (await fetch(`/api/moment/${pk}/edit?kind=${k}`, { cache: "no-store" })).json();
    } catch (e) {
      return { ok: false, error: "Couldn't reach the dashboard. Is cw serve still running?" };
    }
  }
  // Take one edit's data (long or short) as the editor's state.
  function apply(d) {
    D = d;
    kind = d.kind || "long";
    segs = clone(d.segments);
    fx = cloneFx(d.effects || []);
    source = d.source;
    subs = cloneFx(d.subs || []);
    subStyle = { ...d.subs_style };
    subsMine = d.subs_source === "saved";
    layout = d.layout ? { ...d.layout } : null;
    undo = []; sel = 0; selFx = null; selSub = null; subKey = ""; listFor = null; dirty = false; layEditing = false;
    $('[data-ed-act="undo"]').disabled = true;
    paintKind();
  }
  async function switchKind(k) {
    if (!D || k === kind) return;
    if (regionMode) toggleRegions(false);
    video.pause();
    stopEditPlay();
    clearTimeout(saveTimer);
    while (saving) await new Promise(r => setTimeout(r, 100));
    if (dirty) await save();
    if (dirty) return;                      // couldn't save: stay, the message says why
    const d = await fetchEdit(k);
    if (!d.ok) { setSaved(d.error || "Couldn't load the edit.", true); return; }
    try { localStorage.setItem("cw-ed-kind", k); } catch (e) {}
    apply(d);
    resize();
    paintAll();
    showRender(d.render);
    if (d.render.state === "queued" || d.render.state === "running") pollRender();
    setSaved(d.saved ? "Saved" : k === "short"
      ? `New Short from ${d.source === "short" ? "Claude's short cut" : d.source === "long" ? "Claude's cut" : "the whole clip"}, with your edit's effects and subtitles`
      : (source === "long" ? "Claude's cut, snapped to pauses" : ""));
    if (segs[0]) seek(segs[0][0]);
    needsDraw = true;
  }
  video.parentElement.addEventListener("click", ev => {        // the Short's preview covers the video's controls
    if (isShort() && ev.target === video) { hold(); toggle(); }
  });
  root.addEventListener("input", ev => {
    const t = ev.target;
    if (!t.matches("[data-lay]") || t.tagName === "SELECT" || !layout) return;
    hold();
    if (!layEditing) { pushUndo(); layEditing = true; }
    layout[t.dataset.lay] = t.type === "range" ? +t.value : t.value;
    subKey = "";
    needsDraw = true;
  });

  // ── regions: boxes on the picture that zooms aim at, shared by every stream of the channel ──
  function toggleRegions(onOff = !regionMode) {
    regionMode = onOff;
    root.classList.toggle("ed-regmode", regionMode);
    video.pause();
    stopEditPlay();
    regLayer.hidden = !regionMode;
    subKey = "";
    if (regionMode) {
      selFx = null;
      if (!regSel || !D.regions[regSel]) regSel = Object.keys(D.regions)[0] || null;
      paintRegions();
    }
    paintAll();
    drawStage();
  }
  function paintRegions() {
    if (!regionMode) return;
    place(regLayer, pictureBox());
    regLayer.textContent = "";
    for (const [name, b] of Object.entries(D.regions)) {
      const r = el("div", "ed-region" + (name === regSel ? " on" : ""));
      r.dataset.region = name;
      Object.assign(r.style, { left: b.x * 100 + "%", top: b.y * 100 + "%", width: b.w * 100 + "%", height: b.h * 100 + "%" });
      r.append(el("span", "", name), el("i"));
      regLayer.appendChild(r);
    }
  }
  function paintRegionPanel() {
    const box = $("[data-ed-selected]");
    box.textContent = "";
    const head = el("div", "ed-sel-head");
    head.append(el("b", "", "Regions"), ` · for every stream of ${D.region_scope}`);
    box.appendChild(head);
    box.appendChild(el("p", "ed-why", Object.keys(D.regions).length
      ? "Drag a box to move it, its corner to resize it, or drag on the picture for a new one. Zooms aim at the centre of their region."
      : "Drag on the picture to draw a box around her face. Zooms aim at it, in this clip and every later one from this channel."));
    for (const name of Object.keys(D.regions)) {
      const row = el("div", "ed-reg-row" + (name === regSel ? " on" : ""));
      const input = el("input");
      Object.assign(input, { type: "text", value: name, maxLength: 20 });
      input.dataset.regName = name;
      input.setAttribute("aria-label", "Region name");
      const used = fx.filter(f => ZOOMS.includes(f.type) && f.region === name).length;
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
        const d = await post(`/moment/${pk}/regions`, { regions: Object.entries(D.regions).map(([name, b]) => ({ name, ...b })), kind });
        D.regions = d.regions;
        setSaved("Regions saved");
        showRender(d.render);
        if ($("[data-ed-fx-status]").textContent.includes("Draw regions")) libraryStatus();   // drop the "draw her face" hint
        paintButtons();
      } catch (e) { setSaved("Couldn't save the regions: " + e.message, true); }
    }, 350);
  }
  function renameRegion(old, raw) {
    const name = raw.trim().toLowerCase().replace(/\s+/g, " ");
    if (!name || name === old) { paintRegionPanel(); return; }
    if (!/^[a-z0-9][a-z0-9 _-]{0,19}$/.test(name)) { flash("Use letters, numbers, spaces, - or _ (up to 20)."); paintRegionPanel(); return; }
    if (D.regions[name]) { flash(`There's already a region called ${name}.`); paintRegionPanel(); return; }
    D.regions = Object.fromEntries(Object.entries(D.regions).map(([n, b]) => [n === old ? name : n, b]));
    if (fx.some(f => ZOOMS.includes(f.type) && f.region === old)) {        // this edit's zooms follow it
      pushUndo();
      fx.forEach(f => { if (ZOOMS.includes(f.type) && f.region === old) f.region = name; });
      changed();
    }
    regSel = name;
    saveRegions();
    paintRegions();
    paintAll();
  }
  function deleteRegion(name) {
    delete D.regions[name];
    if (regSel === name) regSel = Object.keys(D.regions)[0] || null;
    saveRegions();
    paintRegions();
    paintAll();
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
      const name = boxEl.dataset.region, b = D.regions[name];
      regSel = name;
      regDrag = { name, mode: ev.target.tagName === "I" ? "size" : "move", px, py, b0: { ...b } };
    } else {
      if (Object.keys(D.regions).length >= 8) { flash("At most 8 regions."); return; }
      let name = REGION_NAMES.find(n => !D.regions[n]);
      for (let k = 1; !name; k++) if (!D.regions["region " + k]) name = "region " + k;
      D.regions[name] = { x: px, y: py, w: 0, h: 0 };
      regSel = name;
      regDrag = { name, mode: "new", px, py };
    }
    paintRegions();
    paintRegionPanel();
  });
  regLayer.addEventListener("pointermove", ev => {
    if (!regDrag) return;
    const [px, py] = frac(ev), b = D.regions[regDrag.name], d = regDrag;
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
    const d = regDrag, b = D.regions[d.name];
    regDrag = null;
    if (d.mode === "new" && (!b || b.w < 0.03 || b.h < 0.03)) {      // a click, not a box
      delete D.regions[d.name];
      regSel = Object.keys(D.regions)[0] || null;
      paintRegions();
      paintRegionPanel();
      return;
    }
    for (const k of ["x", "y", "w", "h"]) b[k] = Math.round(b[k] * 10000) / 10000;
    saveRegions();
    paintRegions();
    paintAll();
  }
  regLayer.addEventListener("pointerup", endRegionDrag);
  regLayer.addEventListener("pointercancel", endRegionDrag);

  function paintButtons() {
    const rb = $('[data-ed-act="regions"]');
    const nr = Object.keys(D.regions).length;
    rb.textContent = regionMode ? "✓ Done with regions" : nr ? `▢ Draw regions (${nr})` : "▢ Draw regions";
    rb.classList.toggle("on", regionMode);
    const b = $('[data-ed-act="play-edit"]');
    b.textContent = editPlay && !editPlay.only ? "⏹ Stop" : "▶ Play edit";
    root.querySelectorAll("[data-ed-preset]").forEach(p => {
      const has = !!D.presets[p.dataset.edPreset];
      p.disabled = !has;
      p.classList.toggle("on", has && source === p.dataset.edPreset);
      if (!has) p.title = "Claude didn't suggest one for this clip";
    });
    $('[data-ed-act="clear-claude"]').disabled = !fx.some(f => f.by === "claude");
  }
  // The list beside the player: your subtitle lines (click one to retype it) once there
  // are any, otherwise the transcript. ⇤ / ⇥ start or end the selected segment at a line.
  const listMode = () => (subs.length ? "subs" : "lines");
  function paintList() {
    const box = $("[data-ed-lines]");
    if (box.contains(document.activeElement)) return;      // not under your cursor; again after the edit
    const mode = listMode();
    const key = mode === "subs" ? JSON.stringify(subs.map(c => [c.id, c.t0, c.t1, c.text, c.by, c.id === selSub])) : "lines:" + D.lines.length;
    if (key === listFor) return;
    listFor = key;
    box.textContent = "";
    lastLine = -1;
    if (mode === "lines") {
      const head = el("div", "ed-list-head");
      if (D.lines.length) {
        head.append(el("small", "muted", "The transcript, as it came. To change times or add lines, edit them as subtitle lines:"),
          btn("✎ Edit as lines", "Turn these into subtitle lines you can retime, retype, add to and delete", { edAct: "lines-edit" }));
      } else {
        head.append(el("small", "muted", "No transcript for this clip. Add lines yourself:"),
          btn("+ Line at playhead", "A new subtitle line at the playhead", { edAct: "sub-add" }));
      }
      box.appendChild(head);
      D.lines.forEach((l, k) => {
        const row = el("div", "ed-line");
        row.dataset.k = k;
        const t = btn(fmt(l.t, 1), "Jump here", { line: k });
        t.className = "ed-lt mono";
        const txt = el("span", "ed-lx", l.text);
        txt.dataset.line = k;
        row.append(t, txt, btn("⇤", "Start the selected segment at this line", { lineStart: k }),
          btn("⇥", "End the selected segment after this line", { lineEnd: k }));
        box.appendChild(row);
      });
      return;
    }
    subs.forEach((c, k) => {
      const row = el("div", "ed-line ed-sub-row" + (c.id === selSub ? " sel" : "") + (c.by === "you" ? " mine" : ""));
      row.dataset.k = k;
      row.dataset.sub = c.id;
      const times = el("div", "ed-times");
      for (const edge of ["t0", "t1"]) {
        const inp = el("input", "ed-tin mono" + (edge === "t1" ? " end" : ""));
        Object.assign(inp, { type: "text", value: fmt(c[edge], 2), spellcheck: false, autocomplete: "off" });
        inp.dataset.subT = c.id;
        inp.dataset.edge = edge;
        inp.title = edge === "t0" ? "Start: type a time (1:02.5 or 62.5), Enter to apply, Esc to cancel"
                                  : "End: type a time, Enter to apply, Esc to cancel";
        inp.setAttribute("aria-label", (edge === "t0" ? "Start of: " : "End of: ") + c.text);
        times.appendChild(inp);
      }
      const txt = el("span", "ed-lx", c.text);
      txt.contentEditable = "plaintext-only";
      if (txt.contentEditable !== "plaintext-only") txt.contentEditable = "true";
      txt.dataset.subText = c.id;
      txt.title = "Click to retype. Enter keeps it, Esc undoes it. *stars* around a word make it yellow.";
      const acts = el("span", "ed-line-acts");
      acts.append(btn("+", "A new line after this one", { subAfter: c.id }),
        Object.assign(btn("×", "Delete this line", { subDel: c.id }), { className: "small danger" }),
        btn("⇤", "Start the selected segment at this line", { subStart: c.id }),
        btn("⇥", "End the selected segment after this line", { subEnd: c.id }));
      row.append(times, txt, acts);
      box.appendChild(row);
    });
  }
  // m:ss.ss, ss.ss or h:mm:ss → seconds; NaN if it isn't a time.
  function parseTime(text) {
    const m = String(text).trim().replace(",", ".").match(/^(?:(?:(\d+):)?(\d+):)?(\d+(?:\.\d*)?)$/);
    return m ? (+(m[1] || 0)) * 3600 + (+(m[2] || 0)) * 60 + parseFloat(m[3]) : NaN;
  }
  // A typed start or end. A start past the line's end moves the whole line; an overlap with
  // another line is trimmed off, or refused when nothing would be left. Returns an error or "".
  function retimeSub(id, edge, t) {
    const c = subs.find(x => x.id === id);
    if (!c) return "That line is gone.";
    if (!isFinite(t)) return "Type a time like 1:02.5 or 62.5.";
    let t0 = c.t0, t1 = c.t1;
    if (edge === "t0") { t0 = t; if (t0 > t1 - SUB_MIN) t1 = t0 + (c.t1 - c.t0); }
    else { t1 = t; if (t1 < t0 + SUB_MIN) return `The end must be after the start (${fmt(t0, 2)}).`; }
    t0 = Math.min(Math.max(t0, 0), D.duration); t1 = Math.min(Math.max(t1, 0), D.duration);
    const want = [t0, t1];
    for (const o of subs) {
      if (o === c || !(o.t0 < t1 && o.t1 > t0)) continue;
      if (o.t0 <= t0) t0 = o.t1; else t1 = o.t0;          // keep the part before / after it
      if (t1 - t0 < SUB_MIN)
        return `That overlaps the line at ${fmt(o.t0, 1)} ("${o.text.slice(0, 30)}"): move or shorten it first.`;
    }
    pushUndo();
    c.t0 = r3(t0); c.t1 = r3(t1); c.by = "you";
    subs.sort((a, b) => a.t0 - b.t0);
    selSub = c.id;
    seek(edge === "t0" ? c.t0 : Math.max(c.t0, c.t1 - 0.05));
    subsChanged();
    if (Math.abs(want[0] - t0) > 0.005 || Math.abs(want[1] - t1) > 0.005)
      listFlash(`Trimmed to ${fmt(t0, 2)}–${fmt(t1, 2)} so it doesn't overlap the lines around it.`);
    return "";
  }
  function addSubAfter(id) {
    const k = subIndex(id), c = subs[k];
    if (!c) return;
    const next = subs[k + 1];
    let t0 = c.t1 + 0.04, t1 = Math.min(t0 + 2, next ? next.t0 - 0.04 : D.duration);
    let cut = null;                    // no gap after it: the new line takes this line's second half
    if (t1 - t0 < 0.3) {
      if (c.t1 - c.t0 < 2 * SUB_MIN + 0.3) { listFlash("This line is too short to make room after it: type an earlier end on it or the next one."); return; }
      cut = Math.max(c.t0 + (c.t1 - c.t0) / 2, c.t1 - 2);
      t0 = cut + 0.04; t1 = c.t1;
    }
    pushUndo();
    if (cut !== null) { c.t1 = r3(cut); c.by = "you"; }
    const n = { id: "u" + Math.random().toString(36).slice(2, 8), t0: r3(t0), t1: r3(t1), text: "…", by: "you" };
    subs.splice(k + 1, 0, n);
    selSub = n.id;
    seek(n.t0);
    subsChanged();
    paintList();
    const span = $(`[data-sub-text="${n.id}"]`);
    if (span) { span.focus(); document.getSelection().selectAllChildren(span); }
  }
  // The transcript's lines as subtitle lines (Whisper's words give better ones when there are any).
  function linesToSubs() {
    if (D.subs_auto.length) { rebuildSubs(); return; }
    pushUndo();
    subs = D.lines.map((l, k) => {
      const next = D.lines[k + 1], t0 = Math.min(l.t, D.duration - SUB_MIN);
      const t1 = Math.min(next ? next.t - 0.04 : D.duration, t0 + 6, D.duration);
      return { id: "l" + (k + 1), t0: r3(t0), t1: r3(Math.max(t1, t0 + SUB_MIN)), text: l.text, by: "you" };
    });
    selSub = null;
    subsChanged();
    listFlash("Now subtitle lines: type new times, retype them, + to add, × to delete.");
  }
  function paintLine(t) {
    const times = listMode() === "subs" ? subs.map(c => c.t0) : D.lines.map(l => l.t);
    if (!times.length) return;
    let k = -1;
    for (let i = 0; i < times.length && times[i] <= t; i++) k = i;
    if (k === lastLine) return;
    const box = $("[data-ed-lines]");
    const old = box.querySelector(".ed-line.now");
    if (old) old.classList.remove("now");
    lastLine = k;
    const row = k >= 0 && box.querySelector(`.ed-line[data-k="${k}"]`);
    if (!row) return;
    row.classList.add("now");
    if (!box.matches(":hover") && !box.contains(document.activeElement)) {   // scroll the list, never the page
      const top = row.offsetTop - box.offsetTop;
      if (top < box.scrollTop || top > box.scrollTop + box.clientHeight - row.offsetHeight)
        box.scrollTop = top - box.clientHeight / 3;
    }
  }
  const subsList = $("[data-ed-lines]");
  subsList.addEventListener("focusin", ev => {
    const sp = ev.target.closest("[data-sub-text]");
    if (sp) { sp.dataset.orig = sp.textContent; hold(); }
    const ti = ev.target.closest("[data-sub-t]");
    if (ti) {
      hold();
      ti.dataset.orig = ti.value;
      ti.select();
      const c = subs.find(x => x.id === ti.dataset.subT);
      if (c) { selSub = c.id; selFx = null; seek(ti.dataset.edge === "t0" ? c.t0 : Math.max(c.t0, c.t1 - 0.05)); paintAll(); }
    }
  });
  subsList.addEventListener("keydown", ev => {
    const ti = ev.target.closest("[data-sub-t]");
    if (ti) {
      if (ev.key === "Enter") { ev.preventDefault(); ti.blur(); }
      else if (ev.key === "Escape") { ev.preventDefault(); ti.value = ti.dataset.orig || ti.value; ti.dataset.cancel = "1"; ti.blur(); }
      return;
    }
    const sp = ev.target.closest("[data-sub-text]");
    if (!sp) return;
    if (ev.key === "Enter") { ev.preventDefault(); sp.blur(); }
    else if (ev.key === "Escape") { ev.preventDefault(); sp.textContent = sp.dataset.orig || ""; sp.blur(); }
  });
  subsList.addEventListener("focusout", ev => {
    const ti = ev.target.closest("[data-sub-t]");
    if (ti) {
      const cancel = ti.dataset.cancel === "1";
      delete ti.dataset.cancel;
      if (cancel || ti.value === ti.dataset.orig) return;
      setTimeout(() => {
        const err = retimeSub(ti.dataset.subT, ti.dataset.edge, parseTime(ti.value));
        if (err) { listFlash(err); ti.value = ti.dataset.orig; }
        else {
          const c = subs.find(x => x.id === ti.dataset.subT);
          if (c) ti.value = fmt(c[ti.dataset.edge], 2);
        }
        paintAll();
      }, 0);
      return;
    }
    const sp = ev.target.closest("[data-sub-text]");
    if (!sp) return;
    setTimeout(() => { setSubText(sp.dataset.subText, sp.textContent); paintAll(); }, 0);
  });
  const lineEnd = k => (k + 1 < D.lines.length ? D.lines[k + 1].t : D.duration);

  // ── transcript: Whisper on this clip (word timings, for subtitles) or YouTube's auto-captions ──
  const LANG = { en: "English", id: "Indonesian", ja: "Japanese", ko: "Korean", zh: "Chinese", es: "Spanish" };
  function paintTranscript() {
    const box = $("[data-ed-tx]"), t = D.transcript;
    box.textContent = "";
    const what = el("small", "muted");
    if (t.state === "queued" || t.state === "running") {
      what.className = "busy";
      what.textContent = t.state === "running" ? `🎙 Whisper is transcribing this clip… (about half the clip's length)`
                                               : "🎙 Queued: Whisper starts when the worker finishes its current job.";
    } else if (t.source === "whisper") {
      const w = t.whisper;
      what.textContent = `Transcript: Whisper ${w.model} · ${LANG[w.language] || w.language} · ${w.lines} lines`;
      what.title = `Made ${w.created_at} UTC in ${Math.round(w.took_s)} s` + (w.dropped ? ` · ${w.dropped} likely made-up line(s) left out` : "");
    } else {
      what.textContent = t.source === "youtube" ? "Transcript: YouTube auto-captions" : "No transcript for this clip yet.";
    }
    if (t.state === "error") box.append(el("small", "err", "Transcribing failed: " + t.error));
    box.prepend(what);
    if (t.can && t.state !== "queued" && t.state !== "running")
      box.appendChild(btn(t.source === "whisper" ? "🎙 Again" : "🎙 Transcribe",
        t.source === "whisper" ? "Run Whisper on this clip again" : `Whisper (${t.model}) turns her speech into text with a time for every word: the start of subtitles`,
        { edAct: "transcribe" }));
  }
  async function transcribe() {
    try {
      const d = await post(`/moment/${pk}/transcribe`, {});
      D.transcript = d.transcript;
      paintTranscript();
      pollTranscript();
    } catch (e) { flash(e.message); }
  }
  function pollTranscript() {
    clearTimeout(txPoll);
    txPoll = setTimeout(async () => {
      try {
        const d = await (await fetch(`/api/moment/${pk}/transcript`, { cache: "no-store" })).json();
        const was = D.transcript.state;
        D.transcript = d.transcript;
        if (d.transcript.state === "queued" || d.transcript.state === "running") pollTranscript();
        else if (was !== "none") {
          D.lines = d.lines;
          Object.assign(D, { subs_auto: d.subs_auto, sub_words: d.sub_words, sub_language: d.sub_language });
          if (!subsMine) subs = cloneFx(d.subs_auto);     // lines that follow the words: the new ones
          listFor = null;
          subKey = "";
          paintAll();
        }
        paintTranscript();
      } catch (e) { pollTranscript(); }
    }, 3000);
  }

  let flashTimer = null;
  function flash(text) {
    const s = $("[data-ed-saved]");
    s.textContent = text;
    s.className = "ed-warn";
    clearTimeout(flashTimer);
    flashTimer = setTimeout(() => { s.textContent = ""; s.className = "muted"; }, 2500);
  }
  // A message under the list (where you're typing), for a few seconds.
  let listMsgTimer = null;
  function listFlash(text) {
    const m = $("[data-ed-list-msg]");
    m.textContent = text;
    m.hidden = false;
    clearTimeout(listMsgTimer);
    listMsgTimer = setTimeout(() => { m.hidden = true; }, 5000);
  }
  function setSaved(text, bad) {
    const s = $("[data-ed-saved]");
    clearTimeout(flashTimer);
    s.textContent = text;
    s.className = bad ? "ed-warn" : "muted";
  }
  function libraryStatus() {
    const n = D.sounds.length;
    // Only when something needs doing: the status line is for news, not a manual.
    const regionsTip = Object.keys(D.regions).length ? "" : "Draw the regions (⋯ More → ▢ Draw regions) so the camera can show her face, the game…";
    if (!n) fxStatus("No sounds in your library yet. " + regionsTip, "", true);
    else if (regionsTip) fxStatus(regionsTip, "");
  }
  function fxStatus(text, kind, linkSounds) {
    const s = $("[data-ed-fx-status]");
    s.textContent = text;
    s.className = "ed-fx-status " + (kind || "muted");
    if (linkSounds) {
      const a = el("a", "", " Open Sounds →");
      a.href = "/sounds";
      s.appendChild(a);
    }
  }

  // ── saving and rendering ─────────────────────────────────────────────────
  const body = () => ({ segments: segs.map(s => [r3(s[0]), r3(s[1]), s[2] || 0]), source,
                        effects: fx.map(f => ({ ...f, t: r3(f.t) })),
                        subs: subsMine ? subs.map(c => ({ ...c, t0: r3(c.t0), t1: r3(c.t1) })) : "auto",
                        subs_style: subStyle, kind, ...(kind === "short" && layout ? { layout } : {}) });
  const outFile = () => (kind === "short" ? "short.mp4" : "edit.mp4");
  async function save() {
    if (saving) { clearTimeout(saveTimer); saveTimer = setTimeout(save, 300); return; }
    saving = true;
    dirty = false;
    try {
      const d = await post(`/moment/${pk}/edit`, body());
      D.saved = true;
      setSaved("Saved");
      showRender(d.render);
      if (kind === "short") window.dispatchEvent(new Event("cw-short-changed"));   // the cover follows the layout
    } catch (e) {
      dirty = true;
      setSaved("Couldn't save: " + e.message, true);
    } finally {
      saving = false;
    }
  }
  // The intro panel (maker.js) changed something edit.mp4 depends on: show whether it's out of date.
  window.addEventListener("cw-render-changed", async () => {
    try {
      const d = await (await fetch(`/api/moment/${pk}/render?kind=${kind}`, { cache: "no-store" })).json();
      showRender(d.render);
      if (d.render.state === "queued" || d.render.state === "running") pollRender();
    } catch (e) {}
  });
  window.addEventListener("pagehide", () => {
    if (!dirty) return;
    fetch(`/moment/${pk}/edit`, { method: "POST", keepalive: true, headers: { "Content-Type": "application/json" },
                                  body: JSON.stringify(body()) });
  });
  async function render() {
    clearTimeout(saveTimer);
    const b = $('[data-ed-act="render"]');
    b.disabled = true;
    try {
      const d = await post(`/moment/${pk}/render`, body());
      dirty = false;
      D.saved = true;
      setSaved("Saved");
      showRender(d.render);
      pollRender();
    } catch (e) {
      setSaved(e.message, true);
    } finally {
      b.disabled = false;
    }
  }
  function pollRender() {
    clearTimeout(renderPoll);
    renderPoll = setTimeout(async () => {
      try {
        const d = await (await fetch(`/api/moment/${pk}/render?kind=${kind}`, { cache: "no-store" })).json();
        showRender(d.render);
        if (d.render.state === "queued" || d.render.state === "running") pollRender();
      } catch (e) { pollRender(); }
    }, 1200);
  }
  function showRender(r) {
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
      box.append(el("p", "faint", kind === "short"
        ? "Render makes short.mp4 (1080×1920) next to the clip: your cut in the layout above, sounds mixed in, zooms, subtitles and the title burned in, levelled loudness."
        : "Render makes edit.mp4 next to the clip: frame-exact cuts, your sounds mixed in, levelled loudness, ready to upload."));
    }
  }
  function toggleWatch() {
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

  // ── input ────────────────────────────────────────────────────────────────
  function hitSeg(x) {
    let best = null, bestD = EDGE_PX + 1;
    const order = segs.map((_, i) => i).sort((a, b) => (b === sel) - (a === sel));
    for (const i of order) {
      for (const edge of [0, 1]) {
        const d = Math.abs(x - X(segs[i][edge]));
        if (d < bestD) { best = { i, edge }; bestD = d; }
      }
    }
    if (best) return best;
    const inside = order.find(i => x >= X(segs[i][0]) && x <= X(segs[i][1]));
    return inside === undefined ? null : { i: inside };
  }
  function hitFx(x) {
    let best = null, bestD = FX_PX + 1;
    for (const f of fx) {
      if (f.type !== "sfx") continue;
      const d = Math.abs(x - X(f.t)) - (f.id === selFx ? 0.5 : 0);
      if (d < bestD) { best = f; bestD = d; }
    }
    return best;
  }
  // The effect under x in the picture lane, and whether x is on its right end (to resize it).
  function hitVfx(x, lane) {
    const under = fx.filter(f => isVis(f) && laneOf(f) === lane && x >= X(f.t) - 4 && x <= Math.max(X(f.t + f.d), X(f.t) + 4) + 4);
    if (!under.length) return null;
    const f = under.find(f => f.id === selFx) || under.sort((a, b) => b.t - a.t)[0];
    const end = X(f.t + f.d);
    return { f, edge: Math.abs(x - end) <= EDGE_PX && end - X(f.t) > 10 ? "end" : null };
  }
  // The subtitle line under x, and whether x is on its start or end edge.
  function hitSubAt(x) {
    const c = subs.find(c => c.id === selSub && x >= X(c.t0) - 4 && x <= X(c.t1) + 4)
           || subs.find(c => x >= X(c.t0) - 3 && x <= X(c.t1) + 3);
    if (!c) return null;
    const a = X(c.t0), z = X(c.t1), wide = z - a > 14;
    return { c, edge: wide && Math.abs(x - a) <= EDGE_PX ? "t0" : wide && Math.abs(x - z) <= EDGE_PX ? "t1" : null };
  }
  canvas.addEventListener("pointerdown", ev => {
    if (!D || ev.button !== 0) return;
    hold();
    audio();
    const x = gx(ev), y = gy(ev);
    canvas.setPointerCapture(ev.pointerId);
    if (y >= Y_SUBS && y < Y_RULER) {                     // the subtitle lane
      const h = hitSubAt(x);
      if (h) {
        pushUndo();
        const k = subIndex(h.c.id);
        drag = { sub: h.c.id, edge: h.edge, moved: false, x0: x, dx: x - X(h.c.t0), len: h.c.t1 - h.c.t0,
                 lo: subs[k - 1] ? subs[k - 1].t1 : 0, hi: subs[k + 1] ? subs[k + 1].t0 : D.duration };
        selSub = h.c.id;
        selFx = null;
        video.pause();
        stopEditPlay();
        paintAll();
        return;
      }
      if (selSub) { selSub = null; paintAll(); }
      drag = { scrub: true };
      seek(T(x));
      return;
    }
    if (y >= Y_CAM && y < Y_SUBS) {                       // the camera and effects lanes
      const h = hitVfx(x, y < Y_VFX ? "cam" : "vfx");
      if (h) {
        pushUndo();
        drag = { vis: h.f.id, edge: h.edge, moved: false, dx: x - X(h.f.t), x0: x };
        selFx = h.f.id;
        video.pause();
        stopEditPlay();
        paintAll();
        return;
      }
      if (selFx) { selFx = null; paintAll(); }
      drag = { scrub: true };
      seek(T(x));
      return;
    }
    if (y >= Y_FX && y < Y_CAM) {                         // the sound lane
      const f = hitFx(x);
      if (f) {
        pushUndo();
        drag = { fx: f.id, moved: false, dx: x - X(f.t), x0: x };
        selFx = f.id;
        video.pause();
        stopEditPlay();
        paintAll();
        return;
      }
      if (selFx) { selFx = null; paintAll(); }
      drag = { scrub: true };
      seek(T(x));
      return;
    }
    const h = hitSeg(x);
    if (h && h.edge !== undefined) {
      pushUndo();
      sel = h.i;
      selFx = null;
      drag = { i: h.i, edge: h.edge, moved: false, x0: x };
      video.pause();
      stopEditPlay();
      paintAll();
      return;
    }
    if (h && (h.i !== sel || selFx)) { sel = h.i; selFx = null; paintAll(); }
    drag = { scrub: true };
    seek(T(x));
  });
  canvas.addEventListener("pointermove", ev => {
    if (!D) return;
    const x = gx(ev), y = gy(ev);
    if (!drag) {
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
    if (drag.scrub) { seek(T(x)); return; }
    if (!drag.moved && Math.abs(x - drag.x0) < 3) return;   // a click, not a drag
    if (drag.sub) {
      const c = curSub();
      if (!c) return;
      if (drag.edge === "t0") c.t0 = Math.min(Math.max(T(x), drag.lo), c.t1 - SUB_MIN);
      else if (drag.edge === "t1") c.t1 = Math.min(Math.max(T(x), c.t0 + SUB_MIN), drag.hi);
      else {
        const t0 = Math.min(Math.max(T(x - drag.dx), drag.lo), drag.hi - drag.len);
        c.t0 = t0;
        c.t1 = t0 + drag.len;
      }
      drag.moved = true;
      subKey = "";
      if (!video.seeking) video.currentTime = drag.edge === "t1" ? Math.max(c.t0, c.t1 - 0.05) : c.t0;
      needsDraw = true;
      return;
    }
    if (drag.vis) {
      const f = curFx();
      if (!f) return;
      const [, shortest, longest] = D.visual.types[f.type];
      if (drag.edge === "end") f.d = Math.min(Math.max(T(x) - f.t, shortest), longest, D.duration - f.t);
      else f.t = Math.min(T(x - drag.dx), D.duration - shortest);
      drag.moved = true;
      if (!video.seeking) video.currentTime = drag.edge === "end" ? Math.max(f.t, f.t + f.d - 1 / (D.fps || 30)) : f.t;
      needsDraw = true;
      return;
    }
    if (drag.fx) {
      const f = curFx();
      if (!f) return;
      f.t = T(x - drag.dx);
      drag.moved = true;
      if (!video.seeking) video.currentTime = f.t;
      needsDraw = true;
      return;
    }
    const s = segs[drag.i];
    const t = T(x);
    s[drag.edge] = drag.edge === 0 ? Math.min(t, s[1] - MIN) : Math.max(t, s[0] + MIN);
    drag.moved = true;
    if (!video.seeking) video.currentTime = s[drag.edge];
    needsDraw = true;
  });
  function endDrag(ev) {
    if (!drag) return;
    const d = drag;
    drag = null;
    needsDraw = true;
    if (d.scrub) return;
    if (!d.moved) {
      undo.pop();
      $('[data-ed-act="undo"]').disabled = !undo.length;
      if (d.sub) selectSub(d.sub);                        // a click on a line: jump to it
      return;
    }
    if (d.sub) {
      const c = curSub();
      if (!c) return;
      if (snapOn && !ev.shiftKey && D.sub_words.length) {    // edges land on word starts and ends
        const starts = D.sub_words.map(w => [w[0], 0]), ends = D.sub_words.map(w => [w[1], 0]);
        if (d.edge !== "t1") {
          const t0 = Math.min(Math.max(snap(c.t0, SUB_SNAP_S, starts), d.lo), c.t1 - SUB_MIN);
          if (!d.edge) c.t1 += t0 - c.t0;
          c.t0 = t0;
        } else c.t1 = Math.min(Math.max(snap(c.t1, SUB_SNAP_S, ends), c.t0 + SUB_MIN), d.hi);
      }
      c.t0 = r3(c.t0);
      c.t1 = r3(Math.min(c.t1, D.duration));
      c.by = "you";
      seek(d.edge === "t1" ? Math.max(c.t0, c.t1 - 0.05) : c.t0);
      subsChanged();
      return;
    }
    if (d.vis) {
      const f = curFx();
      if (!f) return;
      if (snapOn && !ev.shiftKey && !d.edge && f.type !== "push") f.t = snap(f.t, DRAG_SNAP_S, D.hits);
      f.t = r3(Math.max(0, f.t));
      f.d = r3(Math.min(f.d, D.duration - f.t));
      f.by = "you";
      seek(f.t);
      changed();
      return;
    }
    if (d.fx) {
      const f = curFx();
      if (snapOn && !ev.shiftKey) f.t = snap(f.t, DRAG_SNAP_S, D.hits);
      f.t = r3(f.t);
      f.by = "you";
      seek(f.t);
      changed();
      return;
    }
    const s = segs[d.i];
    if (snapOn && !ev.shiftKey) {
      const v = snap(s[d.edge], DRAG_SNAP_S);
      if (d.edge === 0 ? v <= s[1] - MIN : v >= s[0] + MIN) s[d.edge] = v;
    }
    s[d.edge] = r3(s[d.edge]);
    seek(s[d.edge]);
    changed();
  }
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", endDrag);
  scroller.addEventListener("scroll", () => { needsDraw = true; });
  window.addEventListener("resize", resize);

  root.addEventListener("click", ev => {
    const b = ev.target.closest("button, [data-line]");
    if (!b || !D) return;
    hold();
    audio();
    if (b.tagName === "BUTTON") b.blur();   // so Space/Enter go to the editor, not the button
    const ds = b.dataset;
    const nudgeVal = v => (v === "here" ? null : ev.shiftKey && ds.step ? Math.sign(+v) * +ds.step : +v);
    if (regionMode && !ds.regDel && ds.edAct !== "regions" && !ds.regName) toggleRegions(false);   // anything else: back to editing
    if (ds.transStyle !== undefined) setTransition(ds.transStyle);
    else if (ds.chipRegion !== undefined) {
      if (ds.chipFor === "seg") setSegRegion(ds.chipRegion);
      else {
        const f = curFx(), change = { region: ds.chipRegion };
        if (f && f.type === "punch" && ds.chipRegion && D.regions[ds.chipRegion]) change.zoom = Math.min(fitZoom(ds.chipRegion), 2.5);
        if (f) editFx(change);
      }
    } else if (ds.edKind) switchKind(ds.edKind);
    else if (ds.layType) { if (layout && layout.type !== ds.layType) setLayout({ type: ds.layType }); }
    else if (ds.layCentre) {
      const b = D.regions[ds.layCentre];
      if (b && layout) setLayout({ type: "crop", x: Math.round((b.x + b.w / 2) * 1e4) / 1e4, y: Math.round((b.y + b.h / 2) * 1e4) / 1e4 });
    } else if (ds.layTitle !== undefined) { if (layout) setLayout({ title: D.titles[+ds.layTitle] || "" }); }
    else if (ds.edAdd) { if (regionMode) toggleRegions(false); addVis(ds.edAdd); }
    else if (ds.subGo) selectSub(ds.subGo);
    else if (ds.subStart || ds.subEnd) {
      const c = subs.find(x => x.id === (ds.subStart || ds.subEnd));
      if (c) setEdge(ds.subStart ? 0 : 1, ds.subStart ? c.t0 : c.t1);
    } else if (ds.subAfter) addSubAfter(ds.subAfter);
    else if (ds.subSplit) splitSub(ds.subSplit);
    else if (ds.subJoin) joinSub(ds.subJoin);
    else if (ds.subDel) deleteSub(ds.subDel);
    else if (ds.subNudge0 !== undefined || ds.subNudge1 !== undefined) {
      const edge = ds.subNudge0 !== undefined ? "t0" : "t1", v = nudgeVal(edge === "t0" ? ds.subNudge0 : ds.subNudge1), c = curSub();
      if (c) setSubTime(c.id, edge, v === null ? video.currentTime : c[edge] + v);
    }
    else if (ds.regDel) deleteRegion(ds.regDel);
    else if (ds.visFit !== undefined) { const f = curFx(); if (f) editFx({ zoom: fitZoom(f.region) }); }
    else if (ds.edPreset) usePreset(ds.edPreset);
    else if (ds.seg !== undefined) selectSeg(+ds.seg);
    else if (ds.nudge0 !== undefined || ds.nudge1 !== undefined) {
      const edge = ds.nudge0 !== undefined ? 0 : 1, v = nudgeVal(edge ? ds.nudge1 : ds.nudge0);
      v === null ? setEdge(edge, video.currentTime) : nudge(edge, v);
    } else if (ds.fxNudge !== undefined) {
      const f = curFx(), v = nudgeVal(ds.fxNudge);
      if (f) { const t = r3(Math.min(Math.max(0, v === null ? video.currentTime : f.t + v), D.duration)); editFx({ t }); seek(t); }
    } else if (ds.goT !== undefined) seek(+ds.goT);
    else if (ds.fxHear !== undefined) { const f = curFx(); if (f) playSound(f.sound, f.gain); }
    else if (ds.lineStart !== undefined) {
      const t = D.lines[+ds.lineStart].t;
      setEdge(0, snapOn ? snap(t, LINE_SNAP_S) : t);
    } else if (ds.lineEnd !== undefined) {
      const t = lineEnd(+ds.lineEnd);
      setEdge(1, snapOn ? snap(t, LINE_SNAP_S) : t);
    } else if (ds.line !== undefined) seek(D.lines[+ds.line].t);
    else if (ds.edAct) {
      ({
        "play-edit": () => (editPlay && !editPlay.only ? (video.pause(), stopEditPlay())
                                                         : ev.shiftKey ? playEdit(0) : playFromHere()),
        toggle, add, split, undo: doUndo, render, watch: toggleWatch,
        "play-seg": () => playEdit(sel, true),
        up: () => move(-1), down: () => move(1), delete: del,
        "add-fx": addFx, suggest: suggestFx, autoedit: autoEdit, "clear-claude": clearClaude,
        "fx-play": previewFx, regions: () => toggleRegions(), transcribe,
        "sub-add": addSub, "sub-rebuild": rebuildSubs, "lines-edit": linesToSubs, "sub-polish": polishSubs, "names-save": saveNames,
      })[ds.edAct]?.();
    }
  });
  root.addEventListener("change", ev => {
    const t = ev.target;
    if (t.matches("[data-lay]") && layout) {
      if (t.tagName === "SELECT") { setLayout({ [t.dataset.lay]: t.value }); return; }
      layout[t.dataset.lay] = t.type === "range" ? +t.value : t.value;
      if (!layEditing) pushUndo();
      layEditing = false;
      changed();
      return;
    }
    if (t.matches("[data-gain]")) setGain(+t.value);
    else if (t.matches("[data-seg-region]")) setSegRegion(t.value);
    else if (t.matches("[data-fx-sound]")) editFx({ sound: t.value, gain: (sound(t.value) || {}).gain || 0 });
    else if (t.matches("[data-fx-gain]")) editFx({ gain: +t.value });
    else if (t.matches("[data-fx-duck]")) editFx({ duck: +t.value });
    else if (t.matches("[data-vis-d]")) editFx({ d: Math.min(+t.value, D.duration - curFx().t) });
    else if (t.matches("[data-vis-region]")) {
      const f = curFx(), change = { region: t.value };
      if (f.type === "punch" && t.value && D.regions[t.value]) change.zoom = Math.min(fitZoom(t.value), 2.5);
      editFx(change);
    } else if (t.matches("[data-vis-zoom]")) editFx({ zoom: +t.value });
    else if (t.matches("[data-vis-strength]")) editFx({ strength: +t.value });
    else if (t.matches("[data-vis-color]")) editFx({ color: t.value });
    else if (t.matches("[data-reg-name]")) renameRegion(t.dataset.regName, t.value);
    else if (t.matches("[data-sub-input]")) setSubText(t.dataset.subInput, t.value);
    else if (t.matches("[data-sub-on]")) setSubStyle({ on: t.checked });
    else if (t.matches("[data-sub-size]")) setSubStyle({ size: t.value });
    else if (t.matches("[data-sub-pos]")) setSubStyle({ position: t.value });
    else if (t.matches("[data-sub-mode]")) setSubMode(t.value);
    else if (t.matches("[data-ed-snap]")) {
      snapOn = t.checked;
      try { localStorage.setItem("cw-ed-snap", snapOn ? "1" : "0"); } catch (e) {}
    } else if (t.matches("[data-ed-zoom]")) setZoom(+t.value);
  });
  function setZoom(z) {
    const w = scroller.clientWidth, centre = T(scroller.scrollLeft + w / 2);
    zoom = z;
    $("[data-ed-zoom]").value = String(z);
    resize();
    scroller.scrollLeft = X(centre) - w / 2;
  }

  document.addEventListener("keydown", ev => {
    if (!D || ev.altKey || ev.isComposing) return;
    const a = document.activeElement;
    if (a && (["INPUT", "SELECT", "TEXTAREA"].includes(a.tagName) || a.isContentEditable)) return;
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "z") { doUndo(); ev.preventDefault(); return; }
    if (ev.ctrlKey || ev.metaKey) return;
    const r = root.getBoundingClientRect();
    if (r.bottom < 0 || r.top > window.innerHeight) return;   // only while the editor is on screen
    if (regionMode) {
      if (ev.key === "Escape") { ev.preventDefault(); toggleRegions(false); }
      return;
    }
    const f = 1 / (D.fps || 30);
    const keys = {
      " ": toggle,
      ArrowLeft: () => seek(video.currentTime - (ev.shiftKey ? 1 : 0.1)),
      ArrowRight: () => seek(video.currentTime + (ev.shiftKey ? 1 : 0.1)),
      ",": () => { video.pause(); seek(video.currentTime - f); },
      ".": () => { video.pause(); seek(video.currentTime + f); },
      i: () => setEdge(0, video.currentTime), o: () => setEdge(1, video.currentTime),
      s: split, p: () => playEdit(sel, true), f: addFx, z: () => addVis("punch"), x: () => addVis("shake"),
      "[": () => selectSeg(sel - 1), "]": () => selectSeg(sel + 1),
      Delete: del, Backspace: del,
      Enter: () => (editPlay ? (video.pause(), stopEditPlay()) : ev.shiftKey ? playEdit(0) : playFromHere()),
      Escape: () => { if (selFx || selSub) { selFx = null; selSub = null; paintAll(); } },
      "+": () => setZoom(Math.min(16, zoom * 2)), "=": () => setZoom(Math.min(16, zoom * 2)),
      "-": () => setZoom(Math.max(1, zoom / 2)),
    };
    const fn = keys[ev.key] || keys[ev.key.toLowerCase()];
    if (!fn) return;
    ev.preventDefault();
    ev.stopPropagation();
    hold();
    audio();
    fn();
  }, true);

  // ── start ────────────────────────────────────────────────────────────────
  (async function load() {
    let d = await fetchEdit(kind);
    if (!d.ok && kind === "short") { kind = "long"; d = await fetchEdit("long"); }
    if (!d.ok) { $("[data-ed-loading]").textContent = d.error || "Couldn't load the editor."; return; }
    apply(d);
    $("[data-ed-loading]").remove();
    paintLanes();
    const notes = [d.hook && "Hook: " + d.hook, d.notes].filter(Boolean).join("\n\n");
    if (notes) { $("[data-ed-notes]").hidden = false; $("[data-ed-notes-text]").textContent = notes; }
    paintTranscript();
    paintNames();
    if (d.transcript.state === "queued" || d.transcript.state === "running") pollTranscript();
    resize();
    paintAll();
    showRender(d.render);
    if (d.render.state === "queued" || d.render.state === "running") pollRender();
    setSaved(d.saved ? "Saved" : (source === "long" ? "Claude's cut, snapped to pauses" : ""));
    libraryStatus();
    const start = () => { if (video.currentTime === 0 && segs[0]) seek(segs[0][0]); };
    if (video.readyState >= 1) start(); else video.addEventListener("loadedmetadata", start, { once: true });
    video.addEventListener("loadeddata", () => { needsDraw = true; });
    window.addEventListener("resize", () => { needsDraw = true; subKey = ""; });
    requestAnimationFrame(tick);
  })();
})();
