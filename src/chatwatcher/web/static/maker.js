// Moment page: the brand intro (teasers picked from the edit → glitch → logo). It uses the
// editor's player for the playhead. Times are clip time. (The thumbnail maker is thumb.js.)
(function () {
  const video = document.querySelector("[data-editor] video");
  const GAINS = [6, 3, 0, -3, -6, -10, -15, -20];

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function btn(text, title, onclick, cls = "small") {
    const b = el("button", cls, text);
    b.type = "button";
    if (title) b.title = title;
    b.addEventListener("click", onclick);
    return b;
  }
  function fmt(t, d = 1) {
    t = Math.max(0, t);
    const m = Math.floor(t / 60), s = (t - m * 60).toFixed(d);
    return m + ":" + (s < 10 ? "0" : "") + s;
  }
  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    let d = {};
    try { d = await r.json(); } catch (e) {}
    if (!r.ok || d.ok === false) throw new Error(d.error || ("HTTP " + r.status));
    return d;
  }
  const hold = () => { window.cwHold = true; };   // don't auto-reload the page while you work here
  const r3 = v => Math.round(v * 1000) / 1000;

  // ── playing ranges in the editor's player ────────────────────────────────
  let playing = null;
  function playRanges(ranges) {
    if (!video || !ranges.length) return;
    stopRanges();
    playing = { ranges, i: 0 };
    video.currentTime = ranges[0][0];
    video.play();
    const step = () => {
      if (!playing) return;
      const [, end] = playing.ranges[playing.i];
      if (video.paused && video.currentTime < end - 0.05) { playing = null; return; }   // you paused it
      if (video.currentTime >= end) {
        playing.i += 1;
        if (playing.i >= playing.ranges.length) { video.pause(); playing = null; return; }
        video.currentTime = playing.ranges[playing.i][0];
      }
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  function stopRanges() { playing = null; }

  // ── intro ────────────────────────────────────────────────────────────────
  const ib = document.querySelector("[data-intro]");
  if (ib) (function () {
    const pk = ib.dataset.intro, $ = s => ib.querySelector(s);
    let D = null, tz = [], mine = false, saveTimer = null, poll = null, watch = null;

    const lineAt = (a, b) => (D.cues || []).filter(c => c[0] < b && a < c[1]).map(c => c[2]).join(" ");
    function total() { return tz.reduce((n, t) => n + t[1] - t[0], 0); }
    function changed() {
      mine = true;
      paint();
      clearTimeout(saveTimer);
      saveTimer = setTimeout(save, 400);
      hold();
    }
    async function save(extra = {}) {
      clearTimeout(saveTimer);
      try {
        const d = await post(`/moment/${pk}/intro`, { teasers: mine ? tz : "auto", ...extra });
        D = { ...D, ...d };
        $("[data-in-saved]").textContent = "Saved";
        showState(d.state);
        window.dispatchEvent(new Event("cw-render-changed"));
      } catch (e) {
        $("[data-in-saved]").textContent = "Couldn't save: " + e.message;
      }
    }
    function paint() {
      const list = $("[data-in-list]");
      list.textContent = "";
      if (!tz.length) list.append(el("p", "faint", "No teasers: the intro is just your logo."));
      tz.forEach((t, i) => {
        const row = el("div", "maker-item" + (t[3] === "blast" ? " blast" : ""));
        const play = btn(`▶ ${fmt(t[0])}–${fmt(t[1])}`, "Play this teaser", () => playRanges([[t[0], t[1]]]), "small mono");
        const len = el("span", "muted", (t[1] - t[0]).toFixed(1) + " s");
        const text = el("span", "maker-line", lineAt(t[0], t[1]) || "—");
        const at = () => (video ? r3(video.currentTime) : 0);
        const startHere = btn("⇤", "Start it at the playhead", () => {
          const v = at();
          if (v < t[1] - 0.3) { t[0] = v; changed(); }
        });
        const endHere = btn("⇥", "End it at the playhead", () => {
          const v = at();
          if (v > t[0] + 0.3) { t[1] = v; changed(); }
        });
        const style = el("select");
        style.setAttribute("aria-label", "Teaser style");
        for (const [v, label] of [["normal", "normal"], ["blast", "💥 blast"]]) {
          const o = el("option", null, label);
          o.value = v;
          o.selected = t[3] === v;
          style.appendChild(o);
        }
        style.title = "blast: shake, colour split and a red tint, for a scream or a loud fail";
        style.addEventListener("change", () => { t[3] = style.value; if (t[3] === "blast" && t[2] === 0) t[2] = -6; changed(); });
        const gain = el("select");
        gain.setAttribute("aria-label", "Teaser volume");
        for (const g of GAINS) {
          const o = el("option", null, (g > 0 ? "+" : "") + g + " dB");
          o.value = g;
          o.selected = Math.abs(g - t[2]) < 0.5;
          gain.appendChild(o);
        }
        gain.addEventListener("change", () => { t[2] = +gain.value; changed(); });
        const up = btn("↑", "Earlier in the intro", () => { if (i > 0) { [tz[i - 1], tz[i]] = [tz[i], tz[i - 1]]; changed(); } });
        const down = btn("↓", "Later in the intro", () => { if (i < tz.length - 1) { [tz[i + 1], tz[i]] = [tz[i], tz[i + 1]]; changed(); } });
        const del = btn("✕", "Remove this teaser", () => { tz.splice(i, 1); changed(); });
        row.append(play, len, text, startHere, endHere, style, gain, up, down, del);
        list.appendChild(row);
      });
      const n = total();
      $("[data-in-total]").textContent = `${tz.length} teaser(s), ${n.toFixed(1)} s + logo ${D.logo_s} s = ${(n + D.logo_s + (tz.length ? 0.27 : 0)).toFixed(1)} s`
        + (mine ? "" : " · picked from your edit");
      $('[data-in-act="auto"]').disabled = !mine;
    }
    function add() {
      if (tz.length >= D.max) return;
      const t = video ? video.currentTime : 0;
      const cue = (D.cues || []).find(c => c[0] <= t && t < c[1]);
      let a = cue ? cue[0] : t, b = cue ? Math.min(cue[1], cue[0] + 3) : t + 2;
      b = Math.min(b, D.duration);
      if (b - a < 0.3) return;
      tz.push([r3(a), r3(b), 0, "normal"]);
      tz.sort((x, y) => x[0] - y[0]);
      changed();
    }
    async function render() {
      const b = $('[data-in-act="render"]');
      b.disabled = true;
      try {
        const d = await post(`/moment/${pk}/intro/render`, { teasers: mine ? tz : "auto" });
        showState(d.state);
        pollState();
      } catch (e) {
        $("[data-in-saved]").textContent = e.message;
      } finally {
        b.disabled = false;
      }
    }
    function pollState() {
      clearTimeout(poll);
      poll = setTimeout(async () => {
        try {
          const d = await (await fetch(`/api/moment/${pk}/intro/state`, { cache: "no-store" })).json();
          showState(d.state);
          if (d.state.state === "queued" || d.state.state === "running") pollState();
          else window.dispatchEvent(new Event("cw-render-changed"));
        } catch (e) { pollState(); }
      }, 1200);
    }
    function showState(r) {
      const box = $("[data-in-state]");
      box.textContent = "";
      if (r.state === "queued") {
        const why = r.worker === "stopped" ? "The worker isn't running: start it with cw serve or cw worker."
          : r.behind ? `It starts when the worker finishes its current ${r.behind} job.` : "Starting…";
        box.append(el("p", "ed-status busy", "Queued. " + why));
      } else if (r.state === "running") {
        const pct = Math.round((r.progress || 0) * 100);
        const bar = el("div", "ed-progress"), fill = el("span");
        fill.style.width = pct + "%";
        bar.appendChild(fill);
        box.append(el("p", "ed-status busy", `Rendering the intro… ${pct}%`), bar);
      } else if (r.state === "error") {
        box.append(el("p", "ed-status err", "Intro failed: " + (r.error || "unknown error")));
      } else if (r.state === "done" || r.state === "stale") {
        box.append(el("p", "ed-status " + (r.state === "done" ? "ok" : ""), r.state === "done"
          ? `✓ intro.mp4: ${(r.length || 0).toFixed(1)} s · ${((r.size || 0) / 1048576).toFixed(1)} MB`
          : "The teasers or the brand changed since. Render again to update intro.mp4."));
        const acts = el("div", "actions");
        acts.append(btn(watch ? "Hide" : "▶ Watch", "Watch the intro", () => {
          if (watch) { watch.pause(); watch.remove(); watch = null; showState(r); return; }
          watch = el("video", "ed-watch");
          watch.controls = true;
          watch.src = r.url;
          if (video) video.pause();
          showState(r);
          watch.play();
        }));
        const dl = el("a", "btn small", "⬇ Download");
        dl.href = r.url.replace("?", "?dl=1&");
        acts.append(dl);
        box.append(acts);
        if (watch) { if (watch.src.indexOf(r.url) < 0) watch.src = r.url; box.append(watch); }
      }
    }
    ib.addEventListener("click", ev => {
      const a = ev.target.closest("[data-in-act]");
      if (!a) return;
      const act = a.dataset.inAct;
      if (act === "add") add();
      else if (act === "auto") { mine = false; tz = D.auto.map(t => t.slice()); paint(); save(); }
      else if (act === "play") playRanges(tz.map(t => [t[0], t[1]]));
      else if (act === "render") render();
    });
    $("[data-in-on]").addEventListener("change", ev => { hold(); save({ on_edit: ev.target.checked }); });

    (async function load() {
      let d;
      try { d = await (await fetch(`/api/moment/${pk}/intro`, { cache: "no-store" })).json(); }
      catch (e) { d = { ok: false, error: "Couldn't reach the dashboard." }; }
      if (!d.ok) { $("[data-in-list]").textContent = d.error || "Couldn't load the intro."; return; }
      D = d;
      tz = d.teasers.map(t => t.slice());
      mine = d.source === "saved";
      $("[data-in-on]").checked = d.on_edit;
      $("[data-in-brand]").textContent = d.brand.name + (d.brand.logo ? "" : " (no logo yet)");
      paint();
      showState(d.state);
      if (d.state.state === "queued" || d.state.state === "running") pollState();
    })();
  })();
})();
