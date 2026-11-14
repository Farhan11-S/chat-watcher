// The picture preview: the camera (edit/motion.py), the Short's panels (edit/layout.py),
// spotlights (edit/spots.py) and the stage canvas that draws them over the video.
import { ASS_TO_CSS, S, ZOOMS, sctx, stage, video } from "./state.js";
import { curFx, keptFx, runsOf, wrapSub } from "./util.js";

let stageOn = false;
let comp = null, bgc = null;   // the Short's preview: the picture before zoom, and the small blurred background

// ── picture effects: the same maths as camera() in edit/motion.py, so the preview
// shows what the render will. A view is a centre and a half-size, as fractions of the frame.
function centre(name) {
  const b = S.D.regions[name];
  if (!b) return [0.5, 0.5];
  if (S.kind !== "short" || !S.layout) return [b.x + b.w / 2, b.y + b.h / 2];
  return mapPoint(b.x + b.w / 2, b.y + b.h / 2, shortPanels(video.currentTime)) || [0.5, 0.5];     // where it lands on the Short
}

// ── the Short's layout: the same maths as panels() in edit/layout.py ──────
export const isShort = () => S.kind === "short" && !!S.layout && !S.regionMode;
function fitBox([bx, by, bw, bh], aspect, sw, sh) {
  let pw = bw * sw, ph = bh * sh;
  if (pw / ph < aspect) pw = ph * aspect; else ph = pw / aspect;
  if (pw > sw) { pw = sw; ph = sw / aspect; }
  if (ph > sh) { pw = sh * aspect; ph = sh; }
  const cx = (bx + bw / 2) * sw, cy = (by + bh / 2) * sh;
  const x0 = Math.min(Math.max(cx - pw / 2, 0), sw - pw), y0 = Math.min(Math.max(cy - ph / 2, 0), sh - ph);
  return [x0 / sw, y0 / sh, pw / sw, ph / sh];
}
const boxOf = name => { const b = name && S.D.regions[name]; return b ? [b.x, b.y, b.w, b.h] : [0, 0, 1, 1]; };
function panels(L = S.layout) {
  const [OW, OH] = S.D.layout_info.out;
  const sw = video.videoWidth || S.D.layout_info.src[0], sh = video.videoHeight || S.D.layout_info.src[1];
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
// "full" is the whole picture: fit + blur for a while (region_box in edit/layout.py).
export const FULL = "full";
const frameBox = name => (name === FULL ? { x: 0, y: 0, w: 1, h: 1 } : S.D.regions[name]);
function framePanels(name) {
  const b = frameBox(name), [OW, OH] = S.D.layout_info.out, F = S.D.layout_info.frame;
  const sw = video.videoWidth || S.D.layout_info.src[0], sh = video.videoHeight || S.D.layout_info.src[1];
  const w = Math.min(b.w * F.margin, 1), h = Math.min(b.h * F.margin, 1), cx = b.x + b.w / 2, cy = b.y + b.h / 2;
  const grown = [Math.min(Math.max(cx - w / 2, 0), 1 - w), Math.min(Math.max(cy - h / 2, 0), 1 - h), w, h];
  const aspect = OW / OH;
  if ((w * sw) / (h * sh) <= aspect * F.wide) return [{ src: fitBox(grown, aspect, sw, sh), dst: [0, 0, 1, 1] }];
  const src = fitBox(grown, OW / (OH * 0.6), sw, sh);
  const dh = Math.min(0.6, OW / (src[2] * sw) * (src[3] * sh) / OH);
  return [{ src: fitBox(grown, aspect, sw, sh), dst: [0, 0, 1, 1], blur: true }, { src, dst: [0, (1 - dh) / 2, 1, dh] }];
}
// A frame shot with top / bottom is a stack of two regions for a while (shot_panels).
const isStackShot = f => !!(f.top || f.bottom);
const shotOk = f => (isStackShot(f) ? [f.top, f.bottom].every(n => !n || S.D.regions[n]) : !!frameBox(f.region));
export function shortPanels(t) {
  let on = null;
  for (const f of S.fx) if (f.type === "frame" && active(f, t) && shotOk(f) && (!on || f.t >= on.t)) on = f;
  if (!on) return panels();
  if (isStackShot(on)) return panels({ type: "stack", top: on.top || "", bottom: on.bottom || "", split: on.split || 0.6 });
  return framePanels(on.region);
}
// With two pictures (a stack), a zoom stays inside the panel showing its region (the top
// one for the centre): short_visuals in edit/validate.py. viewPanels: the panels with
// their zooms applied (what the render shows), camera(): what zooms the whole picture.
const sharpOf = ps => ps.filter(p => !p.blur);
function panelOf(f, two) {
  const b = S.D.regions[f.region];
  if (!b) return [two[0], 0.5, 0.5];
  const x = b.x + b.w / 2, y = b.y + b.h / 2;
  for (const p of [...two].reverse()) {
    const [sx, sy, sw, sh] = p.src;
    if (sx <= x && x <= sx + sw && sy <= y && y <= sy + sh) return [p, (x - sx) / sw, (y - sy) / sh];
  }
  return [two[0], 0.5, 0.5];
}
export function viewPanels(t) {
  const ps = shortPanels(t), two = sharpOf(ps);
  if (two.length < 2) return ps;
  const on = new Map();
  for (const f of S.fx.filter(f => ZOOMS.includes(f.type) && active(f, t)).sort((a, b) => a.t - b.t)) {
    const [p, rx, ry] = panelOf(f, two);
    on.set(p, [f, rx, ry]);                                 // the latest one to start wins
  }
  return ps.map(p => {
    if (!on.has(p)) return p;
    const [f, rx, ry] = on.get(p);
    const q = Math.min(Math.max((t - f.t) / f.d, 0), 1), sq = q * q * (3 - 2 * q), k = f.type === "punch" ? 1 : sq;
    const z = 1 + (f.zoom - 1) * k, h = 0.5 / z;
    const cx = Math.min(Math.max(0.5 + (rx - 0.5) * k, h), 1 - h), cy = Math.min(Math.max(0.5 + (ry - 0.5) * k, h), 1 - h);
    const [sx, sy, sw, sh] = p.src;
    return { ...p, src: [sx + (cx - h) * sw, sy + (cy - h) * sh, 2 * h * sw, 2 * h * sh] };
  });
}
const twoPictures = t => isShort() && sharpOf(shortPanels(t)).length > 1;
export function mapPoint(x, y, ps) {
  for (const p of [...ps].reverse()) {
    if (p.blur) continue;
    const [sx, sy, sw, sh] = p.src, [dx, dy, dw, dh] = p.dst;
    if (sx <= x && x <= sx + sw && sy <= y && y <= sy + sh) return [dx + (x - sx) / sw * dw, dy + (y - sy) / sh * dh];
  }
  return null;
}
export function fitZoom(name) {
  const b = S.D.regions[name], [lo, hi] = S.D.visual.zoom_range;
  if (!b) return 1.5;
  return Math.round(Math.min(Math.max(Math.min(1 / Math.max(b.w, 1e-3), 1 / Math.max(b.h, 1e-3)), lo), hi) * 100) / 100;
}
const active = (f, t) => f.t <= t && t < f.t + f.d;
// Transitions on real cuts → camera / flash / blur pieces in clip time (motion.transitions).
function transVis() {
  const out = [], T = S.D.visual.transitions, [dlo, dhi] = S.D.visual.transition_d;
  for (let i = 1; i < S.segs.length; i++) {
    const s = S.segs[i][0], prev = S.segs[i - 1], e = prev[1];
    if (Math.abs(s - e) < 0.002) continue;
    const tr = S.fx.find(f => f.type === "transition" && T[f.style] && Math.abs(f.t - s) < 0.05);
    if (!tr) continue;
    const d = Math.min(Math.max(+tr.d || T[tr.style][0], dlo), dhi);
    const h = Math.min(d / 2, (e - prev[0]) / 3, (S.segs[i][1] - s) / 3);
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
export function camera(t) {
  let z = 1, cx = 0.5, cy = 0.5, zf = null;
  const zs = (twoPictures(t) ? [] : S.fx.filter(f => ZOOMS.includes(f.type)))   // a stack's zooms are its panels' (viewPanels)
    .concat(transVis().filter(v => v.type === "push" || v.type === "pull"));
  for (const f of zs.sort((a, b) => a.t - b.t)) if (active(f, t)) zf = f;
  if (zf) {
    const q = Math.min(Math.max((t - zf.t) / zf.d, 0), 1), sq = q * q * (3 - 2 * q);
    const p = zf.type === "punch" ? 1 : zf.type === "pull" ? 1 - sq : sq;
    const [rx, ry] = zf.cx !== undefined ? [zf.cx, zf.cy] : centre(zf.region);
    z = 1 + (zf.zoom - 1) * p; cx = 0.5 + (rx - 0.5) * p; cy = 0.5 + (ry - 0.5) * p;
  }
  let a = 0, dx = 0, dy = 0;
  for (const f of S.fx) {
    if (f.type !== "shake" || !active(f, t)) continue;
    const e = (S.D.visual.shake[f.strength] || S.D.visual.shake[2]) * (1 - (t - f.t) / f.d);
    a += e;
    dx += e * wave(t - f.t, S.D.visual.waves[0]);
    dy += e * wave(t - f.t, S.D.visual.waves[1]);
  }
  const h = 0.5 / (z * (1 + S.D.visual.margin * a)), lo = h + a, hi = 1 - h - a;
  if (lo > hi) { cx = 0.5; cy = 0.5; } else { cx = Math.min(Math.max(cx, lo), hi); cy = Math.min(Math.max(cy, lo), hi); }
  return [cx + dx, cy + dy, h];
}
function flashesAt(t) {
  return S.fx.filter(f => f.type === "flash").concat(transVis().filter(v => v.type === "flash" || v.type === "fadeout"))
    .filter(f => active(f, t)).map(f => {
      const [rgb, peak] = S.D.visual.flash[f.color] || S.D.visual.flash.white;
      const q = (t - f.t) / f.d;
      return [rgb, peak * (f.type === "fadeout" ? q : 1 - q)];
    });
}
// The picture box inside the <video> element (it can be letterboxed), relative to the player.
export function pictureBox() {
  const vw = video.videoWidth || 16, vh = video.videoHeight || 9, bw = video.clientWidth, bh = video.clientHeight;
  const k = Math.min(bw / vw, bh / vh), w = vw * k, h = vh * k;
  return { left: video.offsetLeft + (bw - w) / 2, top: video.offsetTop + (bh - h) / 2, width: w, height: h };
}
// Where the picture shows: the video's box, or the Short's 9:16 box in the player.
export function viewBox() {
  if (!isShort()) return pictureBox();
  const pl = video.parentElement, bw = pl.clientWidth, bh = pl.clientHeight;
  const k = Math.min(bw / 9, bh / 16), w = 9 * k, h = 16 * k;
  return { left: (bw - w) / 2, top: (bh - h) / 2, width: w, height: h };
}
export function place(elm, box) {
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
    for (const p of viewPanels(video.currentTime)) {
      const [sx, sy, sw, sh] = p.src, [dx, dy, dw, dh] = p.dst;
      if (p.blur) {           // small, blurred, scaled up and darkened: like the render's background
        const bw = Math.max(2, Math.round(cw * dw / S.D.layout_info.blur_down)), bh = Math.max(2, Math.round(ch * dh / S.D.layout_info.blur_down));
        if (bgc.width !== bw || bgc.height !== bh) { bgc.width = bw; bgc.height = bh; }
        const b = bgc.getContext("2d");
        b.filter = "blur(2px)";
        b.drawImage(video, sx * vw, sy * vh, sw * vw, sh * vh, 0, 0, bw, bh);
        c.drawImage(bgc, 0, 0, bw, bh, dx * cw, dy * ch, dw * cw, dh * ch);
        c.fillStyle = `rgba(0,0,0,${1 - S.D.layout_info.dark})`;
        c.fillRect(dx * cw, dy * ch, dw * cw, dh * ch);
      } else c.drawImage(video, sx * vw, sy * vh, sw * vw, sh * vh, dx * cw, dy * ch, dw * cw, dh * ch);
    }
  }
  const [xc, yc, h] = camera(video.currentTime);
  sctx.filter = blurAt(video.currentTime) ? `blur(${Math.round(cw / 60)}px)` : "none";
  sctx.drawImage(comp, (xc - h) * cw, (yc - h) * ch, 2 * h * cw, 2 * h * ch, 0, 0, cw, ch);
  sctx.filter = "none";
  drawSpots(cw, ch, xc, yc, h, video.currentTime);
  for (const [rgb, alpha] of flashesAt(video.currentTime)) {
    sctx.fillStyle = `rgba(${rgb.join(",")},${alpha})`;
    sctx.fillRect(0, 0, cw, ch);
  }
  drawHook(cw, ch);
}
// The hook (edit/hook.py): the layout's title over the Short's first seconds, in its style.
// Shown while the edit's own time is under its seconds, and always while the Layout tab is
// open (so you see what you're styling).
function editTime(t) {
  if (S.editPlay && S.segs[S.editPlay.i]) {
    const i = S.editPlay.i;
    return S.segs.slice(0, i).reduce((n, s) => n + s[1] - s[0], 0) + Math.max(0, t - S.segs[i][0]);
  }
  let before = 0;
  for (const s of S.segs) {
    if (t >= s[0] && t < s[1]) return before + t - s[0];
    before += s[1] - s[0];
  }
  return null;
}
const hookHex = name => (S.D.layout_info.hook.colors[name] || "FFFFFF");
function hookDark(rgb) {           // hook._dark: the ring round a bubble hook's white outline
  const r = parseInt(rgb.slice(0, 2), 16) / 255, g = parseInt(rgb.slice(2, 4), 16) / 255, b = parseInt(rgb.slice(4, 6), 16) / 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2, d = mx - mn;
  const sat = d === 0 ? 0 : d / (1 - Math.abs(2 * l - 1));
  if (sat < 0.12) return "1A1A1A";
  const k = (Math.min(l, 0.5) * 0.38) / Math.max(l, 1e-6);
  return [r, g, b].map(v => Math.round(Math.min(1, v * k) * 255).toString(16).padStart(2, "0")).join("");
}
function drawHook(cw, ch) {
  const text = S.layout.title, H = S.D.layout_info.hook;
  if (!text || !H) return;
  const st = Object.assign({}, H.default, S.layout.hook || {});
  const pane = document.querySelector('[data-ed-pane="lay"]'), styling = pane && !pane.hidden;
  const et = editTime(video.currentTime);
  const showing = et !== null && et < st.s;
  if (!showing && !styling) return;
  const u = showing ? et * 1000 : 900, dur = st.s * 1000;          // ms into the hook
  const C = S.D.sub_layout.captions, L = S.D.sub_layout, k = H.sizes[st.size] || 1;
  const size = L.title_size * cw * k, lineH = size * C.line_h;
  const rows = wrapSub(text, Math.max(6, Math.floor(L.title_line / Math.max(k, 1))), 3);
  const x = cw / 2, y = H.places[st.at] * ch + rows.length * lineH / 2;
  const fam = st.font ? `"${C.fonts[st.font]}", ` : `"${L.font}", `;
  const font = sz => `700 ${sz * ASS_TO_CSS}px ${fam}"Noto Sans CJK JP", "Noto Sans JP", system-ui, sans-serif`;
  // The motion: the same steps as captions.events / hook.events.
  const P = C.pop_ms, h = P / 2;
  let sc = 1, dx = 0, dy = 0, rot = st.fx === "shake" || st.fx === "wobble" || st.fx === "wave" ? 0 : st.tilt, alpha = 1;
  if (["pop", "shake", "wave", "wobble", "grow"].includes(st.fx) && u < P) sc = u < h ? 0.4 + 0.75 * u / h : 1.15 - 0.15 * (u - h) / h;
  if (st.fx === "grow" && u >= P) sc = 1 + (C.grow_to - 1) * Math.min(1, (u - P) / Math.max(1, dur - P));
  if (st.fx === "slam" && u < H.slam_ms) { sc = 2.6 - 1.6 * u / H.slam_ms; alpha = u / H.slam_ms; }
  let sx = sc, sy = sc;
  if (st.fx === "bounce") {
    const B = C.bounce_ms;
    if (u < B) dy = -size * 1.2 * (1 - u / B);
    else if (u < B + 60) { const q = (u - B) / 60; sx = 1 + 0.25 * q; sy = 1 - 0.22 * q; }
    else if (u < B + 170) { const q = (u - B - 60) / 110; sx = 1.25 - 0.25 * q; sy = 0.78 + 0.22 * q; }
  }
  if (st.fx === "shake" && u >= P) {
    const [step, amp, tilt] = C.shake, n = Math.floor((u - P) / step);
    const j = m => { const v = Math.sin(7 * 12.9898 + n * 78.233 + m * 37.719) * 43758.5453; return (v - Math.floor(v)) * 2 - 1; };
    dx = j(1) * amp * size; dy = j(2) * amp * size; rot = j(3) * tilt;
  }
  if (st.fx === "wobble") rot = C.wobble[0] * Math.sin(2 * Math.PI * u / C.wobble[1]);
  const fade = Math.min(H.fade_ms, dur / 2);
  if (showing && dur - u < fade) alpha *= Math.max(0, (dur - u) / fade);
  // Lay the runs out: *starred* runs are hi_scale bigger and in the accent colour.
  const lines = [];
  let on = false;
  sctx.save();
  for (const row of rows) {
    const [pieces, after] = runsOf(row, on);
    on = after;
    const runs = pieces.map(([t, hi]) => { sctx.font = font(size * (hi ? C.hi_scale : 1)); return { t, hi, w: sctx.measureText(t).width }; });
    lines.push({ runs, w: runs.reduce((a, r) => a + r.w, 0) });
  }
  sctx.translate(x + dx, y + dy);
  sctx.rotate(-rot * Math.PI / 180);                 // ASS \frz turns anticlockwise
  sctx.scale(sx, sy);
  sctx.globalAlpha = alpha;
  sctx.textBaseline = "middle";
  sctx.lineJoin = "round";
  const top = -lines.length * lineH / 2, fill = hookHex(st.color), hi = hookHex(st.accent);
  const passes = st.look === "bubble" ? [["stroke", size * C.outer * 2, hookDark(fill)], ["stroke", size * C.inner * 2, "FFFFFF"], ["fill"]]
    : st.look === "box" ? [["box"], ["fill"]] : [["shadow"], ["stroke", size * 0.24, "000000"], ["fill"]];
  for (const [kind, lw, col] of passes) {
    lines.forEach((ln, r) => {
      const cy = top + lineH * (r + 0.5);
      if (kind === "box") {
        const pad = size * 0.28;
        sctx.fillStyle = "#" + hookHex(st.box);
        sctx.fillRect(-ln.w / 2 - pad, cy - lineH / 2 - pad * 0.4, ln.w + 2 * pad, lineH + pad * 0.8);
        return;
      }
      let cx = -ln.w / 2;
      for (const run of ln.runs) {
        const t = run.t;
        sctx.font = font(size * (run.hi ? C.hi_scale : 1));
        const wob = st.fx === "wave";
        const draw = (fn, s0, x0) => {
          if (!wob) { fn(s0, x0, cy); return; }
          let xx = x0;
          [...s0].forEach((c, i) => {
            const off = -C.wave[0] * size * Math.sin(2 * Math.PI * u / C.wave[1] - i * C.wave[2]);
            fn(c, xx, cy + off);
            xx += sctx.measureText(c).width;
          });
        };
        if (kind === "shadow") { sctx.fillStyle = "rgba(0,0,0,.5)"; draw((c, a, b) => sctx.fillText(c, a, b), t, cx + size * 0.05); }
        else if (kind === "stroke") { sctx.lineWidth = lw; sctx.strokeStyle = "#" + col; draw((c, a, b) => sctx.strokeText(c, a, b), t, cx); }
        else { sctx.fillStyle = "#" + (run.hi ? hi : fill); draw((c, a, b) => sctx.fillText(c, a, b), t, cx); }
        cx += run.w;
      }
    });
  }
  sctx.restore();
}
// While a zoom, shake or flash shows, a canvas over the video draws the view; otherwise
// it's hidden and you see the video (and its controls) as usual.
export function drawStage() {
  if (isShort()) return drawShort();
  const t = video.currentTime;
  const [xc, yc, h] = camera(t), fl = flashesAt(t);
  const show = !S.regionMode && video.videoWidth > 0 && video.readyState >= 2
    && (h < 0.4995 || Math.abs(xc - 0.5) > 1e-4 || Math.abs(yc - 0.5) > 1e-4 || fl.length > 0 || blurAt(t)
        || spotsAt(t).length > 0);
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
  drawSpots(cw, ch, xc, yc, h, t);
  for (const [rgb, alpha] of fl) {
    sctx.fillStyle = `rgba(${rgb.join(",")},${alpha})`;
    sctx.fillRect(0, 0, cw, ch);
  }
}
function spotsAt(t) {
  const picked = curFx();
  return S.regionMode ? [] : S.fx.filter(f => f.type === "spot" && keptFx(f) && (active(f, t) || f === picked));
}
export function spotBox(f, t) {       // map_box in edit/spots.py
  if (!isShort()) return [f.x, f.y, f.w, f.h];
  const cx = f.x + f.w / 2, cy = f.y + f.h / 2;
  for (const p of [...viewPanels(t)].reverse()) {          // a stack's zoom goes into the spotlight too
    if (p.blur) continue;
    const [sx, sy, sw, sh] = p.src, [dx, dy, dw, dh] = p.dst;
    if (sx <= cx && cx <= sx + sw && sy <= cy && cy <= sy + sh) {
      const kx = dw / sw, ky = dh / sh, ox = dx + (cx - sx) * kx, oy = dy + (cy - sy) * ky;
      return [ox - f.w * kx / 2, oy - f.h * ky / 2, f.w * kx, f.h * ky];
    }
  }
  return null;
}
function spotAlpha(f, t) {
  if (!active(f, t)) return 0.5;               // picked, but not at the playhead: shown faintly
  const [fi, fo] = S.D.spot_info.fade;
  return Math.max(0, Math.min(1, (t - f.t) / Math.min(fi, f.d / 2), (f.t + f.d - t) / Math.min(fo, f.d / 2)));
}
function drawSpots(cw, ch, xc, yc, h, t) {
  const info = S.D.spot_info, side = Math.min(cw, ch) / (2 * h);
  for (const f of spotsAt(t)) {
    const b = spotBox(f, t);
    if (!b) continue;
    const cx = (b[0] + b[2] / 2 - (xc - h)) / (2 * h) * cw, cy = (b[1] + b[3] / 2 - (yc - h)) / (2 * h) * ch;
    const rx = Math.max(b[2] / 2 / (2 * h) * cw, 2), ry = Math.max(b[3] / 2 / (2 * h) * ch, 2);
    sctx.save();
    sctx.globalAlpha = spotAlpha(f, t);
    if (f.style !== "circle") {
      sctx.save();
      sctx.translate(cx, cy);
      sctx.scale(rx, ry);
      const g = sctx.createRadialGradient(0, 0, 1, 0, 0, 1 + info.feather * side / Math.min(rx, ry));
      g.addColorStop(0, "rgba(0,0,0,0)");
      g.addColorStop(1, `rgba(0,0,0,${info.dim})`);
      sctx.fillStyle = g;
      sctx.fillRect(-cx / rx, -cy / ry, cw / rx, ch / ry);
      sctx.restore();
    }
    if (f.style !== "dim") {
      sctx.beginPath();
      sctx.ellipse(cx, cy, rx, ry, 0, 0, 2 * Math.PI);
      sctx.lineWidth = Math.max(3, info.ring * side);
      sctx.strokeStyle = "#" + (info.colors[f.color] || info.colors[info.default_color[f.style || "spotlight"]]);
      sctx.stroke();
    }
    sctx.restore();
  }
}
