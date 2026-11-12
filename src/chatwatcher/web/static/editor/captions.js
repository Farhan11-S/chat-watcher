// Subtitles over the video: who says each line and their colour, and the caption styles
// (edit/captions.py) drawn like the render.
import { ASS_TO_CSS, S, subLayer, video } from "./state.js";
import { el, runsOf, subWidth, wrapSub } from "./util.js";
import { isShort, mapPoint, place, shortPanels, viewBox } from "./preview.js";

// ── people (collabs): who says each line, a colour each; lines stacked when two talk at once ──
export const V = () => S.D.voices || { count: 0, map: {}, people: {}, palette: [] };
export function voiceColor(k) {     // a voice's colour; voices with one name are one person: the first one's
  const v = V(), m = v.map || {}, me = m[k] || {}, name = (me.name || "").toLowerCase();
  if (name) for (let j = 0; j < v.count; j++)
    if (((m[j] || {}).name || "").toLowerCase() === name) return (m[j] || {}).color || me.color || "FFFFFF";
  const pal = v.palette || [];
  return me.color || pal[k % Math.max(1, pal.length)] || "FFFFFF";
}
export const voiceName = k => ((V().map || {})[k] || {}).name || `Person ${k + 1}`;
// Caption text: the readable shade the server made of a dark colour (speakers.readable).
export const voiceText = k => { const m = (V().map || {})[k] || {}; return m.text && m.color === voiceColor(k) ? m.text : voiceColor(k); };
export const cueColor = c => (c.speaker != null ? voiceText(c.speaker) : null);
export const subsAt = t => S.subs.filter(c => c.t0 <= t && t < c.t1).sort((a, b) => a.t0 - b.t0 || a.t1 - b.t1);

// ── caption styles (edit/captions.py): the bubble look, and the effect / font / place a
// caption style gives the captions that start while it lasts. Built when what's on screen
// changes; moved every frame (capAnim) the way libass's \t tags move them in the render.
export const CAPI = () => (S.D && S.D.sub_layout && S.D.sub_layout.captions) || null;
const capMark = t0 => {        // the latest caption style covering a caption's start (captions.mark_for)
  let on = null;
  for (const f of S.fx) if (f.type === "caption" && f.t - 0.02 <= t0 && t0 < f.t + f.d && (!on || f.t >= on.t)) on = f;
  return on;
};
const capStyled = shown => !!CAPI() && (S.subStyle.look === "bubble" || shown.some(c => capMark(c.t0)));
function hlsOf(rgb) {
  const [r, g, b] = [0, 2, 4].map(i => parseInt(rgb.slice(i, i + 2), 16) / 255);
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2;
  if (mx === mn) return [0, l, 0];
  const d = mx - mn, s = l <= 0.5 ? d / (mx + mn) : d / (2 - mx - mn);
  const h = mx === r ? (g - b) / d + (g < b ? 6 : 0) : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return [h / 6, l, s];
}
function rgbOf(h, l, s) {
  const hex = v => Math.round(v * 255).toString(16).padStart(2, "0").toUpperCase();
  if (!s) return hex(l) + hex(l) + hex(l);
  const q = l <= 0.5 ? l * (1 + s) : l + s - l * s, p = 2 * l - q;
  const f = t => { t = ((t % 1) + 1) % 1; return t < 1 / 6 ? p + (q - p) * 6 * t : t < 0.5 ? q : t < 2 / 3 ? p + (q - p) * (2 / 3 - t) * 6 : p; };
  return hex(f(h + 1 / 3)) + hex(f(h)) + hex(f(h - 1 / 3));
}
function capDeep(rgb) {        // captions.deep: a voice's pastel as a fill inside a white outline
  const C = CAPI();
  if (!rgb || !/^[0-9A-Fa-f]{6}$/.test(rgb)) return C.default_fill;
  const [h, l, s] = hlsOf(rgb);
  return s < 0.12 ? C.default_fill : rgbOf(h, Math.min(l, 0.38), Math.max(s, 0.6));
}
const capRing = fill => { const [h, l, s] = hlsOf(fill); return rgbOf(h, l * 0.38, s); };
const capAccent = fill => { const h = hlsOf(fill)[0]; return h < 0.05 || h > 0.85 ? "2F6BE6" : "E62E6B"; };
function capFamily(key, text, bubble) {      // captions.font_for
  const C = CAPI(), fall = `"${S.D.sub_layout.font}", "Noto Sans CJK JP", "Noto Sans JP", system-ui, sans-serif`;
  if (!key && !bubble) return fall;
  if (subWidth(text) !== text.replace(/\*/g, "").length) return `"${C.cjk_font}", ${fall}`;
  return `"${C.fonts[key || "round"] || C.fonts.round}", ${fall}`;
}
function capSeam(t) {
  if (!isShort()) return 0.5;
  const ys = new Set();
  for (const p of shortPanels(t)) if (!p.blur) { ys.add(+p.dst[1].toFixed(4)); ys.add(+(p.dst[1] + p.dst[3]).toFixed(4)); }
  const inner = [...ys].filter(y => y > 0.12 && y < 0.88);
  return inner.length ? inner.reduce((a, b) => (Math.abs(b - 0.5) < Math.abs(a - 0.5) ? b : a)) : 0.5;
}
function capAnchor(at, t) {    // captions.anchor
  if (at === "middle") return [0.5, 0.5];
  if (at === "seam") return [0.5, capSeam(t)];
  const b = at && !CAPI().places.includes(at) ? S.D.regions[at] : null;
  if (!b) return null;
  const cx = b.x + b.w / 2, low = b.y + b.h * CAPI().low;
  if (!isShort()) return [cx, low];
  const ps = shortPanels(t);
  return mapPoint(cx, low, ps) || mapPoint(cx, b.y + b.h / 2, ps);
}
let capRecs = [];
function drawCaptions(shown, box, titled) {
  const C = CAPI(), L = S.D.sub_layout, bubble = S.subStyle.look === "bubble", W = box.width, H = box.height;
  const size = L.sizes[S.subStyle.size] * (L.vertical ? W : H) * (bubble ? C.bubble_scale : 1);
  const topEdge = H * (titled ? L.top_title : L.top), bottomEdge = H * (1 - L.bottom);
  const stacked = { top: 0, bottom: 0 };
  const items = shown.map((c, n) => {      // captions.events: where each goes and how it looks
    const m = capMark(c.t0), fxName = m ? m.fx || "pop" : (bubble ? "pop" : "none");
    const k = (m && (C.sizes || {})[m.size]) || 1, sz = size * k;       // bigger text wraps sooner, so it still fits
    const rows = wrapSub(c.text, Math.max(6, Math.floor(L.max_line / Math.max(k, 1))), L.max_lines);
    const height = rows.length * sz * C.line_h;
    let edge = m && (m.at === "top" || m.at === "bottom") ? m.at : null;
    const xy = !edge && m && m.at ? capAnchor(m.at, c.t0) : null;
    if (!edge && !xy) edge = S.subStyle.position === "top" ? "top" : "bottom";
    let x, y, an;
    if (edge) {
      const below = stacked[edge];
      stacked[edge] += height;
      x = W / 2; y = edge === "top" ? topEdge + below : bottomEdge - below; an = edge === "top" ? 8 : 2;
    } else {
      const widest = Math.max(...rows.map(subWidth)) * C.char_w * sz * (c.text.includes("*") ? C.hi_scale : 1);
      const hw = Math.min(widest / 2 + sz * 0.3, W / 2);
      x = Math.min(Math.max(xy[0] * W, hw), W - hw);
      y = Math.min(Math.max(xy[1] * H, height / 2 + H * 0.04), H * 0.96 - height / 2);
      an = 5;
    }
    const col = cueColor(c), fill = bubble ? capDeep(col) : (col || "FFFFFF").toUpperCase();
    return { c, fx: fxName, rows, x, y, an, size: sz, fill, family: capFamily(m ? m.font : (bubble ? "round" : ""), c.text, bubble),
             seed: n + 1 + Math.round(c.t0 * 100) };
  });
  const key = "cap|" + JSON.stringify(items.map(i => [i.c.id, i.c.text, i.fx, Math.round(i.x), Math.round(i.y), i.an, i.fill, i.family,
                                                        Math.round(i.size)]))
              + [S.subStyle.look, Math.round(size), box.left, box.top].join("|");
  if (key !== S.subKey) {
    S.subKey = key;
    subLayer.hidden = !items.length;
    subLayer.textContent = "";
    capRecs = [];
    if (items.length) place(subLayer, box);
    for (const it of items) {
      const wrap = el("div", "ed-cap"), letters = [];
      Object.assign(wrap.style, { left: it.x + "px", top: it.y + "px", fontFamily: it.family,
                                  fontSize: it.size * ASS_TO_CSS + "px", lineHeight: it.size + "px",
                                  transformOrigin: { 2: "50% 100%", 8: "50% 0", 5: "50% 50%" }[it.an] });
      const layers = bubble
        ? [[capRing(it.fill), capRing(it.fill), `${it.size * C.outer * 2}px #${capRing(it.fill)}`, ""],
           [it.fill, capAccent(it.fill), `${it.size * C.inner * 2}px #ffffff`, ""]]
        : [[it.fill, it.fill.toUpperCase() === L.highlight.toUpperCase() ? "FFFFFF" : L.highlight,
            `${it.size * C.classic_bord * 2}px #000`, `${it.size * 0.045}px ${it.size * 0.045}px 0 rgba(0,0,0,.5)`]];
      layers.forEach(([fill, hi, stroke, shadow], k) => {
        const copy = el("div", k === 0 && layers.length > 1 ? "ed-cap-back" : "");
        Object.assign(copy.style, { color: "#" + fill, WebkitTextStroke: stroke, textShadow: shadow });
        let on = false, i = 0;
        it.rows.forEach((row, r) => {
          if (r) copy.appendChild(el("br"));
          const [pieces, after] = runsOf(row, on);
          on = after;
          for (const [text, h] of pieces) {
            const add = (t, parent) => {
              const sp = el("span", "", t);
              if (h) Object.assign(sp.style, { color: "#" + hi, fontSize: C.hi_scale + "em" });
              parent.appendChild(sp);
              return sp;
            };
            if (it.fx === "wave") for (const ch of text) { const sp = add(ch, copy); Object.assign(sp.style, { display: "inline-block", whiteSpace: "pre" }); (letters[i] = letters[i] || []).push(sp); i++; }
            else add(text, copy);
          }
        });
        wrap.appendChild(copy);
      });
      subLayer.appendChild(wrap);
      capRecs.push({ el: wrap, letters, fx: it.fx, t0: it.c.t0, t1: it.c.t1, size: it.size, seed: it.seed,
                     anchor: { 2: "translate(-50%, -100%)", 8: "translate(-50%, 0)", 5: "translate(-50%, -50%)" }[it.an] });
    }
  }
  capAnim(video.currentTime);
}
function capAnim(t) {          // the \t / \move / shake steps of captions.events, at time t
  const C = CAPI(), P = C.pop_ms, h = P / 2;
  for (const r of capRecs) {
    const u = Math.max(0, (t - r.t0) * 1000), dur = (r.t1 - r.t0) * 1000;
    let sx = 1, sy = 1, dx = 0, dy = 0, rot = 0;
    if (["pop", "shake", "wave", "wobble", "grow"].includes(r.fx) && u < P) sx = sy = u < h ? 0.4 + 0.75 * u / h : 1.15 - 0.15 * (u - h) / h;
    if (r.fx === "grow" && u >= P) sx = sy = 1 + (C.grow_to - 1) * Math.min(1, (u - P) / Math.max(1, dur - P));
    if (r.fx === "bounce") {
      const B = C.bounce_ms;
      if (u < B) dy = -r.size * 1.2 * (1 - u / B);
      else if (u < B + 60) { const k = (u - B) / 60; sx = 1 + 0.25 * k; sy = 1 - 0.22 * k; }
      else if (u < B + 170) { const k = (u - B - 60) / 110; sx = 1.25 - 0.25 * k; sy = 0.78 + 0.22 * k; }
    }
    if (r.fx === "shake" && u >= P) {
      const [step, amp, tilt] = C.shake, k = Math.floor((u - P) / step);
      const j = n => { const v = Math.sin(r.seed * 12.9898 + k * 78.233 + n * 37.719) * 43758.5453; return (v - Math.floor(v)) * 2 - 1; };
      dx = j(1) * amp * r.size; dy = j(2) * amp * r.size; rot = -j(3) * tilt;
    }
    if (r.fx === "wobble") rot = -C.wobble[0] * Math.sin(2 * Math.PI * u / C.wobble[1]);
    r.el.style.transform = `${r.anchor} translate(${dx}px, ${dy}px) rotate(${rot}deg) scale(${sx}, ${sy})`;
    if (r.fx === "wave") {
      const [amp, per, ph] = C.wave;
      r.letters.forEach((ls, i) => {
        const off = -amp * r.size * Math.sin(2 * Math.PI * u / per - i * ph);
        for (const l of ls) l.style.transform = `translateY(${off}px)`;
      });
    }
  }
}

export function drawSubPreview() {
  const shown = !S.regionMode && S.subStyle.on && video.videoWidth ? subsAt(video.currentTime) : [];
  const box = viewBox(), L = S.D.sub_layout, titled = isShort() && !!S.layout.title;
  if (shown.length && capStyled(shown)) { drawCaptions(shown, box, titled); return; }
  capRecs = [];
  const key = shown.length ? [shown.map(c => `${c.id}:${c.text}:${cueColor(c) || ""}`).join(","), S.subStyle.size,
                              S.subStyle.position, Math.round(box.width), Math.round(box.height), box.left, titled].join("|") : "";
  if (key === S.subKey) return;
  S.subKey = key;
  subLayer.hidden = !shown.length;
  if (!shown.length) return;
  place(subLayer, box);
  const size = L.sizes[S.subStyle.size] * (L.vertical ? box.width : box.height);      // libass size in screen pixels
  const top = S.subStyle.position === "top";
  const inner = el("div", "ed-subs-text");
  Object.assign(inner.style, {
    fontFamily: `"${S.D.sub_layout.font}", "Noto Sans CJK JP", "Noto Sans JP", system-ui, sans-serif`,
    fontSize: size * ASS_TO_CSS + "px", lineHeight: size + "px", WebkitTextStroke: size * 0.2 + "px #000",
    textShadow: `${size * 0.045}px ${size * 0.045}px 0 rgba(0,0,0,.5)`,
    display: "flex", flexDirection: top ? "column" : "column-reverse",
    [top ? "top" : "bottom"]: box.height * (top ? (titled ? L.top_title : L.top) : L.bottom) + "px",
  });
  const hiOf = col => "#" + (col && col.toUpperCase() === S.D.sub_layout.highlight.toUpperCase() ? "FFFFFF" : S.D.sub_layout.highlight);
  for (const c of shown) {
    const col = cueColor(c), block = el("div");
    let on = false;
    wrapSub(c.text, L.max_line, L.max_lines).forEach((line, k) => {
      if (k) block.appendChild(el("br"));
      const [pieces, after] = runsOf(line, on);
      on = after;
      for (const [text, hi] of pieces) {
        const sp = el("span", "", text);
        if (hi) sp.style.color = hiOf(col);
        else if (col) sp.style.color = "#" + col;
        block.appendChild(sp);
      }
    });
    inner.appendChild(block);
  }
  subLayer.textContent = "";
  subLayer.appendChild(inner);
}
