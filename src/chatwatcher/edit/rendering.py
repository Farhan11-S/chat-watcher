"""Rendering a saved edit to edit.mp4 (16:9) or short.mp4 (9:16): gathers the cut, the
effects, sounds, subtitles, layout and intro, calls edit.render, and records what was
rendered so render_state can tell when it goes stale."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Callable

from chatwatcher.config import Config
from chatwatcher.edit import captions as cap
from chatwatcher.edit import clipref as cr
from chatwatcher.edit import layout as lay
from chatwatcher.edit import overlays as ovl
from chatwatcher.edit import spots as spt
from chatwatcher.edit import texts as txt
from chatwatcher.edit import motion
from chatwatcher.edit.clipref import TABLE, EditError, check_kind, clip_ref
from chatwatcher.edit.editstore import current_layout
from chatwatcher.edit.substate import current_subs, subs_rendered
from chatwatcher.edit.validate import short_regions, short_visuals, split_at, used_regions, visuals


def render_moment(cfg: Config, conn: sqlite3.Connection, moment_pk: int, *, kind: str = "long",
                  on_progress: Callable[[float], None] | None = None) -> dict[str, Any]:
    """Render the saved edit to <clip folder>/edit.mp4 (a Short: short.mp4).
    Returns {duration_s, size_bytes, missing, …}."""
    from chatwatcher.edit.render import SoundCue, place, render
    from chatwatcher.edit.sounds import get_sound, sound_path
    check_kind(kind)
    short = kind == "short"
    table = TABLE[kind]
    clip = clip_ref(conn, moment_pk)
    row = cr.stored(conn, moment_pk, kind)
    if row is None:
        raise EditError("Nothing to render: no edit saved for this clip." if not short
                        else "Nothing to render: no Short saved for this clip.", "invalid")
    stream = json.loads(row["segments_json"])
    segs = cr.to_clip(stream, clip)
    if not segs:
        raise EditError("The edit's segments are outside the downloaded clip. Pick them again.", "invalid")
    stream_fx = cr.effects(row)
    clip_fx = cr.effects_to_clip(stream_fx, clip)
    cues, _source, sub_style = current_subs(cfg, row, clip, kind)
    graph, layout, title = None, None, ""
    if short:
        layout = current_layout(conn, moment_pk, clip, row)
        boxes = short_regions(conn, moment_pk, stream_fx, layout)
        sw, sh = cr.size(clip)
        ps = lay.panels(layout, boxes, sw, sh)
        title = layout["title"]
        # Frame shots: the picture moves to another region for a while, so the cut is split
        # where they start and end, and each part gets its own picture and zoom targets.
        frames = [f for f in clip_fx if f.get("type") == lay.FRAME and lay.region_box(f.get("region") or "", boxes)]
        segs = split_at(segs, [t for f in frames for t in (f["t"], f["t"] + f["d"])], clip.fps)
        graph, pictures, no_region, ov_lists, ov_missing, text_lists, spot_lists = [], [], set(), [], set(), [], []
        cap_lists = []
        for s, e, _g in segs:
            fr = lay.frame_at((s + e) / 2, frames)
            pk = lay.frame_panels(lay.region_box(fr["region"], boxes), sw, sh) if fr else ps
            graph.append(lay.graph(pk, sw, sh))
            placed, gone = ovl.placed(cfg, conn, clip_fx, boxes, pk)
            ov_lists.append(placed)
            text_lists.append(txt.placed(clip_fx, boxes, pk))
            spot_lists.append(spt.placed(clip_fx, pk))
            cap_lists.append(cap.placed(clip_fx, boxes, pk))
            ov_missing |= gone
            pics, missing = short_visuals(clip_fx, boxes, pk)
            pictures.append(pics)
            no_region |= missing
        size = (lay.OUT_W, lay.OUT_H)
    else:
        boxes = used_regions(conn, moment_pk, stream_fx)
        pictures, no_region = visuals(clip_fx, boxes)
        pictures = [list(pictures) for _ in segs]          # one list per segment, for the transitions
        placed, ov_missing = ovl.placed(cfg, conn, clip_fx, boxes, None)
        ov_lists = [placed] * len(segs)
        text_lists = [txt.placed(clip_fx, boxes, None)] * len(segs)
        spot_lists = [spt.placed(clip_fx, None)] * len(segs)
        cap_lists = [cap.placed(clip_fx, boxes, None)] * len(segs)
        size = (clip.width, clip.height)
    for k, v in motion.transitions(segs, clip_fx):
        pictures[k].append(v)
    from chatwatcher.clip.media import probe as probe_media
    made_intro = []

    def intro_progress(frac: float) -> None:
        made_intro.append(frac)
        conn.execute(f"UPDATE {table} SET render_progress = ? WHERE moment_id = ?", (round(frac * 0.3, 3), moment_pk))

    # The intro first, when the long edit starts with one (rendered now if it's out of date).
    intro, intro_key, intro_s = None, None, 0.0
    if not short:
        from chatwatcher.brand.service import intro_for_edit
        intro, intro_key = intro_for_edit(cfg, conn, moment_pk, clip, on_progress=intro_progress)
        if intro:
            intro_s = max(1, round(probe_media(intro).duration_s * clip.fps)) / clip.fps
    sound_cues, missing = [], set()
    for fx in clip_fx:
        if fx.get("type", "sfx") != "sfx":
            continue
        snd = get_sound(conn, fx["sound"])
        if not snd or not sound_path(cfg, snd).is_file():
            missing.add(fx["sound"])
            continue
        for at in place(fx["t"], segs, clip.fps):
            sound_cues.append(SoundCue(at + intro_s, sound_path(cfg, snd), fx["gain"], fx["duck"], snd["duration_s"]))
    for fx in clip_fx:                                     # an overlay's own sound
        ov = (ovl.get_overlay(conn, fx.get("overlay") or "")
              if fx.get("type") == ovl.OVERLAY and fx.get("mode") != "picture" else None)
        snd_file = ovl.sound_path(cfg, ov) if ov else None
        if snd_file and snd_file.is_file():
            for at in place(fx["t"], segs, clip.fps):
                sound_cues.append(SoundCue(at + intro_s, snd_file, fx.get("gain", 0) + ov["gain_db"], 0.0,
                                           min(fx["d"], ov["duration_s"])))

    last = [0.0]

    start = 0.3 if made_intro else 0.0     # rendering the intro took the first part of the bar

    def progress(frac: float) -> None:
        frac = start + frac * (1 - start)
        if time.monotonic() - last[0] > 0.5:
            last[0] = time.monotonic()
            conn.execute(f"UPDATE {table} SET render_progress = ? WHERE moment_id = ?", (round(frac, 3), moment_pk))
        if on_progress:
            on_progress(frac)

    conn.execute(f"UPDATE {table} SET render_progress = ? WHERE moment_id = ?", (start, moment_pk))
    try:
        r = render(cfg.edit, clip.path, segs, clip.out_path(kind), fps=clip.fps, has_audio=clip.has_audio,
                   sounds=sound_cues, visuals=pictures, subs=cues if sub_style["on"] else [],
                   sub_style=sub_style, size=size, font=cfg.subtitles.font,
                   intro=intro, intro_s=intro_s, layout=graph, title=title, overlays=ov_lists, texts=text_lists, spots=spot_lists,
                   captions=cap_lists, on_progress=progress)
    finally:
        conn.execute(f"UPDATE {table} SET render_progress = NULL WHERE moment_id = ?", (moment_pk,))
    # The subtitles as a .srt on the edit's timeline (for YouTube's CC), burned in or not.
    from chatwatcher.edit.render import lengths
    from chatwatcher.subs.lines import on_edit, srt
    shown = [(a + intro_s, b + intro_s, *rest) for a, b, *rest in on_edit(cues, segs, lengths(segs, clip.fps))]
    srt_path = clip.srt_for(kind)
    if shown:
        srt_path.write_text(srt(shown), encoding="utf-8")
    else:
        srt_path.unlink(missing_ok=True)
    stream_subs, _ = subs_rendered(cfg, row, clip, kind)
    info = {"segments": stream, "effects": stream_fx, "regions": boxes, "subs": stream_subs, "subs_style": sub_style,
            "duration_s": round(r.duration_s, 2), "size_bytes": r.size_bytes, "sounds": len(sound_cues),
            "visuals": sum(f.get("type") in (*motion.TYPES, lay.FRAME, "transition", ovl.OVERLAY, txt.TEXT, spt.SPOT, cap.CAPTION) for f in clip_fx if any(s <= f["t"] < e for s, e, _ in segs)), "subtitles": len(shown) if sub_style["on"] else 0, "srt": bool(shown),
            "missing": sorted(missing | ov_missing), "missing_regions": sorted(no_region),
            "intro": intro_key, "intro_s": round(intro_s, 3)}
    if short:
        info |= {"layout": layout, "size": list(size)}
    conn.execute(f"UPDATE {table} SET rendered_json = ?, rendered_at = datetime('now') WHERE moment_id = ?",
                 (json.dumps(info, ensure_ascii=False), moment_pk))
    return info
