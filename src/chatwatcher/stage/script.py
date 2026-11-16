"""What a 3D scene can say, and checking one (from Claude, or saved by the page).

A scene, times in seconds from the start of the cut it re-enacts:

    shots  [{t, shot, yaw, push, by}]   the camera from t on. yaw: degrees around her
           (+ = from her left side), push: how far it creeps in while the shot is up (0..0.4)
    poses  [{t, pose, ease, by}]        her body; ease = seconds to move into it (she snaps
           in and overshoots a little, like a cartoon)
    actions [{t, action, size, by}]     cartoon gestures on top of the pose (a take, a hop, a
           slump…): the exaggeration is in her body, never her mouth. size 0.5..1.5
    faces  [{t, face, ease, by}]        her expression (laughing and talking are added on
           top from the voice track: stage.track)
    lanes  {name: [{t, v, ease, by}]}   the set's own cues (the cabin's cloud)
    note   one line: the idea

by = "claude" or "you". The names mean something only in the page (static/stage/figure.js,
camera.js, sets/); these lists are what Claude is offered and what is kept."""

from __future__ import annotations

from typing import Any

POSES = {
    "rest": "sitting relaxed, hands down",
    "explain": "one hand up, palm open, explaining",
    "look": "turned towards the set's feature (the screen, the window), watching it",
    "shock": "both hands up by her face, leaning back",
    "phone": "one hand raised in front of her, as if holding up a phone",
    "laugh": "hunched forward, hands in, laughing",
    "sheepish": "hand to her cheek, head tilted, embarrassed",
    "point": "pointing at the set's feature",
    "think": "hand to her chin, head tilted",
    "cheer": "both fists up, excited",
}
FACES = {
    "neutral": "plain",
    "smile": "happy smile",
    "smug": "smug grin, half-closed eyes",
    "worried": "worried brows, mouth down",
    "sad": "sad, teary",
    "angry": "angry brows, pout",
    "shock": "wide eyes, small pupils, pale",
    "excited": "sparkling eyes, grin",
    "laugh": "eyes shut in a laugh",
    "sheepish": "embarrassed: blush, sweat drop, awkward smile",
}
ACTIONS = {
    "take": "the big cartoon take: winds up, then jumps back off the seat, arms flung up, eyes popping, camera shakes",
    "jolt": "a small startle: hops, shoulders up",
    "slump": "deflates: sinks down, head hangs, pale and sweating",
    "bounce": "bounces in her seat three times, fists pumping, sparkly eyes",
    "lean_in": "leans right in towards the camera, conspiratorial",
    "recoil": "jerks back from something, hands up",
    "shake_head": "a fast no-no head shake",
    "nod": "three quick nods",
    "laugh_fit": "rocks forward and back laughing, head thrown back",
    "facepalm": "hand to her face, head drops",
    "shrug": "shoulders up, hands out",
    "tremble": "shivers all over, pale",
    "whip_look": "whips her head round to the set's feature and holds",
}
# (how long it lasts, when it hits) in seconds, as figure.js ACTIONS plays them
ACTION_TIMES = {"take": (1.3, 0.25), "jolt": (0.7, 0.14), "slump": (2.2, 0.5), "bounce": (1.3, 0.2),
                "lean_in": (2.0, 0.35), "recoil": (1.0, 0.1), "shake_head": (0.95, 0.1), "nod": (0.95, 0.12),
                "laugh_fit": (2.4, 0.2), "facepalm": (1.9, 0.3), "shrug": (1.4, 0.3), "tremble": (1.3, 0.05),
                "whip_look": (1.3, 0.14)}
SHOTS = {
    "wide": "her whole body and the set",
    "medium": "waist up",
    "close": "chest up",
    "face": "her face fills the frame",
}
SETS: dict[str, dict[str, Any]] = {
    "room": {
        "label": "Room with a screen",
        "about": "Her room at night. She sits in a chair; a big screen at her left side (the right of the "
                 "frame) plays the stream footage of this moment, so she can react to what is on it.",
        "feature": "the big screen",
        "shots": {"over": "over her shoulder: her and the screen together",
                  "screen": "the screen fills the frame: the stream footage itself"},
        "without_her": ["screen"],
        "lanes": {},
    },
    "cabin": {
        "label": "Plane cabin at night",
        "about": "A night flight. She sits in a window seat, the window at her left side (the right of the "
                 "frame); through it, city lights far below.",
        "feature": "the window",
        "shots": {"over": "from the aisle past her head, to the window", "pov": "out through the window, down at the city"},
        "without_her": ["pov"],
        "lanes": {"cloud": {"about": "-1 = the cloud is still ahead (city lights show), 0 = inside it (all dark "
                                     "outside), 1 = it has slid away behind (lights back). Moves at the cue's ease.",
                            "min": -1.0, "max": 1.0, "start": -1.0}},
    },
}
MAX_CUES = 200
DEFAULTS = {"shots": {"shot": "medium"}, "poses": {"pose": "rest"}, "faces": {"face": "neutral"}}


def vocab() -> dict[str, Any]:
    return {"poses": POSES, "faces": FACES, "actions": ACTIONS, "shots": SHOTS, "sets": SETS}


def _num(v: Any, lo: float, hi: float, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if f != f else max(lo, min(hi, f))


def validate(scene: Any, duration: float, set_name: str, *, by: str = "you") -> tuple[dict[str, Any], list[str]]:
    """A scene as the page and the exporter can trust it, and what was dropped or fixed.
    by: who a cue without one is from."""
    warnings: list[str] = []
    s = scene if isinstance(scene, dict) else {}
    st = SETS.get(set_name, SETS["room"])
    names = {"shots": set(SHOTS) | set(st["shots"]), "poses": set(POSES), "faces": set(FACES)}
    key = {"shots": "shot", "poses": "pose", "faces": "face"}
    out: dict[str, Any] = {}
    for lane, field in key.items():
        cues = []
        for c in (s.get(lane) or [])[:MAX_CUES]:
            if not isinstance(c, dict):
                continue
            name = str(c.get(field, ""))
            if name not in names[lane]:
                warnings.append(f"{lane}: no {field} called {name!r} (skipped)")
                continue
            cue = {"t": round(_num(c.get("t"), 0, duration, 0), 3), field: name,
                   "by": c.get("by") if c.get("by") in ("you", "claude") else by}
            if lane == "shots":
                cue["yaw"] = round(_num(c.get("yaw"), -75, 75, 0), 1)
                cue["push"] = round(_num(c.get("push"), 0, 0.4, 0.05), 3)
            else:
                cue["ease"] = round(_num(c.get("ease"), 0.05, 6, 0.3), 3)
            cues.append(cue)
        cues.sort(key=lambda c: c["t"])
        if not cues or cues[0]["t"] > 0:
            cues.insert(0, {"t": 0.0, **DEFAULTS[lane], "by": by} | ({"yaw": 0.0, "push": 0.05} if lane == "shots" else {"ease": 0.3}))
        out[lane] = cues
    lanes: dict[str, list[dict[str, Any]]] = {}
    raw_lanes = s.get("lanes") if isinstance(s.get("lanes"), dict) else {}
    for name, spec in st["lanes"].items():
        cues = []
        for c in (raw_lanes.get(name) or [])[:MAX_CUES]:
            if isinstance(c, dict):
                cues.append({"t": round(_num(c.get("t"), 0, duration, 0), 3),
                             "v": round(_num(c.get("v"), spec["min"], spec["max"], spec["start"]), 3),
                             "ease": round(_num(c.get("ease"), 0.05, 10, 0.5), 3),
                             "by": c.get("by") if c.get("by") in ("you", "claude") else by})
        cues.sort(key=lambda c: c["t"])
        if not cues or cues[0]["t"] > 0:
            cues.insert(0, {"t": 0.0, "v": spec["start"], "ease": 0.5, "by": by})
        lanes[name] = cues
    for name in raw_lanes:
        if name not in st["lanes"]:
            warnings.append(f"lanes: the {set_name} set has no {name!r} cue (skipped)")
    out["lanes"] = lanes
    actions = []
    for c in (s.get("actions") or [])[:MAX_CUES]:
        if not isinstance(c, dict):
            continue
        if c.get("action") not in ACTIONS:
            warnings.append(f"actions: no action called {c.get('action')!r} (skipped)")
            continue
        actions.append({"t": round(_num(c.get("t"), 0, duration, 0), 3), "action": c["action"],
                        "size": round(_num(c.get("size"), 0.3, 1.5, 1.0), 2),
                        "by": c.get("by") if c.get("by") in ("you", "claude") else by})
    out["actions"] = sorted(actions, key=lambda c: c["t"])
    out["shots"] = show_the_hits(out["shots"], out["actions"], st.get("without_her", []), duration)
    out["note"] = str(s.get("note") or "")[:500]
    return out, warnings


def show_the_hits(shots: list[dict[str, Any]], actions: list[dict[str, Any]], without: list[str],
                  duration: float) -> list[dict[str, Any]]:
    """A gesture that hits while the camera is on a shot she isn't in (the window view, the
    TV) gets a cut to her just before the hit: a gag nobody sees is wasted."""
    shots = list(shots)
    for a in actions:
        hit = a["t"] + ACTION_TIMES[a["action"]][1]
        cur = max((s for s in shots if s["t"] <= hit), key=lambda s: s["t"], default=None)
        if cur is None or cur["shot"] not in without:
            continue
        cut = round(max(cur["t"] + 0.3, hit - 0.25), 3)
        if cut >= hit:
            cut = round(cur["t"], 3)
            shots = [s for s in shots if s is not cur]
        size = "medium" if a["action"] in ("take", "bounce", "laugh_fit", "shrug", "facepalm", "slump") else "close"
        shots.append({"t": min(cut, duration), "shot": size, "by": a.get("by", "claude"), "yaw": 0.0, "push": 0.05})
    return sorted(shots, key=lambda s: s["t"])


def starter(set_name: str) -> dict[str, Any]:
    """A scene that just sits there: what a new stage starts with before Claude writes one."""
    return validate({}, 0.0, set_name)[0]
