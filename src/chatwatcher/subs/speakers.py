"""Who is talking: speaker detection on a clip, so collab subtitles show each voice in its
own colour, stacked when two talk at once.

sherpa-onnx (CPU, ONNX) runs pyannote's segmentation 3.0, which finds speech and where two
voices overlap, and a WeSpeaker voice embedding that groups the stretches by voice. The
voices are numbered by how much they talk (0 = the most, usually the streamer). Each word
in words.json gets the voice that speaks over most of it as a fifth item, and words.json
gets "voices": how many there are.

Detection is off by default (transcribe.speakers): on real clips it split one person
shouting from the same person talking calmly, and music into several voices. Instead you
add the clip's people in the editor (the `voices` table: number → name and colour) and
say who says each line; the cue then holds that number as "speaker". With detection on,
detected voices start as those numbers. Two people with one name are one person.

Each VTuber's colour is master data (the `vtubers` table, the VTubers page): a named
person always shows in her colour there, on every clip. Naming someone new in a clip adds
her to the list; changing a named person's colour in a clip changes it in the list.

The models (~33 MB) are downloaded into <data_dir>/models/speakers the first time."""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import tarfile
import threading
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Callable

RATE = 16000
MODEL_DIR = "speakers"
SEG_DIR = "sherpa-onnx-pyannote-segmentation-3-0"
SEG_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/"
           "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2")
EMB_FILE = "wespeaker_en_voxceleb_resnet34_LM.onnx"
EMB_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/{EMB_FILE}"
THRESHOLD = 0.6          # voice grouping: higher = fewer voices (0.7 merged a two-person collab into one)
MIN_SHARE = 0.12         # a voice with fewer of the words than this (or under MIN_WORDS) joins its neighbours:
MIN_WORDS = 4            # music, game voices and shouting make small extra "voices"
NEAR_S = 1.0             # a word in no voice's stretch takes a voice this close
MAX_VOICES = 8
WHITE = "FFFFFF"
# Readable over video with a black outline; lines with nobody picked stay white.
PALETTE = ["FF8FC8", "7FD3FF", "A6F06E", "FFB35C", "C7A3FF", "6EF0D2", "FF7A7A", WHITE]
_HEX = re.compile(r"^[0-9A-Fa-f]{6}$")
_HEX3 = re.compile(r"^[0-9A-Fa-f]{3}$")


class SpeakerError(Exception):
    pass


def available() -> bool:
    return importlib.util.find_spec("sherpa_onnx") is not None


# ── detecting voices ─────────────────────────────────────────────────────────

def download(models_dir: Path, say: Callable[[str], None] | None = None) -> tuple[Path, Path]:
    """(segmentation model, embedding model), downloaded first if missing."""
    d = Path(models_dir) / MODEL_DIR
    seg, emb = d / SEG_DIR / "model.onnx", d / EMB_FILE
    if seg.is_file() and emb.is_file():
        return seg, emb
    (say or (lambda _m: None))("Downloading the speaker models (33 MB, only this once)…")
    d.mkdir(parents=True, exist_ok=True)
    try:
        if not seg.is_file():
            tar = d / "seg.tar.bz2"
            urllib.request.urlretrieve(SEG_URL, tar)
            with tarfile.open(tar) as t:
                t.extractall(d, filter="data")
            tar.unlink(missing_ok=True)
        if not emb.is_file():
            part = emb.with_suffix(".part")
            urllib.request.urlretrieve(EMB_URL, part)
            part.replace(emb)
    except (OSError, tarfile.TarError) as e:
        raise SpeakerError(f"couldn't download the speaker models: {e}") from e
    return seg, emb


_lock = threading.Lock()
_engines: dict[tuple[str, float, int], Any] = {}


def _engine(models_dir: Path, threshold: float, threads: int) -> Any:
    import sherpa_onnx as so
    seg, emb = download(models_dir)
    key = (str(models_dir), threshold, threads)
    with _lock:
        if key not in _engines:
            n = threads or 4
            cfg = so.OfflineSpeakerDiarizationConfig(
                segmentation=so.OfflineSpeakerSegmentationModelConfig(
                    pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(seg)), num_threads=n),
                embedding=so.SpeakerEmbeddingExtractorConfig(model=str(emb), num_threads=n),
                clustering=so.FastClusteringConfig(num_clusters=-1, threshold=threshold),
                min_duration_on=0.2, min_duration_off=0.3)
            if not cfg.validate():
                raise SpeakerError("the speaker models don't load")
            _engines[key] = so.OfflineSpeakerDiarization(cfg)
        return _engines[key]


def detect(audio: Any, models_dir: Path, threshold: float = THRESHOLD, threads: int = 0) -> list[tuple[float, float, int]]:
    """[(start, end, voice)] in clip seconds; voices numbered by talking time, most first.
    Stretches of two voices may overlap."""
    import numpy as np
    engine = _engine(models_dir, threshold, threads)
    with _lock:                                   # one at a time: it uses every core
        segs = engine.process(np.asarray(audio, dtype=np.float32)).sort_by_start_time()
    raw = [(float(s.start), float(s.end), int(s.speaker)) for s in segs]
    talk = Counter()
    for a, b, k in raw:
        talk[k] += b - a
    order = {k: i for i, (k, _) in enumerate(talk.most_common(MAX_VOICES))}
    last = len(order) - 1
    return [(round(a, 3), round(b, 3), order.get(k, last)) for a, b, k in raw]


def label(words: list[tuple[float, float]], turns: list[tuple[float, float, int]]) -> list[int | None]:
    """The voice of each word: the one speaking over most of it (with two at once, the one
    the word before had: a speaker usually goes on), else the nearest one. A lone word
    between two of another voice's is taken to be theirs (a blip, not a reply)."""
    out: list[int | None] = []
    for s, e in words:
        over: dict[int, float] = {}
        for a, b, k in turns:
            o = min(b, e) - max(a, s)
            if o > 0:
                over[k] = over.get(k, 0.0) + o
        if over:
            best = max(over.values())
            tied = [k for k, o in over.items() if o >= best * 0.8]
            prev = out[-1] if out else None
            out.append(prev if prev in tied else max(tied, key=lambda k: over[k]))
            continue
        near = min(turns, key=lambda t: max(t[0] - e, s - t[1], 0.0), default=None)
        if near is not None and max(near[0] - e, s - near[1], 0.0) <= NEAR_S:
            out.append(near[2])
        else:
            out.append(out[-1] if out else None)
    for i in range(1, len(out) - 1):
        a, b = words[i]
        if out[i - 1] == out[i + 1] != out[i] and b - a < 0.5:
            out[i] = out[i - 1]
    return out


def fold(voices: list[int | None]) -> list[int | None]:
    """Voices that say few of the words join the voice of the nearest word that isn't
    theirs; the rest are renumbered by how many words they say (0 = the most)."""
    said = Counter(v for v in voices if v is not None)
    total = sum(said.values())
    keep = {v for v, n in said.items() if n >= MIN_WORDS and n >= MIN_SHARE * total}
    if not keep:
        keep = {said.most_common(1)[0][0]} if said else set()
    out = list(voices)
    for i, v in enumerate(out):
        if v is None or v in keep:
            continue
        near = next((out[j] for d in range(1, len(out)) for j in (i - d, i + d)
                     if 0 <= j < len(out) and voices[j] in keep), None)
        out[i] = near
    order = {v: k for k, (v, _) in enumerate(Counter(v for v in out if v is not None).most_common())}
    return [order.get(v) if v is not None else None for v in out]


def assign(data: dict[str, Any], turns: list[tuple[float, float, int]]) -> dict[str, Any]:
    """words.json's contents with each word's voice as its fifth item (None when nobody's
    stretch is near it) and "voices": how many voices say the words (after fold)."""
    flat = [(float(w[0]), float(w[1])) for line in data.get("lines") or [] for w in line.get("words") or []]
    labels = fold(label(flat, turns))
    voices = iter(labels)
    lines = []
    for line in data.get("lines") or []:
        lines.append(line | {"words": [[*w[:4], next(voices)] for w in line.get("words") or []]})
    count = len({v for v in labels if v is not None})
    return data | {"lines": lines, "voices": count, "turns": [list(t) for t in turns]}


def write(folder: Path, data: dict[str, Any], audio: Any, models_dir: Path, threshold: float = THRESHOLD,
          threads: int = 0, say: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Detect the voices in `audio`, mark them in `data` and write words.json."""
    from chatwatcher.subs.transcribe import WORDS_FILE
    download(models_dir, say)
    (say or (lambda _m: None))("Telling the voices apart…")
    out = assign(data, detect(audio, models_dir, threshold, threads))
    tmp = folder / (WORDS_FILE + ".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    tmp.replace(folder / WORDS_FILE)
    return out


# ── names and colours (per clip; colours remembered per person per channel) ──

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
