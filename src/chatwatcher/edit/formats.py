"""Named formats: the clip-channel templates that add value beyond the cut (the eurobeat
racing clip, the tuff edit, hopecore, the ironic YOU DIED ending…). A format is a brief for
the AI edit: what it's for, what it's made of (music mood, cards, word art, time effects,
hushes, the ending) and which library items fit it. You pick one next to ✨ AI edit (or
"Claude picks", or none), and it goes into the AI edit's prompt and Direct's follow-ups.
Claude answers with the format it used ("format" in the answer)."""

from __future__ import annotations

import re
from typing import Any

AUTO = "auto"
NONE = "none"
# for: when it fits (shown in the picker and to Claude); moods: music moods (edit.music.MOODS)
# it wants; sounds / overlays: what to look for in the libraries (ids, names, descriptions);
# brief: how to make it, in the answer's own fields.
FORMATS: dict[str, dict[str, Any]] = {
    "eurobeat": {
        "label": "Eurobeat", "for": "racing, drifting, a chase, anything fast in a car or on a track",
        "moods": ["eurobeat"], "sounds": [r"tyre|tire|skid|drift|engine|whoosh"], "overlays": [r"fast|speed|dust|dash"],
        "brief": "A eurobeat track under the whole thing (lead all) at full volume (-2 to 0 dB), its drop on the "
                 "overtake, the drift or the crash. Shots change on the beat; a ramp (speed 0.3) slams into "
                 "the near miss or the crash; a white flash on the drop. Captions in the loud font on the "
                 "hype lines; word art fire on the one hype word. End hard on a beat (no fade).",
    },
    "tuff": {
        "label": "Tuff edit", "for": "a flex, a clutch play, a cold line, a comeback, her being unbothered",
        "moods": ["phonk", "hype"], "sounds": [r"boom|bass|impact|whoosh|hit"], "overlays": [r"shock|impact|dust"],
        "brief": "The setup plays plain, then silence: a hush (silence, 0.4-1 s) ending exactly on the drop, "
                 "and the phonk track comes in at its drop (lead 0) on the cold line or the clutch moment. "
                 "On the drop: a ramp into slow motion (speed 0.3, sound mute or deep), a shake, word art "
                 "box or fire on the cold word. Before it, 1-3 black hushes of 0.15-0.3 s on the beats "
                 "(the stutter). End on a freeze (1.5-2.5 s, zoom 1.3 on face, black and white): the stare.",
    },
    "hopecore": {
        "label": "Hopecore", "for": "a sweet, wholesome or accidentally profound moment; a small win; kindness",
        "moods": ["hopecore"], "sounds": [r"ding|chime|sparkle|twinkle|heart"], "overlays": [r"heart|sparkle|glow"],
        "brief": "A hopecore track that lifts (lead 2-6 s), its drop on the wholesome line or the smile, "
                 "dipping under her words. Slow motion (0.5-0.6, sound tempo) on the moment itself. Word art "
                 "glow on the warm word. End on a poster card (2.5-3 s): the line as a life lesson, 5-8 "
                 "words, the last line in colour (\"YOU DON'T NEED TO EXCEL AT ANYTHING\"). No harsh "
                 "sounds, no shakes.",
    },
    "dreamcore": {
        "label": "Dreamcore", "for": "a surreal, eerie or nonsensical moment; a glitch; a liminal place in a game",
        "moods": ["dreamcore", "chill"], "sounds": [r"echo|reverb|chime|wind|static"], "overlays": [r"glow|cloud|smoke"],
        "brief": "A dreamy track as a soft bed (lead all, -10 dB). Slow motion (0.5-0.75) on the strange "
                 "part; a white flash or a fade between beats; captions that wave; word art glow on the odd "
                 "word. A poster card with an uncanny line (\"YOU HAVE BEEN HERE BEFORE\") or a freeze in "
                 "black and white at the end. Few sounds; nothing loud.",
    },
    "demotivational": {
        "label": "Demotivational", "for": "a fail she takes badly, giving up, a bleak or self-defeating line",
        "moods": ["sad"], "sounds": [r"sad|violin|trombone|womp|fail"], "overlays": [],
        "brief": "Play the attempt and the fail straight. After it, a sad track comes in at its drop (lead "
                 "0), slow motion (0.4-0.5, sound deep) on the fail, then a poster card (2.5-3.5 s) with "
                 "the bleak lesson as advice (\"GIVE UP. IT'S EASIER.\"), last line in colour. Word art box "
                 "on the word that hurts. No jokey sounds after the fail.",
    },
    "died": {
        "label": "Ironic death (YOU DIED)", "for": "she says she's fine, safe, or that it's not her time, then dies, "
                                                   "crashes or fails right after",
        "moods": [], "sounds": [r"died|death|souls"], "overlays": [r"died"],
        "brief": "Keep her confident line clear (that's the setup). On the death, crash or fail: a hush "
                 "(silence + black, 0.2-0.4 s) on the impact, then the died card (YOU DIED, 3-4 s, or "
                 "WASTED / MISSION FAILED to fit the game) with its sound (a died sound or overlay if the "
                 "library has one). END THE EDIT ON THE CARD: keep 3-4 s of picture after the fail for the "
                 "card to show over, then the edit ends (no reaction after it unless it's short and better "
                 "than the card). No music, or none until the card.",
    },
    "sitcom": {
        "label": "Sitcom (time cards)", "for": "a story that jumps in time, a long wait, a plan that goes wrong in "
                                               "steps, two things happening at once",
        "moods": ["funny"], "sounds": [r"laugh|boing|record|scratch|narrat"],
        "overlays": [r"later|meanwhile|eternity|just_then|hours|minutes|am\b|pm\b"],
        "brief": "Cut the story into its beats and put a time card between them: an opaque time-card "
                 "overlay at fill (\"2 HOURS LATER\", \"MEANWHILE\") from the overlay library, or a flat "
                 "card when no overlay says it. A freeze (1-1.5 s, zoom 1.2) with a record-scratch sound "
                 "before the twist. A funny bed or none; deadpan captions.",
    },
}


def clean(key: Any) -> str:
    """A format key, "auto", or "" (none)."""
    key = str(key or "").strip().lower()
    return key if key in FORMATS or key == AUTO else ""


def _match(patterns: list[str], items: list[dict[str, Any]], *fields: str) -> list[str]:
    if not patterns:
        return []
    rx = re.compile("|".join(patterns), re.I)
    return [x["id"] for x in items if any(rx.search(str(x.get(f) or "")) for f in fields)]


def fits(key: str, music: list[dict[str, Any]], sounds: list[dict[str, Any]],
         overlays: list[dict[str, Any]]) -> dict[str, list[str]]:
    """The library items that fit a format: music by mood, sounds and overlays by words."""
    f = FORMATS[key]
    return {"music": [m["id"] for m in music if (m.get("mood") or "") in f["moods"]],
            "sounds": _match(f["sounds"], sounds, "id", "name", "description")[:8],
            "overlays": _match(f["overlays"], overlays, "id", "name", "description")[:12]}


def _one(key: str, music: list[dict[str, Any]], sounds: list[dict[str, Any]], overlays: list[dict[str, Any]]) -> str:
    f, got = FORMATS[key], fits(key, music, sounds, overlays)
    lines = [f"{key} ({f['label']}): for {f['for']}.", f"How: {f['brief']}"]
    if f["moods"]:
        lines.append("Music for it: " + (", ".join(got["music"]) if got["music"] else
                                         f"none in the library with mood {' / '.join(f['moods'])}: carry it without "
                                         "music (the hushes, time effects and cards still make it)") + ".")
    if got["sounds"]:
        lines.append("Sounds that fit: " + ", ".join(got["sounds"]) + ".")
    if got["overlays"]:
        lines.append("Overlays that fit: " + ", ".join(got["overlays"]) + ".")
    return "\n".join(lines)


def brief(key: str, music: list[dict[str, Any]], sounds: list[dict[str, Any]], overlays: list[dict[str, Any]]) -> str:
    """The prompt section for the format the director picked ("" for none)."""
    key = clean(key)
    if not key:
        return ""
    if key == AUTO:
        return ("\n\nFORMAT: pick the one clip-channel format below that this clip fits, the way a human "
                "editor adds value beyond the cut, and make the edit in it; or none when nothing fits "
                "well (a plain, tight cut is better than a forced format). Say which in format.\n\n"
                + "\n\n".join(_one(k, music, sounds, overlays) for k in FORMATS))
    return (f"\n\nFORMAT: the director wants this edit in the {FORMATS[key]['label']} format. Make it so "
            "(it wins over the general guidance on how much music, cards and time effects to use; the "
            "director's own words still win over it), and say format = " + key + ".\n\n"
            + _one(key, music, sounds, overlays))


def info() -> list[dict[str, str]]:
    """The picker's options."""
    return [{"key": k, "label": f["label"], "for": f["for"]} for k, f in FORMATS.items()]
