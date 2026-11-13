"""The intro and thumbnail of one clip, kept in the `intros` and `thumbs` tables.

Times are stored in stream time (like the editor's), so they stay on the same moment
when the clip is downloaded again with slightly different edges. Until you change the
teasers they're picked fresh from your edit each time (saved as NULL = "auto")."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import subprocess
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from chatwatcher.brand import brand as br
from chatwatcher.brand import canvas as cv
from chatwatcher.brand import cutout as co
from chatwatcher.brand import intro as it
from chatwatcher.brand import layers as L
from chatwatcher.brand import thumb as th
from chatwatcher.brand import thumbai as ta
from chatwatcher.config import Config
from chatwatcher.edit import regions
from chatwatcher.edit import service as es

INTRO_FILE = "intro.mp4"


# ── what the clip's edit looks like now ─────────────────────────────────────

def _context(cfg: Config, conn: sqlite3.Connection, pk: int, clip: es.ClipRef) -> dict[str, Any]:
    from chatwatcher.edit.audio import RATE, analyse
    from chatwatcher.subs import lines
    row = es.stored(conn, pk)
    wf = analyse(clip.path, clip.folder / es.WAVEFORM_FILE)
    a = es.latest_analysis(conn, pk)
    segs = es.to_clip(json.loads(row["segments_json"]), clip) if row else []
    if not segs:
        segs = es.whole(clip)
    cues, _src, style = es.current_subs(cfg, row, clip)
    shift = float((a.get("_meta") or {}).get("zero_s", clip.zero_s)) - clip.zero_s
    beats = [{"start": float(b.get("start", 0)) + shift, "end": float(b.get("end", 0)) + shift, "beat": b.get("beat", "")}
             for b in a.get("storyboard") or []]
    words = es.words(clip)
    return {"segments": segs, "cues": cues, "style": style, "wf": wf, "rate": RATE, "beats": beats,
            "words": [[w0, w1] for w0, w1, _ in lines.words_of(words)] if words else [],
            "lines": es.transcript_lines(clip.folder), "analysis": a}


def _clip_or_none(conn: sqlite3.Connection, pk: int) -> es.ClipRef | None:
    try:
        return es.clip_ref(conn, pk)
    except es.EditError:
        return None


# ── intro ────────────────────────────────────────────────────────────────────

def _intro_row(conn: sqlite3.Connection, pk: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM intros WHERE moment_id = ?", (pk,)).fetchone()


def intro_path(clip: es.ClipRef) -> Path:
    return clip.folder / INTRO_FILE


def teasers(cfg: Config, conn: sqlite3.Connection, pk: int, clip: es.ClipRef,
            ctx: dict[str, Any] | None = None) -> tuple[list[list[Any]], str, list[list[Any]]]:
    """(teasers in clip time, saved | auto, what auto-pick gives now)."""
    ctx = ctx or _context(cfg, conn, pk, clip)
    wf = ctx["wf"]
    auto = it.pick(ctx["segments"], ctx["cues"], ctx["lines"], wf.peaks, ctx["rate"], wf.hits, ctx["beats"], ctx["words"])
    row = _intro_row(conn, pk)
    if row and row["teasers_json"] is not None:
        mine = []
        for s, e, g, style in json.loads(row["teasers_json"]):
            s, e = max(s - clip.zero_s, 0.0), min(e - clip.zero_s, clip.duration_s)
            if e - s >= it.MIN_TEASER_S:
                mine.append([round(s, 3), round(e, 3), g, style])
        return mine, "saved", auto
    return auto, "auto", auto


def spec(cfg: Config, conn: sqlite3.Connection, pk: int, clip: es.ClipRef,
         ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    """Everything the intro is made from; its hash tells whether intro.mp4 is current."""
    ctx = ctx or _context(cfg, conn, pk, clip)
    tz, _source, _auto = teasers(cfg, conn, pk, clip, ctx)
    b = br.get(conn)
    from chatwatcher.subs import lines
    subs = []
    if ctx["style"]["on"]:
        for s, e, *_ in tz:
            subs += [[round(a + s + clip.zero_s, 2), round(bb + s + clip.zero_s, 2), t] for a, bb, t, *_ in lines.local(ctx["cues"], s, e - s)]
    stream = [[round(s + clip.zero_s, 3), round(e + clip.zero_s, 3), g, st] for s, e, g, st in tz]
    return {"teasers": stream, "brand": br.fingerprint(cfg, b), "subs": subs,
            "sub_style": ctx["style"] if subs else None, "size": [clip.width, clip.height], "fps": clip.fps, "v": 1}


def key_of(sp: dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(sp, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def on_edit(conn: sqlite3.Connection, pk: int) -> bool:
    row = _intro_row(conn, pk)
    return bool(row and row["on_edit"])


def edit_intro_key(cfg: Config, conn: sqlite3.Connection, pk: int, clip: es.ClipRef) -> str | None:
    """The intro that edit.mp4 should start with now (its key), or None for no intro."""
    return key_of(spec(cfg, conn, pk, clip)) if on_edit(conn, pk) else None


def intro_state(cfg: Config, conn: sqlite3.Connection, pk: int, clip: es.ClipRef | None = None,
                ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    """none | queued | running | done | stale | error, and the file's details."""
    clip = clip or _clip_or_none(conn, pk)
    if clip is None:
        return {"state": "none"}
    row = _intro_row(conn, pk)
    out: dict[str, Any] = {"state": "none"}
    f = intro_path(clip)
    rendered = json.loads(row["rendered_json"]) if row and row["rendered_json"] else None
    if f.is_file() and rendered:
        same = rendered.get("key") == key_of(spec(cfg, conn, pk, clip, ctx))
        out |= {"url": f"/media/intro/{pk}?v={int(f.stat().st_mtime)}", "size": f.stat().st_size, "path": str(f),
                "length": rendered.get("duration_s"), "at": row["rendered_at"], "state": "done" if same else "stale"}
    job = conn.execute("SELECT status, error FROM jobs WHERE moment_id = ? AND stage = 'intro'", (pk,)).fetchone()
    if job and job["status"] in ("pending", "running"):
        out["state"] = "queued" if job["status"] == "pending" else "running"
        out["progress"] = (row["progress"] if row else None) or 0.0
        if job["status"] == "pending":
            busy = conn.execute("SELECT stage FROM jobs WHERE status = 'running' LIMIT 1").fetchone()
            out["behind"] = busy["stage"] if busy else None
    elif job and job["status"] == "error" and out["state"] != "done":
        out |= {"state": "error", "error": (job["error"] or "")[:600]}
    return out


def load_intro(cfg: Config, conn: sqlite3.Connection, pk: int) -> dict[str, Any]:
    clip = es.clip_ref(conn, pk)
    ctx = _context(cfg, conn, pk, clip)
    tz, source, auto = teasers(cfg, conn, pk, clip, ctx)
    b = br.get(conn)
    return {"teasers": tz, "source": source, "auto": auto, "on_edit": on_edit(conn, pk),
            "duration": clip.duration_s, "segments": ctx["segments"], "logo_s": round(it.logo_seconds(), 2),
            "styles": list(it.STYLES), "max": it.MAX_TEASERS, "max_total": it.MAX_TOTAL_S,
            "brand": {"name": b.name, "logo": b.logo}, "state": intro_state(cfg, conn, pk, clip, ctx),
            "cues": [[round(c["t0"], 2), round(c["t1"], 2), c["text"].replace("*", "")] for c in ctx["cues"]]
            or [[round(ln["t"], 2), round(ln["t"] + 3, 2), ln["text"]] for ln in ctx["lines"]]}


def save_intro(cfg: Config, conn: sqlite3.Connection, pk: int, raw: Any = None, on: bool | None = None) -> None:
    """raw: teasers (clip time), "auto" to follow the edit again, or None to keep them."""
    clip = es.clip_ref(conn, pk)
    conn.execute("INSERT INTO intros (moment_id) VALUES (?) ON CONFLICT (moment_id) DO NOTHING", (pk,))
    if raw == "auto":
        conn.execute("UPDATE intros SET teasers_json = NULL, updated_at = datetime('now') WHERE moment_id = ?", (pk,))
    elif raw is not None:
        try:
            tz = it.clean(raw, clip.duration_s)
        except it.IntroError as e:
            raise es.EditError(str(e), "invalid")
        stream = [[round(s + clip.zero_s, 3), round(e + clip.zero_s, 3), g, st] for s, e, g, st in tz]
        conn.execute("UPDATE intros SET teasers_json = ?, updated_at = datetime('now') WHERE moment_id = ?",
                     (json.dumps(stream), pk))
    if on is not None:
        conn.execute("UPDATE intros SET on_edit = ?, updated_at = datetime('now') WHERE moment_id = ?", (int(on), pk))


def _fonts(cfg: Config, b: br.Brand) -> tuple[Path | None, str | None, str | None]:
    logo = br.logo_path(cfg) if b.logo and br.logo_path(cfg).is_file() else None
    return logo, br.font_file(cfg, b, "title"), br.font_file(cfg, b, "text")


def render_intro(cfg: Config, conn: sqlite3.Connection, pk: int, *,
                 on_progress: Callable[[float], None] | None = None) -> dict[str, Any]:
    """Make <clip folder>/intro.mp4 from the clip's teasers and the brand."""
    clip = es.clip_ref(conn, pk)
    ctx = _context(cfg, conn, pk, clip)
    sp = spec(cfg, conn, pk, clip, ctx)
    tz, _source, _auto = teasers(cfg, conn, pk, clip, ctx)
    b = br.get(conn)
    logo, title_font, text_font = _fonts(cfg, b)
    conn.execute("INSERT INTO intros (moment_id) VALUES (?) ON CONFLICT (moment_id) DO NOTHING", (pk,))
    last = [0.0]

    def progress(frac: float) -> None:
        if time.monotonic() - last[0] > 0.5:
            last[0] = time.monotonic()
            conn.execute("UPDATE intros SET progress = ? WHERE moment_id = ?", (round(frac, 3), pk))
        if on_progress:
            on_progress(frac)

    conn.execute("UPDATE intros SET progress = 0 WHERE moment_id = ?", (pk,))
    try:
        r = it.render_intro(b, logo, title_font, text_font, clip.path, tz, intro_path(clip), fps=clip.fps,
                            size=(clip.width, clip.height), has_audio=clip.has_audio,
                            subs=ctx["cues"] if ctx["style"]["on"] else [], sub_style=ctx["style"],
                            sub_font=cfg.subtitles.font, crf=cfg.edit.crf, preset=cfg.edit.preset, on_progress=progress)
    finally:
        conn.execute("UPDATE intros SET progress = NULL WHERE moment_id = ?", (pk,))
    info = {"key": key_of(sp), "teasers": sp["teasers"], "duration_s": round(r.duration_s, 2), "size_bytes": r.size_bytes}
    conn.execute("UPDATE intros SET rendered_json = ?, rendered_at = datetime('now') WHERE moment_id = ?",
                 (json.dumps(info), pk))
    return info


def intro_for_edit(cfg: Config, conn: sqlite3.Connection, pk: int, clip: es.ClipRef, *,
                   on_progress: Callable[[float], None] | None = None) -> tuple[Path | None, str | None]:
    """When the edit starts with the intro: make sure intro.mp4 is current (render it if
    not) and return (its path, its key). Otherwise (None, None)."""
    if not on_edit(conn, pk):
        return None, None
    want = key_of(spec(cfg, conn, pk, clip))
    row = _intro_row(conn, pk)
    have = json.loads(row["rendered_json"]).get("key") if row and row["rendered_json"] else None
    if have != want or not intro_path(clip).is_file():
        render_intro(cfg, conn, pk, on_progress=on_progress)
    return intro_path(clip), want


# ── thumbnail ────────────────────────────────────────────────────────────────
# A thumbnail is layers (brand.layers), drawn by brand.canvas. Saved in `thumbs` with
# times in stream time: layers_json (thumbnail.jpg), short_layers_json (short_cover.jpg),
# ideas_json (Claude's versions, brand.thumbai, made by the 'thumb' job). Cut-outs and the
# versions' pictures are kept in the clip's thumbs/ folder.

THUMBS_DIR = "thumbs"

_frames: OrderedDict[tuple[str, float, float], bytes] = OrderedDict()
_frames_lock = threading.Lock()


class ThumbAIError(Exception):
    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind


def frame_at(path: Path, t: float):
    """One frame of the clip as a picture (the last few are kept, so moving a layer
    doesn't decode the video again)."""
    from PIL import Image
    from chatwatcher.clip.media import MediaError, _exe
    key = (str(path), path.stat().st_mtime, round(t, 2))
    with _frames_lock:
        if key in _frames:
            _frames.move_to_end(key)
            return Image.open(io.BytesIO(_frames[key]))
    r = subprocess.run([_exe("ffmpeg"), "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                        "-f", "image2pipe", "-c:v", "png", "-"], capture_output=True, timeout=60)
    if r.returncode != 0 or not r.stdout:
        raise MediaError("Couldn't read that frame of the clip.")
    with _frames_lock:
        _frames[key] = r.stdout
        while len(_frames) > 8:
            _frames.popitem(last=False)
    return Image.open(io.BytesIO(r.stdout))


def _thumb_row(conn: sqlite3.Connection, pk: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM thumbs WHERE moment_id = ?", (pk,)).fetchone()


def _shape(shape: Any) -> str:
    if shape not in th.SHAPES:
        raise es.EditError("shape must be wide or tall.", "invalid")
    return shape


def thumb_path(clip: es.ClipRef, shape: str = "wide") -> Path:
    return clip.folder / (th.COVER_FILE if shape == "tall" else th.THUMB_FILE)


def _short_layout(conn: sqlite3.Connection, pk: int, clip: es.ClipRef) -> tuple[dict[str, Any], list]:
    """The Short's layout (saved, or the first one it would get) and its panels."""
    from chatwatcher.edit import layout as lay
    layout = es.current_layout(conn, pk, clip, es.stored(conn, pk, "short"))
    sw, sh = es.size(clip)
    return layout, lay.panels(layout, regions.get(conn, regions.scope_for(conn, pk)[0]), sw, sh)


def cutout_of(cfg: Config, clip: es.ClipRef, t: float, box: tuple[float, float, float, float]):
    """Her cut-out from the frame at t (kept in thumbs/ once made). Raises CutoutError."""
    from PIL import Image
    d = clip.folder / THUMBS_DIR
    f = d / co.cache_name(t, box, clip.path.stat().st_mtime)
    if f.is_file():
        with Image.open(f) as im:
            return im.convert("RGBA")
    img = co.cut(cfg.paths.data_dir, frame_at(clip.path, t), box)
    d.mkdir(exist_ok=True)
    tmp = f.with_suffix(".tmp.png")
    img.save(tmp)
    tmp.replace(f)
    return img


def _source(cfg: Config, conn: sqlite3.Connection, pk: int, clip: es.ClipRef, shape: str) -> cv.Source:
    b = br.get(conn)
    logo, title_font, text_font = _fonts(cfg, b)
    picture = lambda t: frame_at(clip.path, t)                       # noqa: E731
    if shape == "tall":
        _layout, panels = _short_layout(conn, pk, clip)
        picture = lambda t: th.compose(frame_at(clip.path, t), panels)  # noqa: E731
    return cv.Source(th.canvas_size(shape), b, logo, {"title": title_font, "text": text_font}, picture,
                     lambda t: frame_at(clip.path, t), lambda t, box: cutout_of(cfg, clip, t, box),
                     br.sticker_dir(cfg))


def _claude_pick(conn: sqlite3.Connection, pk: int, clip: es.ClipRef) -> tuple[float, str]:
    """The clip analysis's thumbnail frame (clip time) and words."""
    a = es.latest_analysis(conn, pk)
    pick = (a.get("thumbnail") or {})
    want = pick.get("frame_file") or ""
    t = None
    for f in _frames_of(conn, pk):
        if f.get("file") == want:
            t = float(f.get("clip_s", 0))
    if t is None:
        prev = conn.execute("SELECT frames_json FROM previews WHERE moment_id = ?", (pk,)).fetchone()
        for f in json.loads(prev["frames_json"] or "[]") if prev else []:
            if f.get("file") == want and f.get("stream_s") is not None:
                t = float(f["stream_s"]) - clip.zero_s
    if t is None:
        m = conn.execute("SELECT peak_s FROM moments WHERE id = ?", (pk,)).fetchone()
        t = float(m["peak_s"]) - clip.zero_s if m else clip.duration_s / 2
    return min(max(t, 0.0), max(0.0, clip.duration_s - 0.05)), str(pick.get("text") or "")


def _frames_of(conn: sqlite3.Connection, pk: int) -> list[dict[str, Any]]:
    row = conn.execute("SELECT frames_json FROM clips WHERE moment_id = ?", (pk,)).fetchone()
    try:
        return [f for f in json.loads(row["frames_json"] or "[]") if f.get("file")] if row else []
    except json.JSONDecodeError:
        return []


def default_layers(conn: sqlite3.Connection, pk: int, clip: es.ClipRef, shape: str = "wide") -> list[dict[str, Any]]:
    """Before you make one: the analysis's frame (zoomed on the face box) and its words."""
    t, text = _claude_pick(conn, pk, clip)
    if shape == "tall":
        return L.clean(L.from_settings({"t": t, "text": text, "place": "top"}, aspect_tall=True), clip.duration_s)
    s: dict[str, Any] = {"t": t, "text": text, "zoom": 1.0}
    face = regions.get(conn, regions.scope_for(conn, pk)[0]).get("face")
    if face:
        s |= {"cx": face["x"] + face["w"] / 2, "cy": face["y"] + face["h"] / 2, "zoom": 1.15,
              "side": "left" if face["x"] + face["w"] / 2 >= 0.5 else "right"}
    return L.clean(L.from_settings(s), clip.duration_s)


def _stored_layers(raw: str | None, clip: es.ClipRef) -> list[dict[str, Any]] | None:
    if not raw:
        return None
    try:
        return L.clean(L.shift_times(json.loads(raw), -clip.zero_s), clip.duration_s)
    except (json.JSONDecodeError, L.LayerError, TypeError, KeyError):
        return None


def _ideas(conn: sqlite3.Connection, pk: int, clip: es.ClipRef, shape: str) -> dict[str, Any]:
    row = _thumb_row(conn, pk)
    try:
        allx = json.loads(row["ideas_json"]) if row and row["ideas_json"] else {}
    except json.JSONDecodeError:
        allx = {}
    got = allx.get(shape) or {}
    vs = []
    for i, v in enumerate(got.get("versions") or []):
        layers = _stored_layers(json.dumps(v.get("layers") or []), clip)
        if layers:
            f = clip.folder / THUMBS_DIR / f"idea_{shape}_{i}.jpg"
            vs.append({"name": v.get("name", ""), "why": v.get("why", ""), "warnings": v.get("warnings", []),
                       "lines": v.get("lines", []), "layers": layers,
                       "url": f"/moment/{pk}/thumb/idea/{i}.jpg?shape={shape}&v={int(f.stat().st_mtime)}"
                       if f.is_file() else None})
    return {"versions": vs, "note": got.get("note", ""), "at": got.get("at"), "model": got.get("model")}


def current_layers(conn: sqlite3.Connection, pk: int, clip: es.ClipRef,
                   shape: str = "wide") -> tuple[list[dict[str, Any]], str]:
    """(the thumbnail's layers, where they come from: saved | old | claude | default)."""
    row = _thumb_row(conn, pk)
    mine = _stored_layers(row["short_layers_json" if shape == "tall" else "layers_json"], clip) if row else None
    if mine:
        return mine, "saved"
    old = json.loads(row["short_json" if shape == "tall" else "settings_json"] or "{}") if row else {}
    if old:                                     # made before thumbnails were layers
        old["t"] = float(old.get("t_stream", clip.zero_s)) - clip.zero_s
        return L.clean(L.from_settings(old, aspect_tall=shape == "tall"), clip.duration_s), "old"
    ideas = _ideas(conn, pk, clip, shape)["versions"]
    if ideas:
        return ideas[0]["layers"], "claude"
    return default_layers(conn, pk, clip, shape), "default"


def ai_state(conn: sqlite3.Connection, pk: int) -> dict[str, Any]:
    job = conn.execute("SELECT status, error, params_json FROM jobs WHERE moment_id = ? AND stage = 'thumb'",
                       (pk,)).fetchone()
    if not job:
        return {"state": "none"}
    params = json.loads(job["params_json"] or "{}")
    out = {"state": {"pending": "queued"}.get(job["status"], job["status"]), "shape": params.get("shape", "wide")}
    if job["status"] == "error":
        out["error"] = (job["error"] or "")[:600]
    if job["status"] == "pending":
        busy = conn.execute("SELECT stage FROM jobs WHERE status = 'running' LIMIT 1").fetchone()
        out["behind"] = busy["stage"] if busy else None
    return out


def load_thumb(cfg: Config, conn: sqlite3.Connection, pk: int, shape: str = "wide") -> dict[str, Any]:
    _shape(shape)
    clip = es.clip_ref(conn, pk)
    layers, source = current_layers(conn, pk, clip, shape)
    row = _thumb_row(conn, pk)
    made_col = "short_made_at" if shape == "tall" else "made_at"
    f = thumb_path(clip, shape)
    q = "?shape=tall&" if shape == "tall" else "?"
    made = {"url": f"/media/thumb/{pk}{q}v={int(f.stat().st_mtime)}", "path": str(f), "at": row[made_col] if row else None} \
        if f.is_file() else None
    b = br.get(conn)
    cid = conn.execute("SELECT id FROM clips WHERE moment_id = ?", (pk,)).fetchone()
    frames = [{"n": i, "t": round(float(fr.get("clip_s", 0)), 3), "peak": bool(fr.get("is_peak")),
               "url": f"/media/frame/{cid['id']}/{Path(fr['file']).name}" if cid else None}
              for i, fr in enumerate(_frames_of(conn, pk), 1)]
    out = {"shape": shape, "layers": layers, "source": source, "saved": made, "duration": clip.duration_s,
           "size": list(th.canvas_size(shape)), "pad": L.PAD, "frame": list(es.size(clip)), "frames": frames,
           "regions": regions.get(conn, regions.scope_for(conn, pk)[0]), "stickers": br.list_stickers(cfg),
           "colours": {n: getattr(b, n) for n in L.BRAND_COLOURS} | L.COLOURS,
           "cutouts": co.model_path(cfg.paths.data_dir) is not None, "brand": {"name": b.name, "logo": b.logo},
           "ideas": _ideas(conn, pk, clip, shape), "ai": ai_state(conn, pk),
           "spec": {k: (v if k == "bg" else L.COMMON | v) for k, v in L.FIELDS.items()}}
    if shape == "tall":
        layout, _ps = _short_layout(conn, pk, clip)
        out["layout"] = layout["type"]
        out["short_saved"] = es.stored(conn, pk, "short") is not None
    return out


def _clean_layers(raw: Any, clip: es.ClipRef) -> list[dict[str, Any]]:
    try:
        return L.clean(raw, clip.duration_s)
    except L.LayerError as e:
        raise es.EditError(str(e), "invalid")


def sprite_image(cfg: Config, conn: sqlite3.Connection, pk: int, raw: Any, shape: str = "wide") -> tuple[bytes, str]:
    """One layer drawn on its own, for the editor: (bytes, media type). The background is a
    JPEG of the whole picture; a layer is a PNG with layers.PAD of margin round it."""
    _shape(shape)
    clip = es.clip_ref(conn, pk)
    try:
        ly = L.clean_one(raw, clip.duration_s)
    except L.LayerError as e:
        raise es.EditError(str(e), "invalid")
    src = _source(cfg, conn, pk, clip, shape)
    try:
        img = cv.sprite(ly, src)
    except co.CutoutError as e:
        raise es.EditError(str(e), "invalid")
    buf = io.BytesIO()
    if ly["kind"] == "bg":
        img.convert("RGB").save(buf, "JPEG", quality=88)
        return buf.getvalue(), "image/jpeg"
    img.save(buf, "PNG", compress_level=1)
    return buf.getvalue(), "image/png"


def draw_thumb(cfg: Config, conn: sqlite3.Connection, pk: int, raw: Any,
               shape: str = "wide") -> tuple[bytes, list[dict[str, Any]], list[str]]:
    """The thumbnail (or the Short's cover) for these layers (clip time) as JPEG bytes, the
    clean layers, and what couldn't be drawn."""
    _shape(shape)
    clip = es.clip_ref(conn, pk)
    layers = _clean_layers(raw, clip)
    warnings: list[str] = []
    img = cv.render(layers, _source(cfg, conn, pk, clip, shape),
                    on_error=lambda ly, e: warnings.append(f"Left out a {ly['kind']} layer: {e}"))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92, optimize=True)
    return buf.getvalue(), layers, warnings


def save_thumb(cfg: Config, conn: sqlite3.Connection, pk: int, raw: Any, shape: str = "wide") -> dict[str, Any]:
    _shape(shape)
    clip = es.clip_ref(conn, pk)
    data, layers, warnings = draw_thumb(cfg, conn, pk, raw, shape)
    dest = thumb_path(clip, shape)
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    stored = json.dumps(L.shift_times(layers, clip.zero_s), ensure_ascii=False)
    col, made = ("short_layers_json", "short_made_at") if shape == "tall" else ("layers_json", "made_at")
    conn.execute(f"""INSERT INTO thumbs (moment_id, settings_json, {col}, {made}) VALUES (?, '{{}}', ?, datetime('now'))
                     ON CONFLICT (moment_id) DO UPDATE SET {col} = excluded.{col}, {made} = excluded.{made}""",
                 (pk, stored))
    return load_thumb(cfg, conn, pk, shape) | {"warnings": warnings}


def idea_path(clip: es.ClipRef, shape: str, i: int) -> Path:
    return clip.folder / THUMBS_DIR / f"idea_{_shape(shape)}_{int(i)}.jpg"


def _board(cfg: Config, conn: sqlite3.Connection, pk: int, clip: es.ClipRef, shape: str, src: cv.Source,
           frames: list[dict[str, Any]]) -> ta.Board:
    from chatwatcher.edit import layout as lay
    boxes = regions.get(conn, regions.scope_for(conn, pk)[0])
    on_picture: Callable[[float, float], tuple[float, float] | None] = lambda x, y: (x, y)   # noqa: E731
    pw, ph = es.size(clip)
    if shape == "tall":
        _layout, panels = _short_layout(conn, pk, clip)
        on_picture = lambda x, y: lay.map_point(x, y, panels)                                 # noqa: E731
        pw, ph = th.canvas_size("tall")

    def measure(ly: dict[str, Any]) -> tuple[float, float]:
        clean = L.clean_one(ly, clip.duration_s)
        return cv.content_size(clean, cv.sprite(clean, src), src)
    return ta.Board(shape, src.size, [float(f.get("clip_s", 0)) for f in frames], boxes, (pw, ph), on_picture,
                    measure, br.list_stickers(cfg))


def make_ideas(cfg: Config, conn: sqlite3.Connection, pk: int, shape: str = "wide", *, runner: Any = None,
               on_step: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Ask Claude for thumbnail versions of the clip, place them, draw each, and keep them
    (ideas_json and thumbs/idea_<shape>_<n>.jpg). Raises ThumbAIError."""
    from chatwatcher.analyze.claude_runner import ClaudeError, ClaudeRunner
    from chatwatcher.analyze.sheet import contact_sheet
    say = on_step or (lambda _m: None)
    _shape(shape)
    if not cfg.claude.enabled:
        raise ThumbAIError("claude.enabled is false in the config.", "disabled")
    clip = es.clip_ref(conn, pk)
    frames = _frames_of(conn, pk)
    if not frames:
        raise ThumbAIError("This clip has no frames to choose from (download it again).", "invalid")
    try:
        co.download(cfg.paths.data_dir)
    except co.CutoutError as e:                  # cut-outs become framed crops
        say(str(e))
    sheet = contact_sheet(clip.folder, frames)
    src = _source(cfg, conn, pk, clip, shape)
    a = es.latest_analysis(conn, pk)
    peak = float(es.meta(clip).get("peak_clip_s") or clip.duration_s / 2)
    words = [ln["text"] for ln in es.transcript_lines(clip.folder) if abs(ln["t"] - peak) <= 20]
    prompt = ta.build_prompt(shape=shape, meta=es.meta(clip), analysis=a, frames=frames,
                             boxes=regions.get(conn, regions.scope_for(conn, pk)[0]), stickers=br.list_stickers(cfg),
                             brand=src.brand, words=words)
    say("asking Claude for thumbnail versions")
    try:
        runner = runner or ClaudeRunner(cfg.claude)
        res = runner.run(prompt, ta.schema(), system=ta.SYSTEM.format(language=cfg.claude.output_language),
                         cwd=clip.folder, effort=cfg.claude.edit_effort, images=[sheet] if sheet else None)
    except ClaudeError as e:
        raise ThumbAIError(str(e), e.kind) from e
    board = _board(cfg, conn, pk, clip, shape, src, frames)
    ideas = ta.read(res.data, board, clip.duration_s)
    if not ideas:
        raise ThumbAIError("Claude's answer had no versions.", "bad_output")
    (clip.folder / THUMBS_DIR).mkdir(exist_ok=True)
    for old in (clip.folder / THUMBS_DIR).glob(f"idea_{shape}_*.jpg"):
        old.unlink()
    versions = []
    for i, idea in enumerate(ideas):
        say(f"drawing version {i + 1}: {idea.name}")
        img = cv.render(idea.layers, src, on_error=lambda ly, e, w=idea.warnings: w.append(f"Left out a {ly['kind']}: {e}"))
        img.save(idea_path(clip, shape, i), "JPEG", quality=90)
        versions.append({"name": idea.name, "why": idea.why, "warnings": idea.warnings, "lines": idea.lines,
                         "layers": L.shift_times(idea.layers, clip.zero_s)})
    row = _thumb_row(conn, pk)
    allx = json.loads(row["ideas_json"]) if row and row["ideas_json"] else {}
    allx[shape] = {"versions": versions, "note": str(res.data.get("note") or "")[:500], "model": res.model,
                   "at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), "usage": res.usage}
    conn.execute("""INSERT INTO thumbs (moment_id, settings_json, ideas_json) VALUES (?, '{}', ?)
                    ON CONFLICT (moment_id) DO UPDATE SET ideas_json = excluded.ideas_json""",
                 (pk, json.dumps(allx, ensure_ascii=False)))
    return {"versions": len(versions), "warnings": sum(len(v["warnings"]) for v in versions), "model": res.model}


# ── the brand page's preview ─────────────────────────────────────────────────

_card: dict[str, Any] = {"key": None, "jpg": b""}


def brand_card(cfg: Config, conn: sqlite3.Connection) -> bytes:
    """A still of the intro's logo card at 960×540 (cached until the brand changes)."""
    b = br.get(conn)
    key = br.fingerprint(cfg, b)
    if _card["key"] != key:
        logo, title_font, text_font = _fonts(cfg, b)
        p = it.Painter(b, logo, title_font, text_font, 960, 540, 30)
        img = p.reveal(70, p.logo_frames())
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=88)
        _card.update(key=key, jpg=buf.getvalue())
    return _card["jpg"]
