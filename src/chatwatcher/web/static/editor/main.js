// Moment page editor: Claude's cut on a waveform timeline. Drag or nudge the edges,
// split, reorder and set volume per segment, place sound effects and punch-ins,
// push-ins, shakes and flashes (or let Claude place them), preview it all in the
// browser, edit the subtitles Whisper's words give, then render edit.mp4 on the server.
// The Short tab is a second edit of the same clip in 9:16 (its own cut, effects and
// subtitles, plus a layout: crop / stack / fit), previewed on a canvas and rendered to short.mp4.
// Everything here is clip time (seconds into clip.mp4).
//
// This is the entry: it loads the modules, gives the lower ones the few functions they call
// upwards (call.*), and runs the frame loop. Modules import only from ones listed before
// them here: state, util, preview, audio, playback, captions, io, editing, overlays,
// timeline, transcript, panels, regions, layout, ai, input.
import { $, S, call, video } from "./state.js";
import { curFx, curSub, isVis, libraryStatus, partAt, setSaved, timeParts } from "./util.js";
import { drawStage } from "./preview.js";
import { fire, fireOverlay, mixVolume, syncMusic } from "./audio.js";
import { paintButtons, seek, stopEditPlay } from "./playback.js";
import { drawSubPreview } from "./captions.js";
import { pollRender, showRender } from "./io.js";
import { paintNames } from "./editing.js";
import { drawCards, drawLayers, drawOverlays, drawSpotHandle, drawTexts } from "./overlays.js";
import { draw, follow, paintTime, resize } from "./timeline.js";
import { paintLine, paintList, paintTranscript, pollTranscript } from "./transcript.js";
import {
  followSelection, paintChips, paintFxPanel, paintLanes, paintMusicPanel, paintSelected, paintSubBar, paintSubPanel,
  paintTotal, paintVisPanel, paintVoices,
} from "./panels.js";
import { paintRegionPanel, paintRegions } from "./regions.js";
import { apply, fetchEdit, paintLayout } from "./layout.js";
import { loadChat, paintFormats } from "./ai.js";
import "./input.js";

// ── the frame loop ─────────────────────────────────────────────────────────────
let prevT = null;   // sounds fire when playback crosses them

function tick() {
  if (S.D) {
    const t = video.currentTime;
    if (S.editPlay && !video.paused && !video.seeking) {
      let s = S.segs[S.editPlay.i];
      if (!s || t < s[0] - 0.25 || t > s[1] + 0.25) {      // you jumped elsewhere in the clip
        const j = S.segs.findIndex(q => t >= q[0] && t < q[1]);
        if (j < 0) stopEditPlay(); else { S.editPlay.i = j; s = S.segs[j]; }
      }
      if (S.editPlay && t >= s[1] - 0.02) {
        if (S.editPlay.only || S.editPlay.i + 1 >= S.segs.length) {
          video.pause(); stopEditPlay(); seek(s[1]);
        } else {
          S.editPlay.i++;
          S.armFrom = S.segs[S.editPlay.i][0] - 0.0005;
          seek(S.segs[S.editPlay.i][0]);
        }
      }
    }
    // Time effects (edit/timing.py): the part playing sets the speed; a freeze holds the
    // frame (the video waits, the edit's clock and the music go on), then plays on.
    if (S.editPlay && !video.paused && !video.seeking) {
      const fz = prevT === null ? null : timeParts().find(q => q.freeze && q.i === S.editPlay.i && q.s >= prevT - 0.001 && q.s <= t
                                                             && t - prevT < 0.5);
      if (fz) { S.frozen = { s: fz.s, e: fz.e, f: fz.f, since: performance.now() }; video.pause(); seek(fz.s); }
      else {
        const p = partAt(t, S.editPlay.i), k = p ? Math.min(16, Math.max(0.0625, p.k)) : 1;
        if (Math.abs(video.playbackRate - k) > 1e-3) video.playbackRate = k;
        const pitch = !(p && p.f && p.f.sound === "deep");
        if (video.preservesPitch !== pitch) video.preservesPitch = pitch;
      }
    } else if (!S.editPlay && !S.frozen && video.playbackRate !== 1) { video.playbackRate = 1; video.preservesPitch = true; }
    if (S.frozen) {
      if (!S.editPlay) S.frozen = null;
      else if ((performance.now() - S.frozen.since) / 1000 >= S.frozen.f.d) {
        const e = S.frozen.e;
        S.frozen = null;
        S.armFrom = e - 0.0005;
        seek(e);
        video.play();
      }
      S.needsDraw = true;
    }
    if (video.seeking) prevT = S.armFrom;
    else {
      if (!video.paused && prevT !== null && t > prevT && t - prevT < 0.5)
        for (const f of S.fx) if (f.t > prevT && f.t <= t) { if (f.type === "sfx") fire(f); else if (f.type === "overlay") fireOverlay(f); }
      prevT = t;
      S.armFrom = null;
    }
    if (S.previewUntil !== null && !video.paused && t >= S.previewUntil) { S.previewUntil = null; video.pause(); }
    mixVolume();
    syncMusic(t);
    if (!video.paused || video.seeking || S.needsDraw || S.drag) {
      if (!video.paused && !S.drag) follow(t);
      drawStage();
      drawSpotHandle();
      drawLayers();
      drawOverlays();
      drawSubPreview();
      drawTexts();
      drawCards();
      draw();
      paintTime(t);
      paintLine(t);
      S.needsDraw = false;
    }
  }
  requestAnimationFrame(tick);
}

// ── every panel, after any change ───────────────────────────────────────────────────────────────
function paintAll() {
  paintFormats();
  S.needsDraw = true;
  paintTotal();
  paintChips();
  if (S.regionMode) paintRegionPanel();
  else if (S.selSub && curSub()) paintSubPanel();
  else if (S.selFx && curFx()) (isVis(curFx()) ? paintVisPanel() : curFx().type === "music" ? paintMusicPanel() : paintFxPanel());
  else { S.selFx = null; S.selSub = null; paintSelected(); }
  paintButtons();
  paintList();
  paintSubBar();
  paintVoices();
  paintLayout();
  followSelection();
}

Object.assign(call, { loadChat, paintAll, paintList, paintRegions });

// ── start ────────────────────────────────────────────────────────────────
(async function load() {
  let d = await fetchEdit(S.kind);
  if (!d.ok && S.kind === "short") { S.kind = "long"; d = await fetchEdit("long"); }
  if (!d.ok) { $("[data-ed-loading]").textContent = d.error || "Couldn't load the editor."; return; }
  apply(d);
  $("[data-ed-loading]").remove();
  paintLanes();
  loadChat();
  paintTranscript();
  paintNames();
  if (d.transcript.state === "queued" || d.transcript.state === "running") pollTranscript();
  resize();
  paintAll();
  showRender(d.render);
  if (d.render.state === "queued" || d.render.state === "running") pollRender();
  setSaved(d.saved ? "Saved" : (S.source === "long" ? "Claude's cut, snapped to pauses" : ""));
  libraryStatus();
  const start = () => { if (video.currentTime === 0 && S.segs[0]) seek(S.segs[0][0]); };
  if (video.readyState >= 1) start(); else video.addEventListener("loadedmetadata", start, { once: true });
  video.addEventListener("loadeddata", () => { S.needsDraw = true; });
  window.addEventListener("resize", () => { S.needsDraw = true; S.subKey = ""; });
  requestAnimationFrame(tick);
})();
