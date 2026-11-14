// chat-watcher dashboard, every page: auto-refresh, chart hover, the stream page's review
// player, storage, copy buttons, colour hex boxes; then the list tools (theme, auto-submit
// filters, in-page filter, sortable tables, tabs, menus) at the end.
(function () {
  // Reload the page when the worker changes something (only if the user isn't typing
  // or watching a video).
  let version = null;
  async function poll() {
    try {
      const r = await fetch("/api/version", { cache: "no-store" });
      const d = await r.json();
      const pill = document.getElementById("worker-pill");
      if (pill) pill.textContent = "worker: " + d.worker;
      if (version !== null && d.v !== version && document.body.dataset.autorefresh === "1") {
        const busy = document.activeElement && ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName);
        const playing = [...document.querySelectorAll("video")].some(v => !v.paused);
        if (!busy && !playing && !window.cwHold) location.reload();
        else if (window.cwHold) showUpdateNotice();
      }
      version = d.v;
    } catch (e) { /* server restarting */ }
  }
  // While you review on the YouTube player, don't reload under it; offer a button instead.
  function showUpdateNotice() {
    if (document.querySelector(".update-notice")) return;
    const b = document.createElement("button");
    b.className = "update-notice small";
    b.textContent = "↻ New updates: reload";
    b.addEventListener("click", () => location.reload());
    document.body.appendChild(b);
  }
  poll();
  setInterval(poll, 5000);

  // Chart hover: crosshair + tooltip from the data embedded next to each SVG.
  function fmt(t) {
    t = Math.max(0, Math.floor(t));
    const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s).padStart(2, "0");
  }
  document.querySelectorAll(".chart-wrap[data-hover]").forEach(wrap => {
    const d = JSON.parse(wrap.dataset.hover);
    const svg = wrap.querySelector("svg");
    const cross = svg.querySelector(".crosshair");
    const tip = wrap.querySelector(".tooltip");
    const span = d.width - d.left - d.right;
    svg.addEventListener("mousemove", ev => {
      const r = svg.getBoundingClientRect();
      const vx = (ev.clientX - r.left) / r.width * d.width;
      if (vx < d.left || vx > d.width - d.right || !d.points.length) return hide();
      const t = (vx - d.left) / span * d.duration;
      let best = d.points[0];
      for (const p of d.points) if (Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
      const x = d.left + best[0] / d.duration * span;
      cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
      tip.style.display = "block";
      tip.style.left = (x / d.width * r.width) + "px";
      tip.style.top = (ev.clientY - r.top) + "px";
      tip.textContent = fmt(best[0]) + " · " + d.label + " " + best[1];
    });
    function hide() { cross.setAttribute("visibility", "hidden"); tip.style.display = "none"; }
    svg.addEventListener("mouseleave", hide);
    // Click a time on the chart to play it in the stream page's player.
    svg.addEventListener("click", ev => {
      if (!window.cwSeek || ev.target.closest("[data-goto]")) return;
      const r = svg.getBoundingClientRect();
      const vx = (ev.clientX - r.left) / r.width * d.width;
      if (vx < d.left || vx > d.width - d.right) return;
      window.cwSeek((vx - d.left) / span * d.duration);
    });
    if (document.querySelector("[data-review]")) svg.style.cursor = "pointer";
  });

  // Stream page: review moments on the embedded YouTube player.
  const review = document.querySelector("[data-review]");
  if (review) {
    const data = JSON.parse(document.getElementById("moments-data").textContent);
    const byId = new Map(data.map(m => [String(m.id), m]));
    const panel = review.querySelector("[data-review-panel]");
    const $ = sel => panel.querySelector(sel);
    const autoNext = $("[data-auto-next]");
    try { if (localStorage.getItem("cw-auto-next") === "0") autoNext.checked = false; } catch (e) {}
    autoNext.addEventListener("change", () => { try { localStorage.setItem("cw-auto-next", autoNext.checked ? "1" : "0"); } catch (e) {} });
    let current = data.find(m => m.status === "new") || data[0];
    // Next / Prev order: stream time, Claude's rating (best first) or chat score.
    const orderSel = review.querySelector("[data-order]");
    let order = data;
    function setOrder(kind) {
      const by = { claude: m => -(m.worthiness || 0) * 100 - m.score, chat: m => -m.score, time: m => m.start }[kind] || (m => m.start);
      order = [...data].sort((a, b) => by(a) - by(b));
      try { localStorage.setItem("cw-order", kind); } catch (e) {}
    }
    try { const saved = localStorage.getItem("cw-order"); if (saved && orderSel) orderSel.value = saved; } catch (e) {}
    if (orderSel) { setOrder(orderSel.value); orderSel.addEventListener("change", () => setOrder(orderSel.value)); }
    if (orderSel && orderSel.value !== "time") current = order.find(m => m.status === "new") || order[0];
    let player = null, ready = false, pending = null;

    function seek(t, play = true) {
      window.cwHold = true;
      if (!ready) { pending = t; return; }
      player.seekTo(Math.max(0, t), true);
      if (play) player.playVideo();
    }
    window.cwSeek = t => {
      const inside = data.find(m => t >= m.start && t < m.end);
      if (inside) { current = inside; render(); }
      seek(t);
    };
    function select(m, play = true) {
      if (!m) return;
      current = m;
      render();
      seek(m.start, play);
      const top = review.getBoundingClientRect();
      if (top.bottom < 80 || top.top > window.innerHeight) review.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    const statusLabel = { new: "to review", approved: "kept", rejected: "skipped" };
    function render() {
      const m = current;
      $("[data-r-rank]").textContent = "#" + m.rank + (m.kind === "song" ? " ♪" : "");
      $("[data-r-time]").textContent = fmt(m.start) + "–" + fmt(m.end);
      $("[data-r-len]").textContent = Math.round(m.end - m.start) + "s · score " + m.score;
      const st = $("[data-r-status]");
      st.textContent = statusLabel[m.status]; st.className = "pill " + m.status;
      $("[data-r-title]").textContent = m.title || (m.worthiness ? "" : "Not analysed by Claude yet");
      $("[data-r-summary]").textContent = (m.worthiness ? "Claude " + m.worthiness + "/10 · " + m.category + ". " : "") + (m.summary || "");
      $("[data-r-why]").textContent = m.reasons.length ? "Chat reacted: " + m.reasons.join(", ") : "";
      $("[data-r-whytext]").textContent = m.why || "";
      $(".r-why").hidden = !m.why;
      $("[data-r-context]").textContent = m.context || "";
      $(".r-context").hidden = !m.context;
      $("[data-r-chat]").textContent = m.chat.map(([t, n]) => t.slice(0, 24) + " ×" + n).join("   ");
      $("[data-r-open]").href = "/moment/" + m.id;
      review.querySelectorAll("[data-strip] .chip").forEach(c => c.classList.toggle("current", c.dataset.goto === String(m.id)));
      document.querySelectorAll("tr[data-row]").forEach(r => r.classList.toggle("current-row", r.dataset.row === String(m.id)));
      const kept = data.filter(x => x.status === "approved").length;
      const left = data.filter(x => x.status === "new").length;
      $("[data-r-progress]").textContent = `${data.length - left} of ${data.length} reviewed · ${kept} kept`;
      paintPicks();
    }
    function step(dir) {
      const i = order.indexOf(current);
      select(order[(i + dir + order.length) % order.length]);
    }
    function nextToReview() {
      const i = order.indexOf(current);
      for (let k = 1; k <= order.length; k++) {
        const m = order[(i + k) % order.length];
        if (m.status === "new") return m;
      }
      return null;
    }
    // "Keep the N rated 7+ by Claude": one click for Claude's picks you haven't reviewed.
    const pickMin = review.querySelector("[data-pick-min]");
    const pickBtn = review.querySelector("[data-keep-picks]");
    const picks = () => data.filter(m => m.status === "new" && (m.worthiness || 0) >= +pickMin.value);
    function paintPicks() {
      if (!pickBtn) return;
      const n = picks().length;
      pickBtn.querySelector("[data-pick-count]").textContent = n;
      pickBtn.disabled = !n;
    }
    if (pickBtn) {
      pickMin.addEventListener("change", paintPicks);
      pickBtn.addEventListener("click", async () => {
        const list = picks();
        if (!list.length) return;
        pickBtn.disabled = true;
        for (const m of list) await setStatus(m, "approved", { advance: false });
        paintPicks();
      });
    }
    async function setStatus(m, status, opts = {}) {
      const before = m.status;
      m.status = status;            // show it at once; undo if the server says no
      paintStatus(m);
      render();
      try {
        const r = await fetch("/moment/" + m.id + "/status", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status }) });
        if (!r.ok) throw new Error(r.status);
        const d = await r.json();
        document.querySelectorAll("[data-kept-count]").forEach(e => e.textContent = d.kept);
        document.querySelectorAll("[data-kept-clipped]").forEach(e => e.textContent = d.kept_clipped);
        document.querySelectorAll("[data-needs-kept]").forEach(b => b.disabled = !d.kept);
        document.querySelectorAll("[data-needs-kept-clipped]").forEach(b => b.disabled = !d.kept_clipped);
      } catch (e) {
        m.status = before; paintStatus(m); render();
        alert("Couldn't save that. Is cw serve still running?");
        return;
      }
      paintPicks();
      if (opts.advance !== false && status !== "new" && m === current && autoNext.checked) {
        const nxt = nextToReview();
        if (nxt) select(nxt);
      }
    }
    function paintStatus(m) {
      const chip = review.querySelector(`[data-strip] [data-goto="${m.id}"]`);
      if (chip) chip.className = "chip " + m.status + (m === current ? " current" : "");
      const pill = document.querySelector(`[data-status-pill="${m.id}"] .pill`);
      if (pill) { pill.className = "pill " + m.status; pill.textContent = statusLabel[m.status] || m.status; }
      const row = document.querySelector(`tr[data-row="${m.id}"]`);
      if (row) { row.dataset.status = m.status; row.dispatchEvent(new Event("cw-filter", { bubbles: true })); }
    }

    panel.addEventListener("click", ev => {
      const act = ev.target.closest("[data-act]");
      if (act) setStatus(current, act.dataset.act);
      const nav = ev.target.closest("[data-nav]");
      if (nav) nav.dataset.nav === "replay" ? seek(current.start) : step(nav.dataset.nav === "next" ? 1 : -1);
    });
    document.addEventListener("click", ev => {
      const go = ev.target.closest("[data-goto]");
      if (go && !(ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.button === 1)) {
        const m = byId.get(go.dataset.goto);
        if (m) { ev.preventDefault(); select(m); }
      }
    });
    document.addEventListener("keydown", ev => {
      if (ev.ctrlKey || ev.metaKey || ev.altKey || ev.isComposing) return;
      const el = document.activeElement;
      if (el && (["INPUT", "SELECT", "TEXTAREA"].includes(el.tagName) || el.isContentEditable)) return;
      const k = ev.key.toLowerCase();
      const acts = { k: "approved", s: "rejected", u: "new" };
      if (acts[k]) setStatus(current, acts[k]);
      else if (k === "n") step(1);
      else if (k === "p") step(-1);
      else if (k === "r") seek(current.start);
      else return;
      ev.preventDefault();
    });

    // Follow playback: the panel shows whichever moment the video is in.
    setInterval(() => {
      if (!ready || typeof player.getCurrentTime !== "function") return;
      const t = player.getCurrentTime();
      const inside = data.find(m => t >= m.start && t < m.end);
      if (inside && inside !== current) { current = inside; render(); }
    }, 700);

    const fallback = review.querySelector("[data-yt-fallback]");
    function showFallback(text) {
      fallback.hidden = false;
      fallback.textContent = "";
      fallback.append(text + " ");
      const a = document.createElement("a");
      a.href = "https://youtu.be/" + review.dataset.video + "?t=" + Math.floor(current.start);
      a.target = "_blank"; a.rel = "noopener"; a.textContent = "Open on YouTube ↗";
      fallback.append(a);
    }
    window.onYouTubeIframeAPIReady = () => {
      player = new YT.Player("yt-player", {
        videoId: review.dataset.video,
        playerVars: { start: Math.floor(current.start), rel: 0, playsinline: 1 },
        events: {
          onReady: () => { ready = true; if (pending !== null) { seek(pending); pending = null; } },
          onStateChange: e => { if (e.data === YT.PlayerState.PLAYING) window.cwHold = true; },
          onError: e => showFallback(e.data === 101 || e.data === 150 || e.data === 153
            ? "This stream can't be played here (embedding is off, or it's members-only)."
            : "YouTube couldn't play this stream here."),
        },
      });
    };
    const api = document.createElement("script");
    api.src = "https://www.youtube.com/iframe_api";
    api.onerror = () => showFallback("Couldn't load the YouTube player (offline?).");
    document.head.appendChild(api);
    render();
  }

  // [data-tip] marks (storage bars) show their name and value on hover and keyboard focus.
  const tip = document.createElement("div");
  tip.className = "tip-float";
  document.body.appendChild(tip);
  function showTip(el, x, y) {
    tip.textContent = el.dataset.tip;
    tip.style.display = "block";
    const w = tip.offsetWidth;
    tip.style.left = Math.max(8, Math.min(x - w / 2, window.innerWidth - w - 8)) + "px";
    tip.style.top = Math.max(8, y - tip.offsetHeight - 10) + "px";
  }
  document.addEventListener("pointermove", ev => {
    const el = ev.target.closest && ev.target.closest("[data-tip]");
    if (el) showTip(el, ev.clientX, ev.clientY); else tip.style.display = "none";
  });
  document.addEventListener("focusin", ev => {
    const el = ev.target.closest && ev.target.closest("[data-tip]");
    if (el) { const r = el.getBoundingClientRect(); showTip(el, r.left + r.width / 2, r.top); }
  });
  document.addEventListener("focusout", () => { tip.style.display = "none"; });

  // Storage page: pick streams to delete, with a running total and a clear confirmation.
  const delForm = document.querySelector("[data-delete-form]");
  if (delForm) {
    const boxes = [...delForm.querySelectorAll("input[name=key]")];
    const all = delForm.querySelector("[data-select-all]");
    const label = delForm.querySelector("[data-selection]");
    const fmtBytes = n => { const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
      while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
      return (i ? (n < 100 ? n.toFixed(1) : n.toFixed(0)) : n) + " " + u[i]; };
    const picked = () => boxes.filter(b => b.checked);
    function update() {
      const p = picked();
      const size = p.reduce((s, b) => s + +b.dataset.size, 0);
      const clips = p.reduce((s, b) => s + +b.dataset.clips, 0);
      label.textContent = p.length ? `${p.length} selected · ${fmtBytes(size)} (${fmtBytes(clips)} of clip videos)` : "Nothing selected";
      delForm.querySelectorAll("[data-needs-selection]").forEach(b => b.disabled = !p.length);
      if (all) { all.checked = p.length && p.length === boxes.length; all.indeterminate = p.length > 0 && p.length < boxes.length; }
    }
    boxes.forEach(b => b.addEventListener("change", update));
    if (all) all.addEventListener("change", () => { boxes.forEach(b => b.checked = all.checked); update(); });
    delForm.addEventListener("submit", ev => {
      const p = picked();
      const videosOnly = ev.submitter && ev.submitter.value === "videos";
      const size = p.reduce((s, b) => s + +(videosOnly ? b.dataset.clips : b.dataset.size), 0);
      const notOnDrive = p.filter(b => b.dataset.onDrive !== "1").length;
      let text = videosOnly
        ? `Delete the clip videos of ${p.length} stream(s) (${fmtBytes(size)})? Summaries and frames stay; you can download the clips again later.`
        : `Delete ${p.length} stream(s) (${fmtBytes(size)}) from this computer, including their summaries and edits? This can't be undone.`;
      if (delForm.dataset.drive === "1") {
        if (notOnDrive) text += `\n\n${notOnDrive} of them ${notOnDrive === 1 ? "isn't" : "aren't"} on Google Drive.`;
        text += "\n\nGoogle Drive copies are not touched.";
      }
      if (!confirm(text)) ev.preventDefault();
    });
    update();
  }

  // Storage page: ask Google Drive in the background, then show the answer.
  if (document.querySelector("[data-drive-refresh]")) {
    fetch("/storage/drive", { method: "POST" }).finally(() => location.reload());
  }

  // Clip page, a compilation's pieces ([data-pieces]): add an empty row, take a row out.
  document.addEventListener("click", ev => {
    const btn = ev.target.closest("[data-piece-add], [data-piece-remove]");
    if (!btn) return;
    const rows = btn.closest("[data-pieces]").querySelectorAll("[data-piece]");
    if (btn.matches("[data-piece-remove]")) {
      if (rows.length > 1) btn.closest("[data-piece]").remove();
      return;
    }
    const row = rows[rows.length - 1].cloneNode(true);
    row.querySelectorAll("input").forEach(i => { i.value = i.name === "role" ? "beat" : ""; });
    row.querySelectorAll("a, .tag").forEach(el => el.remove());
    rows[rows.length - 1].after(row);
    row.querySelector("input").focus();
  });

  // [data-seek="12.5"] jumps the clip player to that time.
  document.addEventListener("click", ev => {
    // [data-stream-seek]: a stream time. Plays in the stream page's player; without one the link opens YouTube.
    const st = ev.target.closest("[data-stream-seek]");
    if (st && window.cwSeek && !ev.ctrlKey && !ev.metaKey) {
      ev.preventDefault();
      window.cwSeek(parseFloat(st.dataset.streamSeek));
      const frame = document.querySelector(".yt-frame");
      if (frame) frame.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
    const el = ev.target.closest("[data-seek]");
    if (el) {
      const v = document.querySelector("video");
      if (v) { v.currentTime = parseFloat(el.dataset.seek); v.play(); }
    }
    const c = ev.target.closest("[data-copy]");
    if (c) {
      navigator.clipboard.writeText(c.dataset.copy).then(() => {
        const old = c.textContent; c.textContent = "copied ✓"; setTimeout(() => c.textContent = old, 1200);
      });
    }
  });
  // Every colour picker gets a hex box beside it ("#FF8FC8", or short "#F8C"). Typing a
  // colour sets the picker and fires its input and change events, so a page reacts as if
  // it was picked; picking fills the box. Pickers added later (the editor) get one too.
  function hexFor(picker) {
    if (picker.dataset.hexed) return;
    picker.dataset.hexed = "1";
    const box = document.createElement("input");
    Object.assign(box, { type: "text", className: "hex", maxLength: 7, spellcheck: false,
                         value: picker.value.toUpperCase(), title: "Hex colour, e.g. #FF8FC8 or #F8C" });
    box.setAttribute("aria-label", (picker.getAttribute("aria-label") || picker.title || "Colour") + " (hex)");
    picker.after(box);
    picker.addEventListener("input", () => { box.value = picker.value.toUpperCase(); box.classList.remove("bad"); });
    const apply = () => {
      let c = box.value.trim().replace(/^#/, "");
      if (/^[0-9a-f]{3}$/i.test(c)) c = c.replace(/./g, ch => ch + ch);
      if (!/^[0-9a-f]{6}$/i.test(c)) { box.classList.add("bad"); return false; }
      box.classList.remove("bad");
      box.value = "#" + c.toUpperCase();
      if (picker.value.toLowerCase() !== "#" + c.toLowerCase()) {
        picker.value = "#" + c.toLowerCase();
        picker.dispatchEvent(new Event("input", { bubbles: true }));
        picker.dispatchEvent(new Event("change", { bubbles: true }));
      }
      return true;
    };
    box.addEventListener("change", apply);
    box.addEventListener("keydown", ev => { if (ev.key === "Enter" && !apply()) ev.preventDefault(); });
  }
  const hexAll = el => el.querySelectorAll('input[type="color"]').forEach(hexFor);
  hexAll(document);
  new MutationObserver(changes => {
    for (const m of changes) for (const n of m.addedNodes) {
      if (n.nodeType !== 1) continue;
      if (n.matches('input[type="color"]')) hexFor(n); else hexAll(n);
    }
  }).observe(document.body, { childList: true, subtree: true });

  // ── list tools ───────────────────────────────────────────────────────────
  // Theme: system → light → dark, remembered on this browser (base.html applies it before paint).
  const themeBtn = document.querySelector("[data-theme-toggle]");
  if (themeBtn) {
    const label = themeBtn.querySelector("[data-theme-label]");
    const order = ["", "light", "dark"], names = { "": "Theme: system", light: "Theme: light", dark: "Theme: dark" };
    const now = () => document.documentElement.dataset.theme || "";
    const paint = () => { if (label) label.textContent = names[now()]; };
    themeBtn.addEventListener("click", () => {
      const next = order[(order.indexOf(now()) + 1) % order.length];
      if (next) document.documentElement.dataset.theme = next; else delete document.documentElement.dataset.theme;
      try { next ? localStorage.setItem("cw-theme", next) : localStorage.removeItem("cw-theme"); } catch (e) {}
      paint();
      window.dispatchEvent(new Event("cw-theme"));
    });
    paint();
  }

  // Filter forms ([data-autosubmit]): a changed select submits at once; empty fields stay out of the URL.
  document.querySelectorAll("form[data-autosubmit]").forEach(form => {
    form.querySelectorAll("[data-autosubmit-hide]").forEach(b => b.hidden = true);
    form.addEventListener("change", ev => { if (ev.target.matches("select")) form.requestSubmit(); });
    form.addEventListener("search", () => form.requestSubmit());          // the search box's × button
    form.addEventListener("submit", () => {
      form.querySelectorAll("input, select").forEach(el => { if (!el.value) el.disabled = true; });
    });
  });
  window.addEventListener("pageshow", () => document.querySelectorAll("form[data-autosubmit] :disabled")
    .forEach(el => { if (!el.matches("button")) el.disabled = false; }));

  // In-page filter: <div data-filter="#list"> holds a search box [data-filter-q], button groups
  // [data-filter-attr="status"] (each button's data-value; data-attr on a button names another attribute)
  // and selects [data-filter-min="rating"]. It hides the list's [data-item] children that don't match.
  document.querySelectorAll("[data-filter]").forEach(bar => {
    const list = document.querySelector(bar.dataset.filter);
    if (!list) return;
    const q = bar.querySelector("[data-filter-q]");
    const count = bar.querySelector("[data-filter-count]");
    const empty = list.closest(".card, section, form")?.querySelector("[data-filter-empty]");
    function apply() {
      const text = (q ? q.value : "").trim().toLowerCase();
      const want = [];
      bar.querySelectorAll("[data-filter-attr]").forEach(g => {
        const on = g.querySelector(".on");
        if (on && on.dataset.value) want.push([on.dataset.attr || g.dataset.filterAttr, on.dataset.value]);
      });
      const mins = [...bar.querySelectorAll("[data-filter-min]")].filter(s => s.value).map(s => [s.dataset.filterMin, +s.value]);
      let shown = 0, all = 0;
      list.querySelectorAll("[data-item]").forEach(el => {
        all++;
        const ok = (!text || (el.dataset.search || el.textContent).toLowerCase().includes(text))
          && want.every(([k, v]) => el.dataset[k] === v) && mins.every(([k, v]) => +el.dataset[k] >= v);
        el.hidden = !ok;
        if (ok) shown++;
      });
      if (count) count.textContent = shown === all ? all : `${shown} of ${all}`;
      if (empty) empty.hidden = shown > 0 || !all;
    }
    if (q) q.addEventListener("input", apply);
    bar.querySelectorAll("[data-filter-attr] button").forEach(b => b.addEventListener("click", () => {
      b.parentElement.querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
      apply();
    }));
    bar.querySelectorAll("[data-filter-min]").forEach(s => s.addEventListener("change", apply));
    list.addEventListener("cw-filter", apply);
  });

  // Sortable tables: <table data-sortable>, <th data-sort> (text) or <th data-sort="num">; a cell's
  // data-v is what it sorts by (else its text). Click again to reverse.
  document.querySelectorAll("table[data-sortable]").forEach(table => {
    const heads = [...table.querySelectorAll("thead th")];
    heads.forEach((th, col) => {
      if (!("sort" in th.dataset)) return;
      th.addEventListener("click", () => {
        const num = th.dataset.sort === "num";
        const dir = th.classList.contains(num ? "desc" : "asc") ? (num ? 1 : -1) : (num ? -1 : 1);   // numbers: biggest first
        heads.forEach(h => h.classList.remove("asc", "desc"));
        th.classList.add(dir > 0 ? "asc" : "desc");
        const key = tr => { const c = tr.children[col]; const v = c ? (c.dataset.v ?? c.textContent.trim()) : ""; return num ? (parseFloat(v) || 0) : v.toLowerCase(); };
        const body = table.tBodies[0];
        [...body.rows].sort((a, b) => { const x = key(a), y = key(b); return (x < y ? -1 : x > y ? 1 : 0) * dir; })
          .forEach(tr => body.appendChild(tr));
      });
    });
  });

  // Tabs inside a page: [data-tabs] holds buttons [data-tab="x"] and panels [data-tab-panel="x"].
  // The address's #hash opens the tab that holds that element (e.g. /sounds#overlays, #o-<id>).
  document.querySelectorAll("[data-tabs]").forEach(box => {
    const show = name => {
      box.querySelectorAll("[data-tab]").forEach(b => b.classList.toggle("on", b.dataset.tab === name));
      box.querySelectorAll("[data-tab-panel]").forEach(p => p.hidden = p.dataset.tabPanel !== name);
    };
    box.querySelectorAll("[data-tab]").forEach(b => b.addEventListener("click", () => {
      show(b.dataset.tab);
      history.replaceState(null, "", "#" + b.dataset.tab);
    }));
    const fromHash = () => {
      const id = decodeURIComponent(location.hash.slice(1));
      if (!id) return;
      const panel = box.querySelector(`[data-tab-panel="${CSS.escape(id)}"]`) || document.getElementById(id)?.closest("[data-tab-panel]");
      if (panel && box.contains(panel)) { show(panel.dataset.tabPanel); document.getElementById(id)?.scrollIntoView({ block: "nearest" }); }
    };
    fromHash();
    window.addEventListener("hashchange", fromHash);
  });

  // Menus (details[data-menu]) close when you click outside them or press Escape.
  document.addEventListener("click", ev => {
    document.querySelectorAll("details[data-menu][open]").forEach(d => { if (!d.contains(ev.target)) d.open = false; });
  });
  document.addEventListener("keydown", ev => {
    if (ev.key === "Escape") document.querySelectorAll("details[data-menu][open]").forEach(d => d.open = false);
  });
})();
