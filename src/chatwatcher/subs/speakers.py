"""Who is talking in a clip, so collab subtitles show each voice in its own colour, stacked
when two talk at once.

You add the clip's people in the editor (the `voices` table: number → name and colour)
and say who says each line; the cue then holds that number as "speaker". Two people with
one name are one person.

Each VTuber's colour is master data (the `vtubers` table, the VTubers page): a named
person always shows in her colour there, on every clip. Naming someone new in a clip adds
her to the list; changing a named person's colour in a clip changes it in the list."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

MAX_VOICES = 8
WHITE = "FFFFFF"
# Readable over video with a black outline; lines with nobody picked stay white.
PALETTE = ["FF8FC8", "7FD3FF", "A6F06E", "FFB35C", "C7A3FF", "6EF0D2", "FF7A7A", WHITE]
_HEX = re.compile(r"^[0-9A-Fa-f]{6}$")
_HEX3 = re.compile(r"^[0-9A-Fa-f]{3}$")


class SpeakerError(Exception):
    pass


def default_color(k: int) -> str:
    return PALETTE[k % len(PALETTE)]


def clean(raw: Any) -> dict[str, dict[str, str]]:
    """The clip's people: {"0": {"name": "Gigi", "color": "FF8FC8"}, ...}, keyed by the
    number a line's "speaker" holds (0-7). People with the same name get the first one's
    colour (they're one person). Entries with neither a name nor a colour are dropped."""
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, dict[str, str]] = {}
    seen: dict[str, str] = {}
    for k in range(MAX_VOICES):
        v = raw.get(str(k))
        if not isinstance(v, dict):
            continue
        name = " ".join(str(v.get("name") or "").replace("*", "").split())[:24]
        rgb = hex_color(v.get("color"))
        if not name and rgb is None:
            continue
        color = rgb or default_color(k)
        if name and name.lower() in seen:
            color = seen[name.lower()]
        elif name:
            seen[name.lower()] = color
        out[str(k)] = {"name": name, "color": color}
    return out


def get(conn: sqlite3.Connection, moment_pk: int) -> dict[str, dict[str, str]]:
    """The clip's people, each named one in her colour from the VTubers list."""
    r = conn.execute("SELECT voices_json FROM voices WHERE moment_id = ?", (moment_pk,)).fetchone()
    try:
        voices = clean(json.loads(r[0]) if r else {})
    except json.JSONDecodeError:
        return {}
    master = vtubers(conn)
    out = {}
    for k, v in voices.items():
        hit = find(master, v["name"]) if v["name"] else None
        out[k] = v | ({"name": hit["name"], "color": hit["color"]} if hit else {})
        out[k]["text"] = readable(out[k]["color"])
    return out


def put(conn: sqlite3.Connection, moment_pk: int, raw: Any, recolor: int | None = None) -> dict[str, dict[str, str]]:
    """Store the clip's people. A name that means someone on the VTubers list is stored as
    her full name and shows in her colour; a new name joins the list with its colour.
    recolor: the person whose colour you just changed: that changes it on the list too."""
    voices = clean(raw)
    master = vtubers(conn)
    for k, v in voices.items():
        if not v["name"]:
            continue
        hit = find(master, v["name"])
        if hit is None or str(recolor) == k:
            save_vtuber(conn, hit["name"] if hit else v["name"], v["color"])
        if hit:
            v["name"] = hit["name"]
    conn.execute("""INSERT INTO voices (moment_id, voices_json) VALUES (?, ?)
                    ON CONFLICT (moment_id) DO UPDATE SET voices_json = excluded.voices_json,
                        updated_at = datetime('now')""", (moment_pk, json.dumps(voices, ensure_ascii=False)))
    return get(conn, moment_pk)


# ── the VTubers list (master data) ───────────────────────────────────────────

TEXT_LUMINANCE = 0.3     # caption text darker than this is lightened (same hue) to be readable


def _luminance(rgb: str) -> float:
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(rgb[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def readable(rgb: str) -> str:
    """The colour for caption text: as it is, or, when it's too dark to read over video
    with a black outline (half of the official colours are), the same hue made lighter."""
    import colorsys
    if _luminance(rgb) >= TEXT_LUMINANCE:
        return rgb
    h, l, s = colorsys.rgb_to_hls(*(int(rgb[i:i + 2], 16) / 255 for i in (0, 2, 4)))
    s = max(s, 0.35) if s > 0.05 else s
    for step in range(1, 40):
        l2 = min(0.92, l + step * 0.02)
        out = "".join(f"{round(c * 255):02X}" for c in colorsys.hls_to_rgb(h, l2, s))
        if _luminance(out) >= TEXT_LUMINANCE or l2 >= 0.92:
            return out
    return rgb


_COLUMNS = "key, name, color, japanese_name, accent, status, profile_url"


def vtubers(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    """{name in lower case: {"name", "color", "text" (readable), "japanese_name", "accent",
    "status", "profile_url"}}, by name."""
    out = {}
    for r in conn.execute(f"SELECT {_COLUMNS} FROM vtubers ORDER BY name COLLATE NOCASE"):
        out[r[0]] = {"name": r[1], "color": r[2], "text": readable(r[2]), "japanese_name": r[3], "accent": r[4],
                     "status": r[5], "profile_url": r[6]}
    return out


def find(master: dict[str, dict[str, str]], name: str) -> dict[str, str] | None:
    """The VTuber a name means: her full name, her Japanese name, or one word of her name
    when only one VTuber has it ("Gigi" → Gigi Murin; "Kyoko" → Hyakuto Kyoko)."""
    key = " ".join(str(name or "").split()).lower()
    if not key:
        return None
    if key in master:
        return master[key]
    jp = [v for v in master.values() if v.get("japanese_name") and v["japanese_name"].replace(" ", "") == key.replace(" ", "")]
    if len(jp) == 1:
        return jp[0]
    if " " not in key:
        hits = [v for k, v in master.items() if key in re.split(r"[\s'’+()-]+", k)]
        if len(hits) == 1:
            return hits[0]
    return None


def hex_color(raw: Any) -> str | None:
    """"#ff8fc8", "FF8FC8" or "#f8c" → "FF8FC8"; None if it isn't one."""
    c = str(raw or "").strip().lstrip("#")
    if _HEX3.match(c):
        c = "".join(ch * 2 for ch in c)
    return c.upper() if _HEX.match(c) else None


def save_vtuber(conn: sqlite3.Connection, name: Any, color: Any, old: Any = None, **extra: Any) -> dict[str, str]:
    """Add or change one (old: her name before a rename; extra: japanese_name, accent,
    status, profile_url, kept as they are when not given). Raises SpeakerError."""
    name = " ".join(str(name or "").replace("*", "").split())[:40]
    rgb = hex_color(color)
    if not name:
        raise SpeakerError("A VTuber needs a name.")
    if rgb is None:
        raise SpeakerError(f"“{color}” isn't a colour: use hex like #FF8FC8.")
    more = {k: " ".join(str(v or "").split())[:300] for k, v in extra.items()
            if k in ("japanese_name", "accent", "status", "profile_url") and v is not None}
    if "accent" in more:
        more["accent"] = hex_color(more["accent"]) or ""
    old_key = " ".join(str(old or "").split()).lower()
    if old_key and old_key != name.lower():
        kept = conn.execute("SELECT japanese_name, accent, status, profile_url FROM vtubers WHERE key = ?",
                            (old_key,)).fetchone()
        if kept:
            more = dict(zip(("japanese_name", "accent", "status", "profile_url"), kept)) | more
        conn.execute("DELETE FROM vtubers WHERE key = ?", (old_key,))
    cols = ["key", "name", "color", *more]
    sets = ", ".join(f"{c} = excluded.{c}" for c in cols[1:])
    conn.execute(f"""INSERT INTO vtubers ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})
                     ON CONFLICT (key) DO UPDATE SET {sets}, updated_at = datetime('now')""",
                 (name.lower(), name, rgb, *more.values()))
    return {"name": name, "color": rgb}


def delete_vtuber(conn: sqlite3.Connection, name: Any) -> None:
    conn.execute("DELETE FROM vtubers WHERE key = ?", (" ".join(str(name or "").split()).lower(),))


def import_csv(conn: sqlite3.Connection, text: str) -> tuple[int, int, list[str]]:
    """VTubers from a CSV with a header: name and a colour (main_hex or color) are needed;
    japanese_name, accent_hex, status, profile_url are kept when there. Existing ones are
    updated. Returns (added, updated, problems)."""
    import csv
    import io
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    fields = {(f or "").strip().lower(): f for f in reader.fieldnames or []}
    if "name" not in fields or not ({"main_hex", "color", "colour", "hex"} & set(fields)):
        raise SpeakerError("The CSV needs a header with name and main_hex (or color).")
    col = next(fields[c] for c in ("main_hex", "color", "colour", "hex") if c in fields)
    have = set(vtubers(conn))
    added = updated = 0
    problems: list[str] = []
    for n, row in enumerate(reader, 2):
        name = (row.get(fields["name"]) or "").strip()
        if not name:
            continue
        get = lambda k: (row.get(fields[k]) or "").strip() if k in fields else None     # noqa: E731
        try:
            save_vtuber(conn, name, row.get(col), japanese_name=get("japanese_name"), accent=get("accent_hex"),
                        status=get("status"), profile_url=get("profile_url"))
        except SpeakerError as e:
            problems.append(f"line {n}: {e}")
            continue
        if name.lower() in have:
            updated += 1
        else:
            added += 1
            have.add(name.lower())
    return added, updated, problems


def paint(cues: list[dict[str, Any]], voices: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    """Cues with their person's "color" and "name" (for the render and the .srt)."""
    out = []
    for c in cues:
        k = c.get("speaker")
        if k is None:
            out.append(c)
            continue
        v = voices.get(str(k)) or {"color": default_color(k), "name": ""}
        out.append(c | {"color": v.get("text") or readable(v["color"]), "name": v["name"]})
    return out
