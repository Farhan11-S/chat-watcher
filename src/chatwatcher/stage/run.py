"""Claude writes the 3D scene, and you direct it in words.

Write (or Write again) starts a Claude session for the moment's stage: it gets the cut as
a timeline (subtitle lines, laughs, where chat peaked), what the clip is about, the set and
everything a scene can say (stage.script), and answers with the whole scene. Then you write
to it ("hold the window shot longer", "more shocked at 18s") in the same session, and it
answers with the whole scene again (apply = true) or only answers (apply = false).

Cues it changes because you asked are marked by = "you"; writing again keeps those exactly,
unless you start over. Sessions live in the claude CLI's history for the clip folder, like
the editor's Direct (edit/direct.py); one that can't be resumed is started again with the
recent messages."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from chatwatcher.analyze.claude_runner import ClaudeError, ClaudeRunner
from chatwatcher.config import Config
from chatwatcher.edit import clipref as cr
from chatwatcher.stage import models, script, store, track as tk

MAX_MESSAGE = 2000
RECAP = 6
KEEP_NEAR_S = 0.4         # a cue of yours replaces Claude's cues this close to it in the same lane

SYSTEM = """\
You direct short 3D re-enactments of VTuber stream moments. The talent's official 3D \
model sits in a set and acts out the moment, while the original stream audio plays. Her \
lip-sync, blinks and laughter are made automatically from the audio: never script them. \
You write the rest as a scene: camera shots, her body poses, her facial expressions, and \
the set's own cues, each one a list of timed cues (seconds from the start of the cut).

How a good scene moves:
- The camera never sits idle: a new shot every 2-4 s, cut on the beat of what she says \
(the start of a line, a punchline, a reaction), never shorter than 1 s. Mix sizes; punch \
in to close or face on reactions and punchlines, pull out to wide or a set shot to reset. \
push makes a shot creep in while it is up (0.03-0.08 normal, 0.15-0.3 for tension).
- yaw turns the camera around her (0 = straight on, + = from her left side, where the \
set's feature is, - = from her right). Vary it a little between shots, rarely beyond ±35.
- Poses change on beats and are held 2-6 s; ease is how long she takes to move into it \
(0.15-0.3 snappy, 0.5-1 slow). Use "look" or "point" when she talks about or reacts to \
the set's feature. Match her body to the laughs: "laugh" just before a laugh span.
- Faces follow the feeling of each line; change them at line starts. "laugh" closes her \
eyes, so use it only over laughs.
- Exaggerate like a Tom & Jerry cartoon, through her body (never her mouth: that follows \
her voice). Actions are short cartoon gestures on top of her pose: put a "take" on the \
biggest shock or realisation, smaller ones ("jolt", "recoil", "whip_look") on surprises, \
"slump" or "facepalm" on embarrassment, "bounce" on excitement, "laugh_fit" over a long \
laugh, "nod" / "shake_head" / "shrug" on those words. Start an action a beat before the \
word it plays so the hit (given for each) lands on it, and make sure the camera is on \
her when it hits: never the window or screen view, and for whole-body gags (take, bounce, \
laugh_fit, shrug, facepalm, slump) a medium or wide shot, not her face. One action every \
few seconds at most, and save the biggest for the punchline; size 1.0 is full cartoon, \
0.6 subtler.
- The set's cues stage what she describes (in the cabin, a cloud covering the city lights \
when she says it went dark, and sliding away when she realises).
- Cues the director asked for are marked by = "you": keep them exactly unless the \
director asks to change them. Mark a cue by = "you" when you add or change it because \
the director asked; otherwise by = "claude".

Answer only with the structured output. note: one or two sentences: for a first answer, \
the idea of the scene; later, your answer to the director. Write it in {language}."""

FIRST = """\
The moment: {title}
{about}

The cut is {duration:.1f} s long. Times below are seconds from its start.

What she says (subtitle lines):
{lines}

Laughing (voice with no words): {laughs}
Talking with no subtitle (mumbles): {mumbles}
Chat reacted most around: {peak}

The model: {model}.
The set: {set_label}. {set_about}
Shots: {shots}
Poses: {poses}
Faces: {faces}
Actions: {actions}
{lanes}
{keep}
Write the whole scene: apply = true."""

FOLLOW_UP = """\
The director says:
<director>
{message}
</director>

The scene as it is now (the director may have changed it since your last answer):
{scene}

If the director asks for a change, answer with apply = true and the whole scene again: \
change what they ask for (by = "you" on those cues) and keep the rest as it is. If they \
only ask a question, answer with apply = false and put the answer in note."""


class StageRunError(Exception):
    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind       # disabled | invalid | usage_limit | auth | timeout | no_session | error


@dataclass
class Turn:
    scene: dict[str, Any] | None      # the new scene, or None when Claude only answered
    reply: str
    warnings: list[str]
    messages: list[dict[str, Any]]
    fresh: bool


# ── the conversation ─────────────────────────────────────────────────────────

def thread(conn: sqlite3.Connection, moment_pk: int) -> tuple[str | None, list[dict[str, Any]]]:
    last = conn.execute("SELECT session_id FROM stage_chats WHERE moment_id = ? ORDER BY id DESC LIMIT 1",
                        (moment_pk,)).fetchone()
    if last is None:
        return None, []
    rows = conn.execute("SELECT role, text, applied, model, created_at FROM stage_chats "
                        "WHERE moment_id = ? AND session_id = ? ORDER BY id", (moment_pk, last["session_id"])).fetchall()
    return last["session_id"], [dict(r) | {"applied": bool(r["applied"])} for r in rows]


def _say(conn: sqlite3.Connection, moment_pk: int, session: str, role: str, text: str, applied: bool = False,
         model: str | None = None, usage: dict[str, int] | None = None) -> None:
    conn.execute("INSERT INTO stage_chats (moment_id, session_id, role, text, applied, model, usage_json) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (moment_pk, session, role, text, int(applied), model, json.dumps(usage) if usage else None))


# ── what Claude is told ──────────────────────────────────────────────────────

def schema(set_name: str) -> dict[str, Any]:
    st = script.SETS[set_name]
    num = {"type": "number"}
    by = {"type": "string", "enum": ["you", "claude"]}

    def lane(field: str, names: list[str], extra: dict[str, Any]) -> dict[str, Any]:
        return {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                           "properties": {"t": num, field: {"type": "string", "enum": names},
                                                          **extra, "by": by},
                                           "required": ["t", field]}}
    lanes = {name: {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                               "properties": {"t": num, "v": num, "ease": num, "by": by},
                                               "required": ["t", "v"]}}
             for name in st["lanes"]}
    return {"type": "object", "additionalProperties": False,
            "properties": {
                "apply": {"type": "boolean"},
                "note": {"type": "string"},
                "shots": lane("shot", list(script.SHOTS) + list(st["shots"]), {"yaw": num, "push": num}),
                "poses": lane("pose", list(script.POSES), {"ease": num}),
                "faces": lane("face", list(script.FACES), {"ease": num}),
                "actions": lane("action", list(script.ACTIONS), {"size": num}),
                "lanes": {"type": "object", "additionalProperties": False, "properties": lanes},
            },
            "required": ["apply", "note", "shots", "poses", "faces", "actions"]}


def _fmt_spans(spans: list[list[float]]) -> str:
    return ", ".join(f"{a:.1f}-{b:.1f}" for a, b, *_ in spans) or "none"


def _peak(conn: sqlite3.Connection, moment_pk: int, st: dict[str, Any], tr: dict[str, Any]) -> str:
    m = conn.execute("SELECT peak_s, features_json FROM moments WHERE id = ?", (moment_pk,)).fetchone()
    if m is None:
        return "unknown"
    off, out = 0.0, []
    for a, b in st["segments"]:
        if a <= m["peak_s"] <= b:
            out.append(f"{off + m['peak_s'] - a:.1f} s")
        off += b - a
    return ", ".join(out) or "outside the cut"


def first_prompt(conn: sqlite3.Connection, moment_pk: int, st: dict[str, Any], tr: dict[str, Any],
                 profile: dict[str, Any] | None, keep: list[dict[str, Any]]) -> str:
    a = cr.latest_analysis(conn, moment_pk)
    v = conn.execute("SELECT v.title FROM moments m JOIN videos v ON v.id = m.video_id WHERE m.id = ?",
                     (moment_pk,)).fetchone()
    about = " ".join(str(a.get(k) or "") for k in ("summary", "context", "why_it_works") if a.get(k)).strip()
    lines = "\n".join(f"{x['t0']:6.1f}-{x['t1']:5.1f}  {x['text']}" for x in tr["lines"]) or "(no subtitles)"
    stset = script.SETS[st["set_name"]]
    lanes = "".join(f"Set cue {n!r}: {spec['about']}\n" for n, spec in stset["lanes"].items())
    keep_txt = ""
    if keep:
        keep_txt = ("\nThe director's own cues from before (by = \"you\"): keep each exactly, in your answer too:\n"
                    + json.dumps(keep, ensure_ascii=False) + "\n")
    who = (profile or {}).get("name") or "the talent"
    return FIRST.format(
        title=(a.get("titles") or [{}])[0].get("text") or (v["title"] if v else "") or f"moment {moment_pk}",
        about=about or "(no analysis yet)", duration=tr["duration"], lines=lines,
        laughs=_fmt_spans(tr["laughs"]), mumbles=_fmt_spans(tr["mumbles"]), peak=_peak(conn, moment_pk, st, tr),
        model=who, set_label=stset["label"], set_about=stset["about"],
        shots="; ".join(f"{k} ({d})" for k, d in {**script.SHOTS, **stset["shots"]}.items()),
        poses="; ".join(f"{k} ({d})" for k, d in script.POSES.items()),
        faces="; ".join(f"{k} ({d})" for k, d in script.FACES.items()),
        actions="; ".join(f"{k} ({d}; {script.ACTION_TIMES[k][0]} s, hits {script.ACTION_TIMES[k][1]} s in)"
                          for k, d in script.ACTIONS.items()),
        lanes=lanes, keep=keep_txt)


def yours(scene: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Every cue the director asked for, with the lane it is in."""
    out = []
    for lane in ("shots", "poses", "faces", "actions"):
        out += [c | {"lane": lane} for c in (scene or {}).get(lane, []) if c.get("by") == "you" and (c["t"] > 0 or lane == "actions")]
    for name, cues in ((scene or {}).get("lanes") or {}).items():
        out += [c | {"lane": f"lanes.{name}"} for c in cues if c.get("by") == "you" and c["t"] > 0]
    return out


def keep_yours(scene: dict[str, Any], keep: list[dict[str, Any]]) -> dict[str, Any]:
    """Put the director's cues back where a fresh answer moved or dropped them."""
    for c in keep:
        lane = c["lane"]
        cue = {k: v for k, v in c.items() if k != "lane"}
        if lane.startswith("lanes."):
            lst = scene.setdefault("lanes", {}).setdefault(lane[6:], [])
        else:
            lst = scene.setdefault(lane, [])
        lst[:] = [x for x in lst if abs(x["t"] - cue["t"]) > KEEP_NEAR_S] + [cue]
        lst.sort(key=lambda x: x["t"])
    return scene


# ── asking ───────────────────────────────────────────────────────────────────

def _sheet(clip: cr.ClipRef) -> bytes | None:
    from chatwatcher.analyze.sheet import contact_sheet
    frames = cr.meta(clip).get("frames") or []
    return contact_sheet(clip.folder, frames) if frames else None


def _ask(cfg: Config, clip: cr.ClipRef, set_name: str, prompt: str, *, runner: ClaudeRunner | None,
         session: str, resume: bool, images: bool) -> Any:
    sheet = _sheet(clip) if images else None
    try:
        runner = runner or ClaudeRunner(cfg.claude)
        return runner.run(prompt, schema(set_name), system=SYSTEM.format(language=cfg.claude.output_language),
                          cwd=clip.folder, effort=cfg.claude.edit_effort, images=[sheet] if sheet else None,
                          session=session, resume=resume)
    except ClaudeError as e:
        hint = {"usage_limit": "Claude's usage limit is reached; try again after it resets.",
                "auth": "The claude CLI isn't logged in. Run `claude auth login` in a terminal.",
                "timeout": "Claude took too long to answer; try again."}.get(e.kind)
        raise StageRunError(hint or str(e), e.kind) from e


def direct(cfg: Config, conn: sqlite3.Connection, moment_pk: int, message: str = "", *, new: bool = False,
           start_over: bool = False, runner: ClaudeRunner | None = None) -> Turn:
    """Send the director's message to the stage's session (empty with new = write the scene
    from scratch). Saves the scene Claude writes. Waits for Claude (30-90 s)."""
    if not cfg.claude.enabled:
        raise StageRunError("claude.enabled is false in the config.", "disabled")
    message = str(message or "").strip()[:MAX_MESSAGE]
    st = store.get(conn, moment_pk)
    if st is None:
        raise StageRunError("Set the stage up first.", "invalid")
    tr = tk.ensure(cfg, conn, moment_pk, st)
    clip = cr.clip_ref(conn, moment_pk)
    profile = None
    if st.get("model"):
        try:
            profile = models.load(cfg, st["model"])
        except models.ModelError:
            profile = None
    session, history = thread(conn, moment_pk)
    fresh = new or session is None
    keep = [] if start_over else yours(st.get("scene"))
    res = None
    if not fresh:
        if not message:
            raise StageRunError("Write what you want changed.", "invalid")
        prompt = FOLLOW_UP.format(message=message, scene=json.dumps(st.get("scene") or {}, ensure_ascii=False))
        try:
            res = _ask(cfg, clip, st["set_name"], prompt, runner=runner, session=session, resume=True, images=False)
        except StageRunError as e:
            if e.kind != "no_session":
                raise
            fresh = True
    if fresh:
        recap = ""
        if not new and history:
            said = [f"{'Director' if h['role'] == 'user' else 'You'}: {h['text']}" for h in history[-RECAP:]]
            recap = ("\n\nEarlier in this conversation (the session was lost):\n" + "\n".join(said)
                     + "\n\nThe scene as it is now:\n" + json.dumps(st.get("scene") or {}, ensure_ascii=False))
        session = str(uuid.uuid4())
        prompt = first_prompt(conn, moment_pk, st, tr, profile, keep)
        if message:
            prompt += f"\n\nThe director's direction (it wins over the defaults):\n<director>\n{message}\n</director>"
        res = _ask(cfg, clip, st["set_name"], prompt + recap, runner=runner, session=session, resume=False, images=True)
        res.data["apply"] = True
    assert res is not None and session is not None
    _say(conn, moment_pk, session, "user", message or ("✨ Write the scene" if not start_over else "✨ Start over"))
    d = res.data
    reply = " ".join(str(d.get("note") or "").split())
    scene, warnings = None, []
    if d.get("apply"):
        raw = {k: d.get(k) for k in ("shots", "poses", "faces", "actions", "lanes")} | {"note": reply if fresh else
                                                                            (st.get("scene") or {}).get("note", "")}
        clean, warnings = script.validate(raw, tr["duration"], st["set_name"], by="claude")
        if fresh and keep:
            clean = script.validate(keep_yours(clean, keep), tr["duration"], st["set_name"], by="claude")[0]
        scene, more = store.save_scene(conn, moment_pk, clean, by="claude")
        warnings += more
        counts = f"{len(scene['shots'])} shots, {len(scene['poses'])} poses, {len(scene['faces'])} faces"
        reply = (reply + f" ({counts})").strip()
        if warnings:
            reply += " Skipped: " + "; ".join(warnings[:4])
    if fresh and not new and history:
        reply = "(The earlier session couldn't be resumed, so this is a new one.) " + reply
    _say(conn, moment_pk, session, "assistant", reply or "(no answer)", applied=scene is not None,
         model=res.model, usage=res.usage)
    return Turn(scene, reply, warnings, thread(conn, moment_pk)[1], fresh)
