"""Running an AI edit: gather the material (pieces, audio, libraries), ask Claude, and
turn the answer into a saved edit. auto_edit does all three in one call; edit.direct
uses material / ask / apply to keep a session going."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from chatwatcher.analyze.claude_runner import ClaudeError, ClaudeRunner
from chatwatcher.analyze.prompts import compress_chat
from chatwatcher.analyze.sheet import contact_sheet
from chatwatcher.config import Config
from chatwatcher.edit import layout as lay
from chatwatcher.edit import overlays as ovl
from chatwatcher.edit import pieces as pc, regions, service
from chatwatcher.edit.audio import analyse
from chatwatcher.edit.autoedit.assemble import apply_volume, assemble
from chatwatcher.edit.autoedit.camera import camera_track, layout_effects
from chatwatcher.edit.autoedit.effects import (caption_effects, cutaway_effects, hit_effects, layer_effects, overlay_effects,
                                              sound_effects, spot_effects, text_effects, transition_effects)
from chatwatcher.edit.autoedit.plan import (MAX_CAPTIONS, MAX_CUTAWAYS, MAX_LAYERS, MAX_LAYOUTS, MAX_OVERLAYS, MAX_SPOTS, MAX_TEXTS,
                                           AutoEditError, Plan, parse_keep, parse_range, parts)
from chatwatcher.edit.autoedit.prompt import SYSTEM, TRANSITION_LIST, build_prompt, schema
from chatwatcher.edit.render import lengths
from chatwatcher.edit.sounds import list_sounds

WEB_TOOLS = ("WebSearch", "WebFetch")
INTENSITY = {"light": 30.0, "normal": 15.0, "heavy": 7.0}   # at most one sound per this many seconds
VISUAL_INTENSITY = {"light": 30.0, "normal": 12.0, "heavy": 6.0}   # and one zoom/shake/flash per this many


@dataclass
class Result:
    segments: list[list[float]]
    effects: list[dict[str, Any]]
    subs: list[dict[str, Any]] | None
    plan: Plan
    note: str
    hook_title: str
    model: str | None
    usage: dict[str, int]
    counts: dict[str, int]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Material:
    """Everything an AI edit is made from: the clip as pieces, its audio, the library."""
    clip: service.ClipRef
    kind: str
    words: dict[str, Any]
    wf: Any
    ps: list[pc.Piece]
    analysis: dict[str, Any]
    meta: dict[str, Any]
    sounds: list[dict[str, Any]]
    overlays: list[dict[str, Any]]
    boxes: dict[str, dict[str, float]]
    row: sqlite3.Row | None
    base: sqlite3.Row | None
    mine: list[dict[str, Any]]                # effects the editor placed (they stay)
    max_sounds: int
    max_hits: int
    max_trans: int
    frames: list[dict[str, Any]]
    visible: set[str]
    chat: str
    inserts: list[dict[str, Any]] = field(default_factory=list)   # this clip's (edit.inserts), for cutaways

    def prompt(self) -> str:
        return build_prompt(meta=self.meta, analysis=self.analysis, ps=self.ps, kind=self.kind, sounds=self.sounds,
                            boxes=self.boxes, placed=self.mine, chat=self.chat, max_sounds=self.max_sounds,
                            max_hits=self.max_hits, frames=self.frames, visible=self.visible,
                            overlays=self.overlays, inserts=self.inserts)

    def schema(self, chat: bool = False) -> dict[str, Any]:
        return schema(self.max_sounds, self.max_hits, self.max_trans, chat=chat,
                      max_overlays=MAX_OVERLAYS if self.overlays else 0, short=self.kind == "short")

    def out_size(self) -> tuple[int, int]:
        return (lay.OUT_W, lay.OUT_H) if self.kind == "short" else service.size(self.clip)

    def sheet(self) -> bytes | None:
        return contact_sheet(self.clip.folder, self.frames)


def material(cfg: Config, conn: sqlite3.Connection, moment_pk: int, kind: str = "short",
             intensity: str = "normal") -> Material:
    service.check_kind(kind)
    if not cfg.claude.enabled:
        raise AutoEditError("claude.enabled is false in the config.", "disabled")
    clip = service.clip_ref(conn, moment_pk)
    words = service.words(clip)
    if not words:
        raise AutoEditError("AI edit cuts between Whisper's words: press 🎙 Transcribe first.", "invalid")
    wf = analyse(clip.path, clip.folder / service.WAVEFORM_FILE)
    chat_path = clip.folder / "chat_excerpt.txt"
    chat_text = chat_path.read_text(encoding="utf-8", errors="replace") if chat_path.is_file() else ""
    ps = pc.build(words, clip.duration_s, wf.hits, pc.chat_times(chat_text))
    pc.name_voices(ps, words, clip.voices)
    sounds = list_sounds(conn)
    boxes = regions.get(conn, regions.scope_for(conn, moment_pk)[0])
    row = service.stored(conn, moment_pk, kind)
    base = row if row is not None or kind == "long" else service.stored(conn, moment_pk, "long")
    mine = [fx for fx in service.effects_to_clip(service.effects(base), clip) if fx.get("by") != "claude"]
    length_guess = sum(p.w1 - p.w0 for p in ps if p.kind == "talk") or clip.duration_s
    max_sounds = max(1, min(20, round(length_guess / INTENSITY.get(intensity, INTENSITY["normal"])))) if sounds else 0
    max_hits = max(1, min(12, round(length_guess / VISUAL_INTENSITY.get(intensity, VISUAL_INTENSITY["normal"]))))
    max_trans = max(1, min(8, round(length_guess / 8)))
    meta = service.meta(clip)
    visible = set(boxes)
    if kind == "short":                        # which regions the Short's layout already shows
        sw, sh = service.size(clip)
        base_ps = lay.panels(service.current_layout(conn, moment_pk, clip, row), boxes, sw, sh)
        visible = {n for n, b in boxes.items() if lay.map_point(b["x"] + b["w"] / 2, b["y"] + b["h"] / 2, base_ps)}
    every = ovl.list_overlays(conn, moment_pk)
    return Material(clip, kind, words, wf, ps, service.latest_analysis(conn, moment_pk), meta, sounds,
                    [o for o in every if o.get("moment_id") is None], boxes, row,
                    base, mine, max_sounds, max_hits, max_trans, meta.get("frames") or [], visible,
                    compress_chat(chat_text), [o for o in every if o.get("moment_id") is not None])


def ask(cfg: Config, m: Material, prompt: str, *, runner: ClaudeRunner | None = None, model: str | None = None,
        effort: str | None = None, images: bool = True, chat: bool = False, session: str | None = None,
        resume: bool = False, web: bool = False) -> Any:
    """One Claude call for an edit. Raises AutoEditError with a hint the page can show.
    web: Claude may search the web and read pages (to find a reference the director asks for)."""
    sheet = m.sheet() if images else None
    try:
        runner = runner or ClaudeRunner(cfg.claude)
        return runner.run(prompt, m.schema(chat=chat),
                          system=SYSTEM.format(language=cfg.claude.output_language, transitions=TRANSITION_LIST,
                                             max_texts=MAX_TEXTS, max_spots=MAX_SPOTS, max_captions=MAX_CAPTIONS,
                                             max_cutaways=MAX_CUTAWAYS, max_layers=MAX_LAYERS,
                                             max_layouts=MAX_LAYOUTS),
                          cwd=m.clip.folder, model=model, effort=effort or cfg.claude.edit_effort,
                          images=[sheet] if sheet else None, session=session, resume=resume,
                          **({"tools": list(WEB_TOOLS)} if web else {}))
    except ClaudeError as e:
        hint = {"usage_limit": "Claude's usage limit is reached; try again after it resets.",
                "auth": "The claude CLI isn't logged in. Run `claude auth login` in a terminal.",
                "timeout": "Claude took too long to answer; try again."}.get(e.kind)
        raise AutoEditError(hint or str(e), e.kind) from e


def apply(cfg: Config, conn: sqlite3.Connection, moment_pk: int, m: Material, d: dict[str, Any], *,
          model: str | None = None, usage: dict[str, int] | None = None, captions_on: bool = True,
          restyle: bool = True, save: bool = True) -> Result:
    """Turn Claude's answer into the edit (cut, camera, effects, captions, a Short's hook
    title) and save it. Effects you placed yourself are kept. save=False: only return it.

    Subtitles you changed (retyped lines, who says what, so their colours) are never
    replaced: only subtitles that still follow Whisper's words get Claude's emphasis.
    restyle: also switch them to word-by-word captions in the bubble look (a new AI edit;
    not when you go on directing it, so your caption settings stay). Lines you changed keep
    their words and timing, and only get the look."""
    clip, ps, kind = m.clip, m.ps, m.kind
    plan = assemble(ps, parse_keep(d.get("keep") or [], len(ps)), parse_range(d.get("cold_open") or "", len(ps)),
                    clip.duration_s, speech=m.words.get("speech"))
    segs, volumes = apply_volume(d.get("volume") or [], ps, plan.segments)
    plan.segments = segs
    short = kind == "short"
    scenes = layout_effects(d.get("layouts") or [], ps, segs, m.boxes) if short else []
    cam = scenes + camera_track(d.get("camera") or [], ps, segs, m.boxes, short, m.visible, scenes, service.size(clip))
    layers = layer_effects(d.get("layers") or [], segs, m.boxes, service.size(clip), m.out_size())
    sfx = sound_effects(d.get("sounds") or [], segs, m.wf.hits, {x["id"] for x in m.sounds}, m.max_sounds)
    hits_fx = hit_effects(d.get("hits") or [], segs, m.wf.hits, m.boxes, clip.duration_s, m.max_hits)
    trans = transition_effects(d.get("transitions") or [], ps, segs, plan.cold_open, m.max_trans)
    ovs = overlay_effects(d.get("overlays") or [], segs, m.wf.hits, m.overlays, m.boxes, m.out_size())
    cuts = cutaway_effects(_fetch_links(cfg, conn, moment_pk, m, d.get("cutaways") or [], plan.warnings), segs,
                           m.inserts)
    texts = text_effects(d.get("texts") or [], segs, m.boxes, short)
    spots = spot_effects(d.get("spots") or [], segs, m.boxes)
    caps = caption_effects(d.get("captions") or [], ps, segs, m.boxes, clip.duration_s) if captions_on else []
    mine = [fx for fx in m.mine if fx.get("type") != "transition"]       # the cuts changed: old ones don't fit
    effects = (mine + cam + sfx + hits_fx + trans + cuts + ovs + layers + texts + spots + caps)[:service.MAX_EFFECTS]
    # The captions follow Whisper's words (saved as "auto", with Claude's emphasis words in
    # the style), so transcribing again updates them instead of leaving stale lines.
    hook = " ".join(str(d.get("hook_title") or "").split())[:lay.MAX_TITLE]
    cues = style = None
    layout = None
    old_cues, source, old = service.current_subs(cfg, m.base, clip, kind)
    if captions_on and source != "saved":
        style = old | {"emphasis": d.get("emphasis") or []}
        if restyle:
            style |= {"on": True, "mode": "words", "look": "bubble"} | ({"size": "large", "position": "top"} if short else {})
        cues = service.auto_cues(m.words, kind, style)
    elif captions_on:
        cues = old_cues                        # yours: kept as they are…
        if restyle:                            # …but they get the AI edit's look
            style = old | {"on": True, "look": "bubble"} | ({"size": "large", "position": "top"} if short else {})
    if short:
        layout = service.current_layout(conn, moment_pk, clip, m.row) | {"title": hook}
    total = sum(lengths(segs, clip.fps))
    if short and total > 60:
        plan.warnings.append(f"The cut is {total:.0f} s; Shorts over 60 s lose most viewers.")
    counts = {"camera": len(cam) - len(scenes), "layouts": len(scenes), "layers": len(layers), "sounds": len(sfx), "hits": len(hits_fx), "volume": volumes,
              "transitions": len(trans), "overlays": len(ovs), "cutaways": len(cuts), "texts": len(texts), "spots": len(spots),
              "captions": len(caps)}
    if not save:
        return Result(segs, effects, cues, plan, str(d.get("note") or ""), hook, model, usage or {}, counts, raw=d)
    service.save(cfg, conn, moment_pk, segs, None, effects, subs="auto" if style is not None and source != "saved" else None,
                 subs_style=style, kind=kind, layout=layout)
    saved = service.effects_to_clip(service.effects(service.stored(conn, moment_pk, kind)), clip)
    return Result(segs, saved, cues, plan, str(d.get("note") or ""), hook, model, usage or {}, counts, raw=d)


def _fetch_links(cfg: Config, conn: sqlite3.Connection, moment_pk: int, m: Material, items: list[str],
                 warnings: list[str]) -> list[str]:
    """Cutaways that give a link instead of an insert id: download it into the clip's
    inserts (m.inserts grows) and use the first insert it gave."""
    from chatwatcher.edit import inserts as ins
    out = []
    for item in items[:MAX_CUTAWAYS]:
        bits = parts(item)
        if len(bits) > 2 and ins.is_link(bits[2]):
            try:
                got = ins.fetch(cfg, conn, moment_pk, bits[2])
            except ins.InsertError as e:
                warnings.append(f"Couldn't bring in {bits[2]}: {e}.")
                continue
            m.inserts += [g for g in got if g["id"] not in {o["id"] for o in m.inserts}]
            bits[2] = got[0]["id"]
            item = " | ".join(bits)
        out.append(item)
    return out


def auto_edit(cfg: Config, conn: sqlite3.Connection, moment_pk: int, *, kind: str = "short",
              intensity: str = "normal", runner: ClaudeRunner | None = None, model: str | None = None,
              effort: str | None = None, captions_on: bool = True, save: bool = True) -> Result:
    """Ask Claude for a whole edit of the clip, in one call with no session, and save it.
    (The editor's ✨ AI edit starts a session you can go on directing: edit.direct.)"""
    m = material(cfg, conn, moment_pk, kind, intensity)
    res = ask(cfg, m, m.prompt(), runner=runner, model=model, effort=effort)
    return apply(cfg, conn, moment_pk, m, res.data, model=res.model, usage=res.usage, captions_on=captions_on,
                 save=save)
