"""Your cut of one clip, kept in the `edits` table.

Segments are stored in stream time, so they stay right when the clip is downloaded again
with slightly different edges; the browser works in clip time (seconds into clip.mp4).
Each segment is [start, end, gain_db], in the order they play. Effects are tied to a
moment of the clip, not of the edit, so they move with the content when you trim or
reorder. Every effect has {"id", "type", "t", "by": "claude" | "you", "why"}, plus:
    sfx    "sound", "gain", "duck"           a sound effect
    punch  "d", "region", "zoom"             instant zoom on a region (see edit.regions)
    push   "d", "region", "zoom"             slow zoom towards a region
    shake  "d", "strength" (1-3)             camera shake
    flash  "d", "color"                      white / red / black flash
    frame  "d", "region"                     a Short shows the region full-screen
    transition "style", "d"                  how the cut into the segment starting at t looks
                                             (motion.TRANSITIONS: flash, fade, zoom, whip)

A clip can have two edits: the long one (16:9, table `edits`, edit.mp4) and a Short (9:16,
table `shorts`, short.mp4) with its own cut, effects and subtitles plus a layout (see
edit.layout). Functions take kind="long" | "short".

This module is the front door: it re-exports the edit service, whose code lives in
    clipref.py    ClipRef, EditError, file names, the stored row and its small readers
    validate.py   clean_segments, clean_effects, visuals, short_visuals, split_at
    substate.py   subtitles and transcript state (current_subs, auto_cues, subtitles)
    editstore.py  load, save, current_layout, render_state
    rendering.py  render_moment
"""

from chatwatcher.edit.clipref import (  # noqa: F401
    EDIT_FILE,
    KINDS,
    MIN_SEGMENT_S,
    SHORT_FILE,
    SHORT_SRT,
    SRT_FILE,
    STAGE,
    TABLE,
    WAVEFORM_FILE,
    ClipRef,
    EditError,
    Segment,
    check_kind,
    clip_ref,
    effects,
    effects_to_clip,
    latest_analysis,
    meta,
    size,
    stored,
    to_clip,
    whole,
    words,
)
from chatwatcher.edit.editstore import (  # noqa: F401
    current_layout,
    load,
    render_state,
    save,
)
from chatwatcher.edit.rendering import (  # noqa: F401
    render_moment,
)
from chatwatcher.edit.substate import (  # noqa: F401
    auto_cues,
    current_subs,
    sub_default,
    subs_rendered,
    subtitles,
    transcript_lines,
    transcript_state,
)
from chatwatcher.edit.validate import (  # noqa: F401
    DUCK_RANGE,
    GAIN_RANGE,
    MAX_EFFECTS,
    MAX_SEGMENTS,
    clean_effects,
    clean_segments,
    short_regions,
    short_visuals,
    split_at,
    used_regions,
    visuals,
)
