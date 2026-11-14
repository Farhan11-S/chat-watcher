"""✨ AI edit: Claude cuts the clip, picks the hook, plans the camera and places effects,
in one call.

Claude sees the clip as numbered pieces (edit.pieces: lines of talk and quiet stretches,
with exact times from Whisper) and a contact sheet of its frames, and answers with piece
ids, so it chooses and the code does the timing:
    keep        which pieces stay ("p7:1.2" keeps 1.2 s of a quiet piece, "p7!" all of it)
    cold_open   pieces played first as a teaser (the payoff), before the story
    camera      a held shot per stretch: region, how tight, cut or push
    sounds/hits sound effects, shakes and flashes at clip times (snapped to audio hits)
    volume      louder or quieter pieces ("p12-p14 | -12 | feedback screech")
    transitions how a cut into a piece looks ("p14 | whip | jump to the next topic")
    hook_title  the Short's top text; emphasis: words to highlight in the captions
    captions    caption styles on stretches: an effect, a font, a place (edit.captions)

Then hard rules the prompt can't be trusted with: talk starts within half a second, no
quiet longer than QUIET_MAX unless Claude marked it as action, the end comes soon after
the last line, and the picture never holds still longer than IDLE_MAX (a long shot gets
a small re-frame, a long wide stretch a slow push-in), like the clip channels do.

The answer schema has no nested objects: models sometimes slip out of JSON inside them
(see analyze.prompts.nest), so lists are of "a | b | c" strings.

The code is split by what it does; this package re-exports all of it, so
`from chatwatcher.edit import autoedit as ae` and `ae.auto_edit(...)` work as before:
    prompt.py    the system prompt, the answer schema, build_prompt
    plan.py      reading the answer: piece ids, "a | b | c" lists, limits, Plan
    assemble.py  the cut (assemble) and volume changes (apply_volume)
    camera.py    the camera track
    effects.py   sounds, overlays, texts, spotlights, hits, transitions, caption styles
    run.py       material, ask, apply, auto_edit
"""

from chatwatcher.edit.autoedit.assemble import (  # noqa: F401
    AUTO_BEAT,
    BEAT_MAX,
    COLD_OPEN_MAX,
    EDGE_SNAP,
    FIRST_SOUND_S,
    KEEP_PAUSE,
    LONG_PAUSE,
    MIN_JUMP,
    PAD_IN,
    PAD_OUT,
    QUIET_KEEP,
    QUIET_MAX,
    TAIL_MAX,
    TALK_REACH,
    VOLUME_RANGE,
    apply_volume,
    assemble,
    blocks,
)
from chatwatcher.edit.autoedit.camera import (  # noqa: F401
    IDLE_MAX,
    LEVELS,
    MOVES,
    REFRAME,
    SHOT_SNAP,
    WIDE_PUSH,
    WIDE_PUSH_S,
    camera_track,
    layout_effects,
)
from chatwatcher.edit.autoedit.effects import (  # noqa: F401
    CAPTION_LEAD,
    HIT_SNAP_S,
    KINDS,
    SPOT_PAD,
    caption_effects,
    captions,
    cutaway_effects,
    hit_effects,
    layer_effects,
    overlay_effects,
    sound_effects,
    spot_effects,
    spot_target,
    text_effects,
    transition_effects,
)
from chatwatcher.edit.autoedit.plan import (  # noqa: F401
    CENTRE,
    MAX_CAPTIONS,
    MAX_CUTAWAYS,
    MAX_LAYERS,
    MAX_LAYOUTS,
    MAX_OVERLAYS,
    MAX_SPOTS,
    MAX_TEXTS,
    STRENGTHS,
    AutoEditError,
    Plan,
    parse_keep,
    parse_range,
    parts,
)
from chatwatcher.edit.autoedit.prompt import (  # noqa: F401
    SHORT_TARGET,
    SYSTEM,
    TRANSITION_LIST,
    build_prompt,
    inserts_text,
    placed_line,
    schema,
)
from chatwatcher.edit.autoedit.run import (  # noqa: F401
    INTENSITY,
    VISUAL_INTENSITY,
    Material,
    Result,
    apply,
    ask,
    auto_edit,
    material,
)
