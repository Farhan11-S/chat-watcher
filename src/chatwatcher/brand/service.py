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
from chatwatcher.brand import intro as it
from chatwatcher.brand import thumb as th
from chatwatcher.config import Config
from chatwatcher.edit import regions
from chatwatcher.edit import service as es

INTRO_FILE = "intro.mp4"


# ── what the clip's edit looks like now ─────────────────────────────────────

def _context(cfg: Config, conn: sqlite3.Connection, pk: int, clip: es.ClipRef) -> dict[str, Any]:
    from chatwatcher.edit.audio import RATE, analyse
    from chatwatcher.subs import lines
    row = es._stored(conn, pk)
    wf = analyse(clip.path, clip.folder / es.WAVEFORM_FILE)
    a = es._latest_analysis(conn, pk)
    segs = es._to_clip(json.loads(row["segments_json"]), clip) if row else []
    if not segs:
        props = es.proposals(a, clip, wf, cfg.edit.snap_s)
        segs = props["long"] or props["whole"]
    cues, _src, style = es.current_subs(cfg, row, clip)
    shift = float((a.get("_meta") or {}).get("zero_s", clip.zero_s)) - clip.zero_s
    beats = [{"start": float(b.get("start", 0)) + shift, "end": float(b.get("end", 0)) + shift, "beat": b.get("beat", "")}
             for b in a.get("storyboard") or []]
    words = es._words(clip)
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
            subs += [[round(a + s + clip.zero_s, 2), round(bb + s + clip.zero_s, 2), t] for a, bb, t in lines.local(ctx["cues"], s, e - s)]
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

_frames: OrderedDict[tuple[str, float, float], bytes] = OrderedDict()
_frames_lock = threading.Lock()


def frame_at(path: Path, t: float):
    """One frame of the clip as a picture (the last few are kept, so moving the text or
    zoom doesn't decode the video again)."""
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
        while len(_frames) > 6:
            _frames.popitem(last=False)
    return Image.open(io.BytesIO(r.stdout))


def _thumb_row(conn: sqlite3.Connection, pk: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM thumbs WHERE moment_id = ?", (pk,)).fetchone()


def thumb_defaults(conn: sqlite3.Connection, pk: int, clip: es.ClipRef, shape: str = "wide") -> dict[str, Any]:
    """Claude's frame and text; the thumbnail zoomed on the channel's face box if there is
    one, the Short's cover as the Short shows it."""
    a = es._latest_analysis(conn, pk)
    pick = (a.get("thumbnail") or {})
    want = pick.get("frame_file") or ""
    t = None
    row = conn.execute("SELECT frames_json FROM clips WHERE moment_id = ?", (pk,)).fetchone()
    for f in json.loads(row["frames_json"] or "[]") if row else []:
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
    picked = {"t": min(max(t, 0.0), max(0.0, clip.duration_s - 0.05)), "text": pick.get("text", "")}
    if shape == "tall":
        return th.clean(dict(th.TALL_DEFAULTS) | picked, clip.duration_s, th.TALL_DEFAULTS)
    s = dict(th.DEFAULTS) | picked
    face = regions.get(conn, regions.scope_for(conn, pk)[0]).get("face")
    if face:
        s |= {"cx": round(face["x"] + face["w"] / 2, 4), "cy": round(face["y"] + face["h"] / 2, 4), "zoom": 1.15}
    return th.clean(s, clip.duration_s)


def _shape(shape: Any) -> str:
    if shape not in th.SHAPES:
        raise es.EditError("shape must be wide or tall.", "invalid")
    return shape


def thumb_path(clip: es.ClipRef, shape: str = "wide") -> Path:
    return clip.folder / (th.COVER_FILE if shape == "tall" else th.THUMB_FILE)


def _short_layout(conn: sqlite3.Connection, pk: int, clip: es.ClipRef) -> tuple[dict[str, Any], list]:
    """The Short's layout (saved, or the first one it would get) and its panels."""
    from chatwatcher.edit import layout as lay
    layout = es.current_layout(conn, pk, clip, es._stored(conn, pk, "short"))
    sw, sh = es._size(clip)
    return layout, lay.panels(layout, regions.get(conn, regions.scope_for(conn, pk)[0]), sw, sh)


def load_thumb(cfg: Config, conn: sqlite3.Connection, pk: int, shape: str = "wide") -> dict[str, Any]:
    _shape(shape)
    clip = es.clip_ref(conn, pk)
    base = thumb_defaults(conn, pk, clip, shape)
    row = _thumb_row(conn, pk)
    col, made_col = ("short_json", "short_made_at") if shape == "tall" else ("settings_json", "made_at")
    settings = base
    saved = json.loads(row[col]) if row and row[col] else {}
    if saved:                                   # (a cover saved first leaves {} for the thumbnail)
        saved["t"] = float(saved.get("t_stream", clip.zero_s)) - clip.zero_s
        settings = th.clean(saved, clip.duration_s, base)
    f = thumb_path(clip, shape)
    q = "?shape=tall&" if shape == "tall" else "?"
    made = {"url": f"/media/thumb/{pk}{q}v={int(f.stat().st_mtime)}", "path": str(f), "at": row[made_col] if row else None} \
        if f.is_file() else None
    out = {"shape": shape, "settings": settings, "defaults": base, "saved": made, "duration": clip.duration_s,
           "styles": list(th.STYLES), "sides": list(th.SIDES), "places": list(th.PLACES),
           "zoom_range": list(th.ZOOM_RANGE), "has_face": base.get("zoom", 1) != 1,
           "brand": {"name": br.get(conn).name}, "frame": [clip.width or 1920, clip.height or 1080],
           "size": [th.TW, th.TH] if shape == "tall" else [th.W, th.H]}
    if shape == "tall":
        layout, _ps = _short_layout(conn, pk, clip)
        out["layout"] = layout["type"]
        out["short_saved"] = es._stored(conn, pk, "short") is not None
    return out


def draw_thumb(cfg: Config, conn: sqlite3.Connection, pk: int, raw: Any,
               shape: str = "wide") -> tuple[bytes, dict[str, Any]]:
    """The thumbnail (or the Short's cover) for these settings (clip time) as JPEG bytes,
    and the clean settings."""
    _shape(shape)
    clip = es.clip_ref(conn, pk)
    try:
        s = th.clean(raw if isinstance(raw, dict) else {}, clip.duration_s, thumb_defaults(conn, pk, clip, shape))
    except th.ThumbError as e:
        raise es.EditError(str(e), "invalid")
    b = br.get(conn)
    logo, title_font, text_font = _fonts(cfg, b)
    frame = frame_at(clip.path, s["t"])
    if shape == "tall":
        _layout, ps = _short_layout(conn, pk, clip)
        img = th.draw_tall(th.compose(frame, ps), s, b, logo, title_font, text_font)
    else:
        img = th.draw(frame, s, b, logo, title_font, text_font)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90, optimize=True)
    return buf.getvalue(), s


def save_thumb(cfg: Config, conn: sqlite3.Connection, pk: int, raw: Any, shape: str = "wide") -> dict[str, Any]:
    _shape(shape)
    clip = es.clip_ref(conn, pk)
    data, s = draw_thumb(cfg, conn, pk, raw, shape)
    dest = thumb_path(clip, shape)
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    stored = json.dumps({k: v for k, v in s.items() if k != "t"} | {"t_stream": round(s["t"] + clip.zero_s, 3)},
                        ensure_ascii=False)
    if shape == "tall":
        conn.execute("""INSERT INTO thumbs (moment_id, settings_json, short_json, short_made_at)
                        VALUES (?, ?, ?, datetime('now'))
                        ON CONFLICT (moment_id) DO UPDATE SET short_json = excluded.short_json,
                            short_made_at = excluded.short_made_at""",
                     (pk, json.dumps({}), stored))
    else:
        conn.execute("""INSERT INTO thumbs (moment_id, settings_json, made_at) VALUES (?, ?, datetime('now'))
                        ON CONFLICT (moment_id) DO UPDATE SET settings_json = excluded.settings_json,
                            made_at = excluded.made_at""", (pk, stored))
    return load_thumb(cfg, conn, pk, shape)


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
