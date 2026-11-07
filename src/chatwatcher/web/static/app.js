// chat-watcher dashboard: auto-refresh, chart hover, seek-to-time, copy buttons.
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
        if (!busy && !playing) location.reload();
      }
      version = d.v;
    } catch (e) { /* server restarting */ }
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
  });

  // [data-seek="12.5"] jumps the clip player to that time.
  document.addEventListener("click", ev => {
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
})();
