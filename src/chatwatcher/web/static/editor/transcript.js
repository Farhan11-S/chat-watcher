// The list beside the player (your subtitle lines, or the transcript) and the Transcript
// tab: Whisper on this clip (word timings, for subtitles) or YouTube's auto-captions.
import { $, LANG, S, SUB_MIN, call, pk, ticked } from "./state.js";
import { btn, cloneFx, el, flash, fmt, hold, listFlash, post, r3, setSpeaker } from "./util.js";
import { seek } from "./playback.js";
import { V, voiceName, voiceText } from "./captions.js";
import { pushUndo, rebuildSubs, setSubText, subIndex, subsChanged } from "./editing.js";

let lastLine = -1;
let txPoll = null;
let lastTick = null;   // the last one ticked: Shift+click ticks everything between
// The list beside the player: your subtitle lines (click one to retype it) once there
// are any, otherwise the transcript. ⇤ / ⇥ start or end the selected segment at a line.
const listMode = () => (S.subs.length ? "subs" : "lines");
// Above the lines: with lines ticked, one button per person gives them all to her.
function bulkBar() {
  const bar = el("div", "ed-list-head ed-bulk");
  const n = S.subs.filter(c => ticked.has(c.id)).length;
  if (!n) {
    bar.append(el("small", "muted", V().count
      ? "Tick lines (Shift+click for a range) to give them a person in one go."
      : "Add the people in this clip under Captions, then tick lines to colour them."),
      btn("Select all", "Tick every line", { bulkAll: "" }));
    return bar;
  }
  bar.appendChild(el("b", "", `${n} selected:`));
  for (let k = 0; k < V().count; k++) {
    const b = btn(`${k + 1} · ${voiceName(k)}`, `Give the ${n} ticked line${n === 1 ? "" : "s"} to ${voiceName(k)} (or press ${k + 1})`,
                  { bulkVoice: k });
    b.style.borderColor = b.style.color = "#" + voiceText(k);
    bar.appendChild(b);
  }
  bar.append(btn("nobody", "Back to white (or press 0)", { bulkVoice: "" }),
             btn("Select all", "Tick every line", { bulkAll: "" }), btn("Clear", "Untick them all (Esc)", { bulkClear: "" }));
  return bar;
}
function pickLine(id, on, range) {
  const order = S.subs.slice().sort((a, b) => a.t0 - b.t0).map(c => c.id);
  const i = order.indexOf(id), j = range && lastTick ? order.indexOf(lastTick) : -1;
  for (const x of j >= 0 ? order.slice(Math.min(i, j), Math.max(i, j) + 1) : [id]) on ? ticked.add(x) : ticked.delete(x);
  lastTick = id;
  S.listFor = null;
  paintList();
}
export function giveTicked(k) {
  const lines = S.subs.filter(c => ticked.has(c.id));
  if (!lines.length) return false;
  if (k !== null && k >= V().count) { flash(`Add person ${k + 1} under Captions first.`); return true; }
  pushUndo();
  for (const c of lines) setSpeaker(c, k);
  ticked.clear();
  subsChanged();
  flash(`${lines.length} line${lines.length === 1 ? "" : "s"} → ${k === null ? "nobody" : voiceName(k)}. Ctrl+Z undoes.`);
  S.listFor = null;
  call.paintAll();
  return true;
}
export function paintList() {
  const box = $("[data-ed-lines]");
  if (box.contains(document.activeElement)) return;      // not under your cursor; again after the edit
  const mode = listMode();
  const key = mode === "subs" ? JSON.stringify([S.subs.map(c => [c.id, c.t0, c.t1, c.text, c.by, c.id === S.selSub, c.speaker ?? "",
                                                               ticked.has(c.id)]), V().count, V().map]) : "lines:" + S.D.lines.length;
  if (key === S.listFor) return;
  S.listFor = key;
  box.textContent = "";
  lastLine = -1;
  if (mode === "lines") {
    const head = el("div", "ed-list-head");
    if (S.D.lines.length) {
      head.append(el("small", "muted", "The transcript, as it came. To change times or add lines, edit them as subtitle lines:"),
        btn("✎ Edit as lines", "Turn these into subtitle lines you can retime, retype, add to and delete", { edAct: "lines-edit" }));
    } else {
      head.append(el("small", "muted", "No transcript for this clip. Add lines yourself:"),
        btn("+ Line at playhead", "A new subtitle line at the playhead", { edAct: "sub-add" }));
    }
    box.appendChild(head);
    S.D.lines.forEach((l, k) => {
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
  box.appendChild(bulkBar());
  S.subs.forEach((c, k) => {
    const row = el("div", "ed-line ed-sub-row" + (c.id === S.selSub ? " sel" : "") + (c.by === "you" ? " mine" : "")
                   + (ticked.has(c.id) ? " picked" : ""));
    row.dataset.k = k;
    row.dataset.sub = c.id;
    if (c.speaker != null) row.style.borderLeft = `4px solid #${voiceText(c.speaker)}`;
    const tick = el("input", "ed-pick");
    Object.assign(tick, { type: "checkbox", checked: ticked.has(c.id),
                          title: "Tick to give many lines a person at once (Shift+click: every line in between)" });
    tick.dataset.subPick = c.id;
    tick.setAttribute("aria-label", "Select: " + c.text);
    row.appendChild(tick);
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
  const c = S.subs.find(x => x.id === id);
  if (!c) return "That line is gone.";
  if (!isFinite(t)) return "Type a time like 1:02.5 or 62.5.";
  let t0 = c.t0, t1 = c.t1;
  if (edge === "t0") { t0 = t; if (t0 > t1 - SUB_MIN) t1 = t0 + (c.t1 - c.t0); }
  else { t1 = t; if (t1 < t0 + SUB_MIN) return `The end must be after the start (${fmt(t0, 2)}).`; }
  t0 = Math.min(Math.max(t0, 0), S.D.duration); t1 = Math.min(Math.max(t1, 0), S.D.duration);
  const want = [t0, t1];
  for (const o of S.subs) {
    if (o === c || !(o.t0 < t1 && o.t1 > t0)) continue;
    if (o.t0 <= t0) t0 = o.t1; else t1 = o.t0;          // keep the part before / after it
    if (t1 - t0 < SUB_MIN)
      return `That overlaps the line at ${fmt(o.t0, 1)} ("${o.text.slice(0, 30)}"): move or shorten it first.`;
  }
  pushUndo();
  c.t0 = r3(t0); c.t1 = r3(t1); c.by = "you";
  S.subs.sort((a, b) => a.t0 - b.t0);
  S.selSub = c.id;
  seek(edge === "t0" ? c.t0 : Math.max(c.t0, c.t1 - 0.05));
  subsChanged();
  if (Math.abs(want[0] - t0) > 0.005 || Math.abs(want[1] - t1) > 0.005)
    listFlash(`Trimmed to ${fmt(t0, 2)}–${fmt(t1, 2)} so it doesn't overlap the lines around it.`);
  return "";
}
export function addSubAfter(id) {
  const k = subIndex(id), c = S.subs[k];
  if (!c) return;
  const next = S.subs[k + 1];
  let t0 = c.t1 + 0.04, t1 = Math.min(t0 + 2, next ? next.t0 - 0.04 : S.D.duration);
  let cut = null;                    // no gap after it: the new line takes this line's second half
  if (t1 - t0 < 0.3) {
    if (c.t1 - c.t0 < 2 * SUB_MIN + 0.3) { listFlash("This line is too short to make room after it: type an earlier end on it or the next one."); return; }
    cut = Math.max(c.t0 + (c.t1 - c.t0) / 2, c.t1 - 2);
    t0 = cut + 0.04; t1 = c.t1;
  }
  pushUndo();
  if (cut !== null) { c.t1 = r3(cut); c.by = "you"; }
  const n = { id: "u" + Math.random().toString(36).slice(2, 8), t0: r3(t0), t1: r3(t1), text: "…", by: "you" };
  S.subs.splice(k + 1, 0, n);
  S.selSub = n.id;
  seek(n.t0);
  subsChanged();
  paintList();
  const span = $(`[data-sub-text="${n.id}"]`);
  if (span) { span.focus(); document.getSelection().selectAllChildren(span); }
}
// The transcript's lines as subtitle lines (Whisper's words give better ones when there are any).
export function linesToSubs() {
  if (S.D.subs_auto.length) { rebuildSubs(); return; }
  pushUndo();
  S.subs = S.D.lines.map((l, k) => {
    const next = S.D.lines[k + 1], t0 = Math.min(l.t, S.D.duration - SUB_MIN);
    const t1 = Math.min(next ? next.t - 0.04 : S.D.duration, t0 + 6, S.D.duration);
    return { id: "l" + (k + 1), t0: r3(t0), t1: r3(Math.max(t1, t0 + SUB_MIN)), text: l.text, by: "you" };
  });
  S.selSub = null;
  subsChanged();
  listFlash("Now subtitle lines: type new times, retype them, + to add, × to delete.");
}
export function paintLine(t) {
  const times = listMode() === "subs" ? S.subs.map(c => c.t0) : S.D.lines.map(l => l.t);
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
    const c = S.subs.find(x => x.id === ti.dataset.subT);
    if (c) { S.selSub = c.id; S.selFx = null; seek(ti.dataset.edge === "t0" ? c.t0 : Math.max(c.t0, c.t1 - 0.05)); call.paintAll(); }
  }
});
subsList.addEventListener("click", ev => {
  const tick = ev.target.closest("[data-sub-pick]");
  if (!tick) return;
  tick.blur();
  pickLine(tick.dataset.subPick, tick.checked, ev.shiftKey);
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
        const c = S.subs.find(x => x.id === ti.dataset.subT);
        if (c) ti.value = fmt(c[ti.dataset.edge], 2);
      }
      call.paintAll();
    }, 0);
    return;
  }
  const sp = ev.target.closest("[data-sub-text]");
  if (!sp) return;
  setTimeout(() => { setSubText(sp.dataset.subText, sp.textContent); call.paintAll(); }, 0);
});
export const lineEnd = k => (k + 1 < S.D.lines.length ? S.D.lines[k + 1].t : S.D.duration);
export function paintTranscript() {
  const box = $("[data-ed-tx]"), t = S.D.transcript;
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
export async function transcribe() {
  try {
    const d = await post(`/moment/${pk}/transcribe`, {});
    S.D.transcript = d.transcript;
    paintTranscript();
    pollTranscript();
  } catch (e) { flash(e.message); }
}
export function pollTranscript() {
  clearTimeout(txPoll);
  txPoll = setTimeout(async () => {
    try {
      const d = await (await fetch(`/api/moment/${pk}/transcript?kind=${S.kind}`, { cache: "no-store" })).json();
      const was = S.D.transcript.state;
      S.D.transcript = d.transcript;
      if (d.transcript.state === "queued" || d.transcript.state === "running") pollTranscript();
      else if (was !== "none") {
        S.D.lines = d.lines;
        Object.assign(S.D, { subs_auto: d.subs_auto, subs_auto_by_mode: d.subs_auto_by_mode, sub_words: d.sub_words,
                           sub_language: d.sub_language, voices: d.voices });
        if (!S.subsMine) S.subs = cloneFx(d.subs_auto);     // lines that follow the words: the new ones
        S.listFor = null;
        S.subKey = "";
        call.paintAll();
      }
      paintTranscript();
    } catch (e) { pollTranscript(); }
  }, 3000);
}
