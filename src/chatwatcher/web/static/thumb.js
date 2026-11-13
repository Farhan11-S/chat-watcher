// Moment page: the thumbnail maker. A thumbnail is layers (brand/layers.py): a background,
// then frames, her cut-out, text and stickers. The server draws each layer on its own
// (POST /moment/{pk}/thumb/sprite, brand/canvas.py); this page lays those pictures out,
// so moving, resizing and turning are instant, and asks again only when what a layer
// shows changes. Saving draws the same layers into thumbnail.jpg / short_cover.jpg.
// Claude's versions (the 'thumb' job, brand/thumbai.py) are layers too: pick one, then edit.
(function () {
  const box = document.querySelector("[data-thumb]");
  if (!box) return;
  const pk = box.dataset.thumb, $ = s => box.querySelector(s);
  const video = document.querySelector("[data-editor] video");
  const stage = $("[data-th-stage]");
  const KIND_NAMES = { bg: "Background", frame: "Frame", cutout: "Cut-out", text: "Text", sticker: "Sticker" };
  const STICKER_NAMES = { arrow: "Arrow", ring: "Ring", burst: "Burst", emoji: "Emoji", logo: "Logo", image: "Sticker" };
  const SNAP = 6;                      // px on screen
  const SPRITE_KEYS_IGNORED = new Set(["x", "y", "rot", "opacity", "hidden"]);

  let D = null;                        // what /api/moment/{pk}/thumb gave
  let layers = [], sel = null, dirty = false;
  let shape = "wide";
  try { if (localStorage.getItem("cw-th-shape") === "tall") shape = "tall"; } catch (e) {}
  const sprites = new Map();           // layer id → {key, url, nw, nh, w, error, busy, want}
  const undo = [], redo = [];
  let aiTimer = null;

  // ── small helpers ──────────────────────────────────────────────────────────
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function button(text, title, onclick, cls = "small") {
    const b = el("button", cls, text);
    b.type = "button";
    if (title) b.title = title;
    b.addEventListener("click", onclick);
    return b;
  }
  function fmt(t) {
    t = Math.max(0, t);
    const m = Math.floor(t / 60), s = (t - m * 60).toFixed(1);
    return m + ":" + (s < 10 ? "0" : "") + s;
  }
  const r4 = v => Math.round(v * 10000) / 10000;
  const clamp = (v, a, b) => Math.min(Math.max(v, a), b);
  const newId = () => Math.random().toString(16).slice(2, 10).padEnd(8, "0");
  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    let d = {};
    try { d = await r.json(); } catch (e) {}
    if (!r.ok || d.ok === false) throw new Error(d.error || ("HTTP " + r.status));
    return d;
  }
  const W = () => D.size[0], H = () => D.size[1];
  const byId = id => layers.find(l => l.id === id);
  const playhead = () => (video ? Math.round(video.currentTime * 1000) / 1000 : 0);

  function make(kind, fields = {}) {
    const ly = { id: newId(), kind };
    for (const [k, v] of Object.entries(D.spec[kind])) ly[k] = v[1];
    return Object.assign(ly, fields);
  }
  function grow(b) {                   // brand/cutout.py grow(): her region → the part to cut her from
    const cx = b.x + b.w / 2, w = Math.min(1, b.w + Math.min(b.w * 2, 0.25));
    const x = clamp(cx - w / 2, 0, 1 - w), y = Math.max(0, b.y - b.h * 0.35);
    return { sx: r4(x), sy: r4(y), sw: r4(w), sh: r4(1 - y) };
  }
  function herRegion() {
    const r = D.regions || {};
    return r.face ? "face" : Object.keys(r)[0] || "";
  }

  // ── history ────────────────────────────────────────────────────────────────
  function snapshot() {
    undo.push(JSON.stringify(layers));
    if (undo.length > 60) undo.shift();
    redo.length = 0;
  }
  function restore(json) {
    layers = JSON.parse(json);
    if (sel && !byId(sel)) sel = null;
    changed();
  }
  function changed(props = true) {
    dirty = true;
    $("[data-th-saved]").textContent = D.saved ? "changed since you saved" : "not saved yet";
    paint();
    paintList();
    if (props) paintProps();
  }

  // ── sprites ────────────────────────────────────────────────────────────────
  function spriteKey(ly) {
    const o = {};
    for (const k of Object.keys(ly).sort()) if (!SPRITE_KEYS_IGNORED.has(k)) o[k] = ly[k];
    return JSON.stringify(o) + shape;
  }
  let fetchTimer = null;
  function wantSprites(now = false) {
    clearTimeout(fetchTimer);
    fetchTimer = setTimeout(() => layers.forEach(fetchSprite), now ? 0 : 220);
  }
  async function fetchSprite(ly) {
    const key = spriteKey(ly);
    let s = sprites.get(ly.id);
    if (!s) { s = {}; sprites.set(ly.id, s); }
    if (s.key === key || s.busy === key) return;
    s.busy = key;
    paintBusy();
    try {
      const r = await fetch(`/moment/${pk}/thumb/sprite`, { method: "POST", headers: { "Content-Type": "application/json" },
                                                             body: JSON.stringify({ layer: ly, shape }) });
      if (!r.ok) {
        let msg = "HTTP " + r.status;
        try { msg = (await r.json()).error || msg; } catch (e) {}
        throw new Error(msg);
      }
      const url = URL.createObjectURL(await r.blob());
      const img = new Image();
      img.src = url;
      await img.decode();
      if (s.busy !== key) { URL.revokeObjectURL(url); return; }   // asked again meanwhile
      if (s.url) URL.revokeObjectURL(s.url);
      Object.assign(s, { key, url, nw: img.naturalWidth, nh: img.naturalHeight, w: ly.w, error: null });
    } catch (e) {
      if (s.busy !== key) return;
      Object.assign(s, { key, error: e.message || String(e) });
    } finally {
      if (s.busy === key) s.busy = null;
      paint();
      paintBusy();
    }
  }
  function paintBusy() {
    const n = [...sprites.values()].filter(s => s.busy).length;
    $("[data-th-busy]").hidden = !n;
  }

  // ── the stage ──────────────────────────────────────────────────────────────
  const view = { k: 1, w: 0, h: 0 };
  function measure() {
    const r = stage.getBoundingClientRect();
    view.w = r.width; view.h = r.height; view.k = r.width / W();
  }
  // A layer's box on screen: centre, size (px), from its sprite (or a guess before it comes).
  function screenBox(ly) {
    const s = sprites.get(ly.id) || {};
    const pad = D.pad * H();
    const cw = ly.w * W();
    let ch;
    if (s.nw) ch = (s.nh - 2 * pad) * (ly.w / s.w);
    else if (ly.kind === "text") ch = ly.size * H() * 1.1;
    else if (ly.kind === "sticker" && ly.shape === "arrow") ch = cw * 0.42;
    else ch = cw * (ly.kind === "sticker" ? ly.aspect : 0.75);
    return { cx: ly.x * view.w, cy: ly.y * view.h, w: cw * view.k, h: ch * view.k, s, pad };
  }
  function paint() {
    if (!D) return;
    measure();
    const bg = layers.find(l => l.kind === "bg");
    const bgImg = $("[data-th-bg]");
    const bs = bg && sprites.get(bg.id);
    bgImg.hidden = !(bg && bs && bs.url && !bg.hidden);
    if (bs && bs.url) bgImg.src = bs.url;
    stage.querySelectorAll(".th-l").forEach(e => { if (!byId(e.dataset.id)) e.remove(); });
    let z = 1;
    for (const ly of layers) {
      if (ly.kind === "bg") continue;
      let e = stage.querySelector(`.th-l[data-id="${ly.id}"]`);
      if (!e) {
        e = el("div", "th-l");
        e.dataset.id = ly.id;
        e.append(el("img"), el("span", "th-err"));
        e.querySelector("img").draggable = false;
        stage.insertBefore(e, $("[data-th-sel]"));
      }
      const b = screenBox(ly);
      e.style.zIndex = z++;
      e.hidden = ly.hidden;
      Object.assign(e.style, { left: b.cx + "px", top: b.cy + "px", width: b.w + "px", height: b.h + "px",
                               transform: `translate(-50%, -50%) rotate(${ly.rot}deg)`, opacity: ly.opacity });
      const img = e.querySelector("img");
      if (b.s.url && !b.s.error) {
        const sc = (ly.w / b.s.w) * view.k;
        img.hidden = false;
        if (img.getAttribute("src") !== b.s.url) img.src = b.s.url;
        Object.assign(img.style, { width: b.s.nw * sc + "px", height: b.s.nh * sc + "px",
                                   left: -b.pad * sc + "px", top: -b.pad * sc + "px" });
      } else img.hidden = true;
      e.classList.toggle("broken", !!b.s.error);
      e.querySelector(".th-err").textContent = b.s.error || "";
    }
    paintSel();
  }
  function paintSel() {
    const s = $("[data-th-sel]");
    const ly = sel && byId(sel);
    if (!ly || ly.kind === "bg" || ly.hidden) { s.hidden = true; return; }
    const b = screenBox(ly);
    s.hidden = false;
    s.classList.toggle("text", ly.kind === "text");
    Object.assign(s.style, { left: b.cx + "px", top: b.cy + "px", width: b.w + "px", height: b.h + "px",
                             transform: `translate(-50%, -50%) rotate(${ly.rot}deg)` });
  }
  function guides(xs, ys) {
    const g = $("[data-th-guides]");
    g.textContent = "";
    for (const x of xs) { const l = el("i", "v"); l.style.left = x * view.w + "px"; g.append(l); }
    for (const y of ys) { const l = el("i", "h"); l.style.top = y * view.h + "px"; g.append(l); }
  }

  // ── dragging ───────────────────────────────────────────────────────────────
  let drag = null;
  stage.addEventListener("pointerdown", ev => {
    if (!D || ev.button !== 0) return;
    measure();
    const handle = ev.target.closest("[data-h]");
    const layerEl = ev.target.closest(".th-l");
    const r = stage.getBoundingClientRect();
    const px = ev.clientX - r.left, py = ev.clientY - r.top;
    let ly;
    if (handle && sel) ly = byId(sel);
    else if (layerEl) { ly = byId(layerEl.dataset.id); select(ly.id); }
    else { ly = layers.find(l => l.kind === "bg"); if (ly) select(ly.id); }
    if (!ly) return;
    ev.preventDefault();
    stage.focus({ preventScroll: true });
    stage.setPointerCapture(ev.pointerId);
    const b = screenBox(ly);
    drag = { ly, mode: handle ? handle.dataset.h : (ly.kind === "bg" ? "pan" : "move"), px, py, b,
             start: JSON.stringify(layers), o: { ...ly }, moved: false };
  });
  stage.addEventListener("pointermove", ev => {
    if (!drag) return;
    const r = stage.getBoundingClientRect();
    const px = ev.clientX - r.left, py = ev.clientY - r.top;
    const dx = px - drag.px, dy = py - drag.py;
    if (!drag.moved && Math.hypot(dx, dy) < 3) return;
    drag.moved = true;
    const { ly, o, b } = drag;
    const free = ev.shiftKey;
    let gx = [], gy = [];
    if (drag.mode === "move") {
      let x = o.x + dx / view.w, y = o.y + dy / view.h;
      if (!free) {
        for (const t of [0.5, 1 / 3, 2 / 3]) {
          if (Math.abs(x - t) * view.w < SNAP) { x = t; gx.push(t); }
          if (Math.abs(y - t) * view.h < SNAP) { y = t; gy.push(t); }
        }
      }
      ly.x = r4(clamp(x, -0.5, 1.5)); ly.y = r4(clamp(y, -0.5, 1.5));
    } else if (drag.mode === "pan") {
      const k = 1 / (ly.zoom || 1);
      ly.cx = r4(clamp(o.cx - dx / view.w * k, 0, 1));
      ly.cy = r4(clamp(o.cy - dy / view.h * k, 0, 1));
      $("[data-th-bg]").style.transform = `translate(${dx}px, ${dy}px)`;
    } else if (drag.mode === "rot") {
      let a = Math.atan2(py - b.cy, px - b.cx) * 180 / Math.PI + 90;
      if (a > 180) a -= 360;
      if (!free) {
        const near = Math.round(a / 15) * 15;
        if (Math.abs(a - near) < 3) a = near;
      }
      ly.rot = Math.round(a * 10) / 10;
    } else if (drag.mode === "side") {        // a text's width: where it wraps
      const t = -ly.rot * Math.PI / 180;
      const lx = (px - b.cx) * Math.cos(t) - (py - b.cy) * Math.sin(t);
      ly.w = r4(clamp(Math.abs(lx) * 2 / view.w, 0.05, 1.5));
    } else {                                  // a corner: scale
      const d0 = Math.hypot(drag.px - b.cx, drag.py - b.cy), d1 = Math.hypot(px - b.cx, py - b.cy);
      const s = d1 / Math.max(4, d0);
      ly.w = r4(clamp(o.w * s, 0.02, 3));
      if (ly.kind === "text") ly.size = r4(clamp(o.size * s, 0.03, 0.5));
    }
    guides(gx, gy);
    paint();
  });
  function endDrag() {
    if (!drag) return;
    const d = drag;
    drag = null;
    guides([], []);
    $("[data-th-bg]").style.transform = "";
    if (!d.moved) return;
    undo.push(d.start);
    redo.length = 0;
    changed();
    wantSprites();
  }
  stage.addEventListener("pointerup", endDrag);
  stage.addEventListener("pointercancel", endDrag);
  stage.addEventListener("keydown", ev => {
    const ly = sel && byId(sel);
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "z") {
      ev.preventDefault();
      if (ev.shiftKey) { if (redo.length) { undo.push(JSON.stringify(layers)); restore(redo.pop()); wantSprites(true); } }
      else if (undo.length) { redo.push(JSON.stringify(layers)); restore(undo.pop()); wantSprites(true); }
      return;
    }
    if (!ly || ly.kind === "bg") return;
    if (ev.key === "Delete" || ev.key === "Backspace") { ev.preventDefault(); remove(ly); return; }
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "d") { ev.preventDefault(); duplicate(ly); return; }
    const step = ev.shiftKey ? 0.02 : 0.004;
    const mv = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[ev.key];
    if (mv) {
      ev.preventDefault();
      snapshot();
      ly.x = r4(ly.x + mv[0]); ly.y = r4(ly.y + mv[1]);
      changed(false);
    }
  });
  new ResizeObserver(() => paint()).observe(stage);

  // ── layer operations ───────────────────────────────────────────────────────
  function select(id) {
    sel = id;
    paintSel();
    paintList();
    paintProps();
  }
  function add(ly) {
    snapshot();
    layers.push(ly);
    sel = ly.id;
    changed();
    wantSprites(true);
  }
  function remove(ly) {
    snapshot();
    layers = layers.filter(l => l !== ly);
    sprites.delete(ly.id);
    sel = null;
    changed();
  }
  function duplicate(ly) {
    add({ ...JSON.parse(JSON.stringify(ly)), id: newId(), x: r4(ly.x + 0.03), y: r4(ly.y + 0.03) });
  }
  function move(ly, by) {
    const i = layers.indexOf(ly), j = i + by;
    if (j < 1 || j >= layers.length) return;     // the background stays first
    snapshot();
    [layers[i], layers[j]] = [layers[j], layers[i]];
    changed();
  }
  function set(ly, change, live = false) {
    if (!live) snapshot();
    Object.assign(ly, change);
    changed(!live);
    wantSprites();
  }
  function frameT() {
    const bg = layers.find(l => l.kind === "bg");
    return video && video.currentTime > 0 ? playhead() : (bg ? bg.t : 0);
  }
  const tall = () => shape === "tall";
  const ADD = {
    text: () => make("text", { text: "WHAT", x: 0.5, y: 0.2, w: tall() ? 0.8 : 0.6, size: tall() ? 0.07 : 0.16 }),
    cutout: () => {
      const r = D.regions[herRegion()];
      return make("cutout", Object.assign({ t: frameT(), x: tall() ? 0.5 : 0.72, y: tall() ? 0.6 : 0.62,
                                            w: tall() ? 0.8 : 0.45 }, r ? grow(r) : {}));
    },
    frame: () => make("frame", { t: frameT(), x: 0.74, y: 0.3, w: tall() ? 0.6 : 0.36 }),
    arrow: () => make("sticker", { shape: "arrow", x: 0.5, y: 0.5, w: 0.2, edge: 0.03 }),
    ring: () => make("sticker", { shape: "ring", x: 0.5, y: 0.45, w: 0.2, aspect: 0.8, edge: 0.012 }),
    emoji: () => make("sticker", { shape: "emoji", emoji: "😱", x: 0.82, y: 0.2, w: tall() ? 0.22 : 0.12, edge: 0 }),
    burst: () => make("sticker", { shape: "burst", colour: "yellow", outline_colour: "black", x: 0.25, y: 0.3, w: 0.3,
                                   aspect: 0.75, edge: 0.02 }),
    logo: () => make("sticker", { shape: "logo", x: 0.1, y: 0.12, w: 0.12, edge: 0 }),
  };
  box.addEventListener("click", ev => {
    const a = ev.target.closest("[data-th-add]");
    if (a && D) {
      const what = a.dataset.thAdd;
      if (what === "sticker") {
        const name = $("[data-th-sticker]").value;
        if (name) add(make("sticker", { shape: "image", image: name, x: 0.2, y: 0.75, w: 0.18 }));
        else location.href = "/brand#stickers";
      } else add(ADD[what]());
      return;
    }
    const sh = ev.target.closest("[data-th-shape]");
    if (sh && sh.dataset.thShape !== shape) {
      if (dirty && !confirm("Switch without saving this one?")) return;
      shape = sh.dataset.thShape;
      try { localStorage.setItem("cw-th-shape", shape); } catch (e) {}
      load();
      return;
    }
    const act = ev.target.closest("[data-th-act]");
    if (!act) return;
    ({ save, ai: askClaude, check: checkPicture })[act.dataset.thAct]?.();
  });

  // ── the layer list ─────────────────────────────────────────────────────────
  function label(ly) {
    if (ly.kind === "text") return "“" + (ly.text || "").replace(/\*/g, "").slice(0, 24) + "”";
    if (ly.kind === "sticker") return (STICKER_NAMES[ly.shape] || "Sticker") + (ly.shape === "emoji" ? " " + ly.emoji : ly.shape === "image" ? " " + ly.image.replace(/\.png$/, "") : "");
    if (ly.kind === "bg") return "Background · " + ({ frame: "frame " + fmt(ly.t), blur: "blurred " + fmt(ly.t), brand: "brand", colour: "colour" })[ly.source];
    return KIND_NAMES[ly.kind] + " " + fmt(ly.t);
  }
  function paintList() {
    const list = $("[data-th-layers]");
    list.textContent = "";
    for (const ly of [...layers].reverse()) {
      const row = el("div", "th-row" + (ly.id === sel ? " on" : "") + (ly.hidden ? " off" : ""));
      const name = button(label(ly), "Select", () => select(ly.id), "th-name");
      const eye = button(ly.hidden ? "◌" : "◉", ly.hidden ? "Show" : "Hide", () => set(ly, { hidden: !ly.hidden }), "small th-eye");
      const err = (sprites.get(ly.id) || {}).error;
      row.append(name);
      if (err) row.append(el("span", "th-warn", "⚠"));
      row.append(eye);
      list.append(row);
    }
  }

  // ── the selected layer's settings ──────────────────────────────────────────
  function field(labelText, input, hint) {
    const l = el("label", "th-f");
    const s = el("span", null, labelText);
    if (hint) s.title = hint;
    l.append(s, input);
    return l;
  }
  function slider(ly, k, lo, hi, step = 0.005, show = v => v.toFixed(2)) {
    const spec = D.spec[ly.kind][k];
    lo = lo ?? spec[2]; hi = hi ?? spec[3];
    const wrap = el("span", "th-slide");
    const i = el("input"), v = el("small", "mono", show(ly[k]));
    Object.assign(i, { type: "range", min: lo, max: hi, step, value: ly[k] });
    let first = true;
    i.addEventListener("input", () => {
      if (first) { snapshot(); first = false; }
      ly[k] = +i.value;
      v.textContent = show(ly[k]);
      changed(false);
      wantSprites();
    });
    i.addEventListener("change", () => { first = true; });
    wrap.append(i, v);
    return wrap;
  }
  function select_(ly, k, options, names = {}) {
    const s = el("select");
    for (const o of options) { const op = el("option", null, names[o] || o); op.value = o; s.append(op); }
    s.value = ly[k];
    s.addEventListener("change", () => set(ly, { [k]: s.value }));
    return s;
  }
  function check(ly, k) {
    const c = el("input");
    c.type = "checkbox";
    c.checked = !!ly[k];
    c.addEventListener("change", () => set(ly, { [k]: c.checked }));
    return c;
  }
  function colour(ly, k) {
    const wrap = el("span", "th-colours");
    const named = ["white", "black", "red", "yellow", "orange", "green", "blue", "pink", "deep", "purple", "lavender", "accent"];
    for (const n of named) {
      const b = button("", n, () => set(ly, { [k]: n }), "th-sw" + (ly[k] === n ? " on" : ""));
      b.style.background = D.colours[n];
      wrap.append(b);
    }
    const c = el("input");
    c.type = "color";
    c.value = (ly[k].startsWith("#") ? ly[k] : D.colours[ly[k]] || "#ffffff").toLowerCase();
    c.title = "Any colour";
    c.addEventListener("change", () => set(ly, { [k]: c.value.toUpperCase() }));
    wrap.append(c);
    return wrap;
  }
  function timePicker(ly) {
    const wrap = el("span", "th-time");
    const s = el("select");
    const cur = el("option", null, "at " + fmt(ly.t));
    cur.value = "";
    s.append(cur);
    for (const f of D.frames) {
      const o = el("option", null, `frame ${f.n} · ${fmt(f.t)}${f.peak ? " ★" : ""}`);
      o.value = f.t;
      s.append(o);
    }
    s.addEventListener("change", () => { if (s.value !== "") set(ly, { t: +s.value }); });
    wrap.append(s, button("Playhead", "The frame at the editor's playhead", () => set(ly, { t: playhead() })));
    if (video) wrap.append(button("▶ Show", "Seek the editor's player to this frame", () => { video.currentTime = ly.t; }));
    return wrap;
  }
  function regionPicker(ly, cutout) {
    const s = el("select");
    const names = Object.keys(D.regions || {});
    const opt = (v, t) => { const o = el("option", null, t); o.value = v; s.append(o); };
    opt("", "…");
    for (const n of names) opt(n, n);
    opt("*", "the whole frame");
    s.addEventListener("change", () => {
      if (!s.value) return;
      const r = s.value === "*" ? { x: 0, y: 0, w: 1, h: 1 } : D.regions[s.value];
      const crop = s.value === "*" ? { sx: 0, sy: 0, sw: 1, sh: 1 }
        : cutout ? grow(r) : { sx: r4(r.x), sy: r4(r.y), sw: r4(r.w), sh: r4(r.h) };
      set(ly, crop);
    });
    return s;
  }
  function paintProps() {
    const p = $("[data-th-props]");
    p.textContent = "";
    const ly = sel && byId(sel);
    if (!ly) { p.append(el("p", "faint", "Click a layer on the picture or in the list.")); return; }
    const head = el("div", "th-props-head");
    head.append(el("b", null, KIND_NAMES[ly.kind] + (ly.kind === "sticker" ? " · " + (STICKER_NAMES[ly.shape] || "") : "")));
    if (ly.kind !== "bg") {
      head.append(button("↑", "Bring forward", () => move(ly, 1)), button("↓", "Send back", () => move(ly, -1)),
                  button("⧉", "Duplicate (Ctrl+D)", () => duplicate(ly)), button("✕", "Delete (Del)", () => remove(ly)));
    }
    p.append(head);
    const f = (...a) => p.append(field(...a));
    if (ly.kind === "bg") {
      f("Shows", select_(ly, "source", ["frame", "blur", "brand", "colour"],
                         { frame: "a frame", blur: "a frame, blurred", brand: "your brand gradient", colour: "one colour" }));
      if (ly.source === "frame" || ly.source === "blur") {
        f("Frame", timePicker(ly));
        f("Zoom", slider(ly, "zoom", 1, 4, 0.01, v => v.toFixed(2) + "×"), "Drag the picture to move it");
        if (ly.source === "blur") f("Blur", slider(ly, "blur", 0, 0.1, 0.002));
      }
      if (ly.source === "colour") f("Colour", colour(ly, "colour"));
      f("Darken", slider(ly, "dim", 0, 0.9, 0.01, v => Math.round(v * 100) + "%"));
      f("Colour boost", slider(ly, "sat", 0, 2, 0.01, v => v.toFixed(2) + "×"));
      p.append(el("p", "faint", "Drag the picture to move what it shows."));
    } else if (ly.kind === "text") {
      const t = el("textarea");
      Object.assign(t, { rows: 2, maxLength: 80, value: ly.text });
      let first = true;
      t.addEventListener("input", () => { if (first) { snapshot(); first = false; } ly.text = t.value; changed(false); wantSprites(); });
      t.addEventListener("change", () => { first = true; paintList(); });
      f("Words", t, "*stars* make a word the accent colour; Enter starts a new line");
      f("Style", select_(ly, "style", ["stroke", "box", "shadow"], { stroke: "outlined", box: "on a box", shadow: "soft shadow" }));
      f("Size", slider(ly, "size", 0.03, 0.5, 0.002));
      f("Colour", colour(ly, "colour"));
      f("*Accent*", colour(ly, "accent"));
      if (ly.style === "box") f("Box", colour(ly, "box_colour"));
      else {
        f("Outline", slider(ly, "outline", 0, 0.35, 0.005));
        f("Outline colour", colour(ly, "outline_colour"));
      }
      f("Font", select_(ly, "font", ["title", "text"], { title: "title font", text: "text font" }));
      f("Align", select_(ly, "align", ["left", "center", "right"]));
    } else if (ly.kind === "cutout") {
      if (!D.cutouts) p.append(el("p", "warn", "The cut-out model isn't downloaded yet: ✨ Claude's versions fetches it."));
      f("Frame", timePicker(ly));
      f("Who", regionPicker(ly, true), "Cut her out of this region (grown to take in her body)");
      f("Outline", slider(ly, "outline", 0, 0.05, 0.001));
      f("Outline colour", colour(ly, "outline_colour"));
      f("Glow", slider(ly, "glow", 0, 1, 0.01));
      if (ly.glow > 0) f("Glow colour", colour(ly, "glow_colour"));
      f("Shadow", check(ly, "shadow"));
      f("Mirror", check(ly, "flip"));
    } else if (ly.kind === "frame") {
      f("Frame", timePicker(ly));
      f("Shows", regionPicker(ly, false));
      f("Shape", select_(ly, "shape", ["rect", "round", "circle"], { rect: "box", round: "rounded box", circle: "circle" }));
      f("Border", slider(ly, "border", 0, 0.05, 0.001));
      f("Border colour", colour(ly, "border_colour"));
      f("Shadow", check(ly, "shadow"));
      f("Mirror", check(ly, "flip"));
    } else {
      if (ly.shape === "emoji") {
        const e = el("input");
        Object.assign(e, { value: ly.emoji, maxLength: 8, size: 4 });
        e.addEventListener("change", () => set(ly, { emoji: e.value.trim() || "😱" }));
        f("Emoji", e);
      }
      if (ly.shape === "image") f("Picture", select_(ly, "image", D.stickers.length ? D.stickers : [ly.image]));
      if (["arrow", "ring", "burst"].includes(ly.shape)) f("Colour", colour(ly, "colour"));
      if (["ring", "burst"].includes(ly.shape)) f("Height", slider(ly, "aspect", 0.15, 3, 0.01, v => v.toFixed(2) + "× width"));
      f("Edge", slider(ly, "edge", 0, 0.12, 0.002));
      if (ly.edge > 0) f("Edge colour", colour(ly, "outline_colour"));
      if (ly.shape !== "ring") f("Mirror", check(ly, "flip"));
    }
    if (ly.kind !== "bg") {
      f("Turn", slider(ly, "rot", -180, 180, 0.5, v => v.toFixed(0) + "°"));
      f("Opacity", slider(ly, "opacity", 0.05, 1, 0.01, v => Math.round(v * 100) + "%"));
    }
    const err = (sprites.get(ly.id) || {}).error;
    if (err) p.append(el("p", "warn", err));
  }

  // ── Claude's versions ──────────────────────────────────────────────────────
  function paintAI(ai, ideas) {
    const a = $("[data-th-ai]");
    a.textContent = "";
    const state = ai ? ai.state : "none";
    const mine = !ai || !ai.shape || ai.shape === shape;
    const running = mine && (state === "queued" || state === "running");
    $('[data-th-act="ai"]').disabled = running;
    if (running) {
      a.append(el("p", "muted", state === "queued"
        ? "Waiting for the worker" + (ai.behind ? ` (busy with ${ai.behind})` : "") + "…"
        : "Claude is composing versions from the clip's frames… (about a minute)"));
    } else if (mine && state === "error") {
      a.append(el("p", "warn", "Claude's versions failed: " + (ai.error || "")));
    }
    const vs = (ideas && ideas.versions) || [];
    if (!vs.length) { a.hidden = !a.childElementCount; return; }
    a.hidden = false;
    const grid = el("div", "th-ideas");
    vs.forEach((v, i) => {
      const card = el("figure", "th-idea");
      if (v.url) { const img = el("img"); img.src = v.url; img.alt = v.name; img.loading = "lazy"; card.append(img); }
      const cap = el("figcaption");
      cap.append(el("b", null, `${i + 1}. ${v.name}`), el("small", "muted", " " + v.why));
      if (v.warnings && v.warnings.length) {
        const w = el("details");
        w.append(el("summary", "faint", `${v.warnings.length} note(s)`));
        v.warnings.forEach(x => w.append(el("small", "faint", x), el("br")));
        cap.append(w);
      }
      cap.append(button("Use this", "Put this version in the editor (you can then change anything)", () => {
        if (dirty && !confirm("Replace your unsaved changes with this version?")) return;
        snapshot();
        layers = JSON.parse(JSON.stringify(v.layers));
        sel = null;
        changed();
        wantSprites(true);
      }, "small primary"));
      card.append(cap);
      grid.append(card);
    });
    a.append(grid);
    if (ideas.note) a.append(el("p", "faint", "Claude: " + ideas.note));
  }
  async function pollAI() {
    clearTimeout(aiTimer);
    let d;
    try { d = await (await fetch(`/api/moment/${pk}/thumb/ai?shape=${shape}`, { cache: "no-store" })).json(); }
    catch (e) { aiTimer = setTimeout(pollAI, 5000); return; }
    if (!d.ok) return;
    D.ideas = d.ideas;
    paintAI(d.ai, d.ideas);
    const busy = d.ai.state === "queued" || d.ai.state === "running";
    if (busy) aiTimer = setTimeout(pollAI, 3000);
    else if (d.ai.state === "done" && d.ideas.versions.length && !dirty && D.source !== "saved") {
      layers = JSON.parse(JSON.stringify(d.ideas.versions[0].layers));
      sel = null;
      paint(); paintList(); paintProps();
      wantSprites(true);
    }
  }
  async function askClaude() {
    try {
      const d = await post(`/moment/${pk}/thumb/ai`, { shape });
      paintAI(d.ai, D.ideas);
      aiTimer = setTimeout(pollAI, 2000);
    } catch (e) {
      $("[data-th-ai]").hidden = false;
      $("[data-th-ai]").textContent = "Couldn't ask Claude: " + e.message;
    }
  }

  // ── saving ─────────────────────────────────────────────────────────────────
  function paintSaved() {
    const links = $("[data-th-links]");
    links.textContent = "";
    $("[data-th-save]").textContent = "Save " + (shape === "tall" ? "short_cover.jpg" : "thumbnail.jpg");
    if (!D.saved) { $("[data-th-saved]").textContent = dirty ? "not saved yet" : (D.source === "claude" ? "Claude's first version, not saved yet" : "not saved yet"); return; }
    $("[data-th-saved]").textContent = dirty ? "changed since you saved" : "saved";
    const dl = el("a", "btn small", "⬇ Download");
    dl.href = D.saved.url.replace("?", "?dl=1&");
    const copy = button("copy path", "Copy the file's path on this computer", () => {});
    copy.dataset.copy = D.saved.path;
    const look = el("a", "btn small", "Open");
    look.href = D.saved.url;
    look.target = "_blank";
    links.append(dl, look, copy);
  }
  async function save() {
    const b = $("[data-th-save]");
    b.disabled = true;
    try {
      const d = await post(`/moment/${pk}/thumb`, { layers, shape });
      D = { ...D, ...d };
      layers = d.layers;
      dirty = false;
      paintSaved();
      if (d.warnings && d.warnings.length) $("[data-th-saved]").textContent = "saved, but: " + d.warnings.join(" ");
    } catch (e) {
      $("[data-th-saved]").textContent = "Couldn't save: " + e.message;
    } finally {
      b.disabled = false;
    }
  }
  async function checkPicture() {              // the server's own drawing, to compare with the stage
    const w = window.open("", "_blank");
    try {
      const r = await fetch(`/moment/${pk}/thumb/preview`, { method: "POST", headers: { "Content-Type": "application/json" },
                                                              body: JSON.stringify({ layers, shape }) });
      if (!r.ok) throw new Error("HTTP " + r.status);
      if (w) w.location = URL.createObjectURL(await r.blob());
    } catch (e) { if (w) w.close(); alert("Couldn't draw it: " + e.message); }
  }

  // ── loading ────────────────────────────────────────────────────────────────
  async function load() {
    const want = shape;
    let d;
    try { d = await (await fetch(`/api/moment/${pk}/thumb?shape=${want}`, { cache: "no-store" })).json(); }
    catch (e) { d = { ok: false, error: "Couldn't reach the dashboard." }; }
    if (want !== shape) return;
    if (!d.ok) { $("[data-th-saved]").textContent = d.error || "Couldn't load the thumbnail."; return; }
    D = d;
    layers = d.layers;
    sel = null;
    dirty = false;
    undo.length = redo.length = 0;
    sprites.forEach(s => s.url && URL.revokeObjectURL(s.url));
    sprites.clear();
    stage.querySelectorAll(".th-l").forEach(e => e.remove());
    box.querySelectorAll("[data-th-shape]").forEach(b => {
      b.classList.toggle("on", b.dataset.thShape === shape);
      b.setAttribute("aria-selected", b.dataset.thShape === shape ? "true" : "false");
    });
    stage.classList.toggle("tall", shape === "tall");
    stage.style.aspectRatio = `${W()} / ${H()}`;
    const st = $("[data-th-sticker]");
    st.textContent = "";
    const o0 = el("option", null, D.stickers.length ? "+ Sticker…" : "+ Sticker (add some on Brand)");
    o0.value = "";
    st.append(o0);
    for (const s of D.stickers) { const o = el("option", null, s.replace(/\.png$/, "")); o.value = s; st.append(o); }
    $("[data-th-help]").textContent = shape === "tall"
      ? `The Short's cover, its background in the Short's layout (${D.layout}${D.short_saved ? "" : ", the one a new Short starts with"}). YouTube covers the bottom quarter and the right edge.`
      : "YouTube puts the video's length in the bottom-right corner: keep it clear.";
    paint(); paintList(); paintProps(); paintSaved();
    paintAI(d.ai, d.ideas);
    wantSprites(true);
    if (d.ai && (d.ai.state === "queued" || d.ai.state === "running")) aiTimer = setTimeout(pollAI, 3000);
  }
  $("[data-th-sticker]").addEventListener("change", ev => {
    if (ev.target.value) { add(make("sticker", { shape: "image", image: ev.target.value, x: 0.2, y: 0.75, w: 0.18 })); ev.target.value = ""; }
  });
  window.addEventListener("cw-short-changed", () => { if (shape === "tall" && !dirty) load(); });
  window.addEventListener("beforeunload", ev => { if (dirty) { ev.preventDefault(); ev.returnValue = ""; } });
  load();
})();
