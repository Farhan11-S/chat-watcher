// ✨ AI edit and Direct: Claude makes or changes the edit; the conversation about it.
import { $, S, call, pk } from "./state.js";
import { body, clone, cloneFx, curFx, el, fxStatus, post, setSaved } from "./util.js";
import { preload } from "./audio.js";
import { showRender } from "./io.js";
import { changed, pushUndo } from "./editing.js";
import { showTab } from "./panels.js";
import { fetchEdit, paintKind } from "./layout.js";

export function clearClaude() {
  const n = S.fx.filter(f => f.by === "claude").length;
  if (!n) { fxStatus("None of the effects are Claude's.", ""); return; }
  pushUndo();
  S.fx = S.fx.filter(f => f.by !== "claude");
  if (S.selFx && !curFx()) S.selFx = null;
  changed();
  fxStatus(`Removed ${n} of Claude's effects. Ctrl+Z brings them back.`, "");
}
// Claude remade the edit on the server: load it, with the edit before it on the undo stack.
async function takeAiEdit(d) {
  const fresh = await fetchEdit(S.kind);
  if (fresh.ok === false) throw new Error(fresh.error || "Couldn't reload the edit.");
  pushUndo();
  S.D = fresh;
  S.segs = clone(fresh.segments);
  S.fx = cloneFx(fresh.effects || []);
  S.source = fresh.source;
  S.subs = cloneFx(fresh.subs || []);
  S.subStyle = { ...fresh.subs_style };
  S.subsMine = fresh.subs_source === "saved";
  if (fresh.layout) S.layout = { ...fresh.layout };
  S.sel = 0; S.selFx = null; S.selSub = null; S.subKey = ""; S.listFor = null; S.dirty = false;
  S.D.saved = true;
  setSaved("Saved");
  paintKind();
  call.paintAll();
  showRender(d.render);
  preload();
}
export async function autoEdit() {
  const b = $('[data-ed-act="autoedit"]');
  if (S.segs.length > 1 && !confirm("✨ AI edit replaces your cut, Claude's effects and the subtitles with a new edit, and starts a new conversation in ✨ Direct (your own effects stay; Ctrl+Z brings everything back). Go on?")) return;
  b.disabled = true;
  clearTimeout(S.saveTimer);
  fxStatus("Claude is editing the whole clip: cuts, hook, camera and effects… (usually 30–90 s)", "busy");
  try {
    const d = await post(`/moment/${pk}/autoedit`, { ...body(), intensity: $("[data-ed-intensity]").value });
    await takeAiEdit(d);
    paintChat(d.messages, true);
    const c = d.counts || {};
    const bits = [`cut ${d.cut_s.toFixed(0)} s (kept ${d.kept} of ${d.pieces} pieces)`,
                  d.cold_open && "opens on a teaser", `${c.camera || 0} camera moves`,
                  c.sounds && `${c.sounds} sounds`, c.hits && `${c.hits} shakes/flashes`].filter(Boolean);
    fxStatus(`Claude's edit: ${bits.concat(c.volume ? [`${c.volume} volume change${c.volume > 1 ? "s" : ""}`] : []).join(", ")}. ${d.note || ""} ${(d.warnings || []).join(" ")} Tell it what to change in ✨ Direct. Ctrl+Z undoes.`, "ok");
    showTab("ai");
  } catch (e) {
    fxStatus(e.message, "err");
  } finally {
    b.disabled = false;
  }
}

// ✨ Direct: a conversation with the AI edit's Claude session about this edit (long and
// short apart). It remembers the clip and its own answers; each message also tells it
// the edit as it is now. When it changes the edit, Ctrl+Z brings yours back.
let chatBusy = false, chatNew = false;
function paintChat(messages, started = false) {
  const log = $("[data-ed-chat]");
  log.textContent = "";
  if (started) chatNew = false;
  if (!messages || !messages.length || chatNew) {
    log.appendChild(el("p", "faint ed-chat-empty", chatNew
      ? "New session: your next message starts a new AI edit from the whole clip, with your words as its direction."
      : "No conversation yet. Press ✨ AI edit, or write how you want this clip cut and Claude makes the edit."));
    return;
  }
  for (const m of messages) {
    const b = el("div", "ed-bubble " + m.role + (m.applied ? " applied" : ""));
    b.appendChild(el("span", "", m.text));
    if (m.role === "assistant") b.appendChild(el("small", "ed-bubble-meta",
      (m.applied ? "changed the edit · " : "answered · ") + (m.model || "").replace(/^claude-/, "")));
    log.appendChild(b);
  }
  log.scrollTop = log.scrollHeight;
}
export async function loadChat() {
  try {
    const d = await (await fetch(`/api/moment/${pk}/direct?kind=${S.kind}`, { cache: "no-store" })).json();
    chatNew = false;
    paintChat(d.messages);
  } catch (e) { /* the pane says there's no conversation */ }
}
async function sendChat() {
  const box = $("[data-ed-chat-text]"), send = $("[data-ed-chat-send]");
  const message = box.value.trim();
  if (!message || chatBusy) return;
  chatBusy = true; send.disabled = true; box.value = "";
  clearTimeout(S.saveTimer);
  const log = $("[data-ed-chat]");
  if (chatNew || log.querySelector(".ed-chat-empty")) log.textContent = "";
  log.appendChild(el("div", "ed-bubble user", message));
  const wait = el("div", "ed-bubble assistant pending", "Claude is working on it… (usually 20–90 s)");
  log.appendChild(wait);
  log.scrollTop = log.scrollHeight;
  try {
    const d = await post(`/moment/${pk}/direct`, { ...body(), intensity: $("[data-ed-intensity]").value,
                                                   message, new: chatNew });
    if (d.applied) await takeAiEdit(d);
    paintChat(d.messages, true);
    if (d.applied) fxStatus("Claude changed the edit. Ctrl+Z undoes.", "ok");
  } catch (e) {
    wait.remove();
    log.appendChild(el("div", "ed-bubble failed", e.message));
    box.value = message;
  } finally {
    chatBusy = false; send.disabled = false;
  }
}
export function newChat() {
  if (chatBusy) return;
  chatNew = true;
  paintChat([]);
  $("[data-ed-chat-text]").focus();
}
$("[data-ed-chat-form]").addEventListener("submit", ev => { ev.preventDefault(); sendChat(); });
$("[data-ed-chat-text]").addEventListener("keydown", ev => {
  if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); sendChat(); }
});
