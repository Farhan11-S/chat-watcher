"""Inserts: pictures and videos from outside the stream (the X post she talks about, a
clip from another video) brought into one clip's edit as full-screen cutaways.

An insert is a row in the overlays table with moment_id set (the clip it belongs to) and
no key colour: an mp4 under data/overlays/, its sound saved apart and levelled like an
overlay's. A picture is saved as a still video MAX_STILL_S long, so the editor and the
render treat every insert like an overlay; a cutaway is an overlay effect at "fill"
(edit.overlays: fitted over a blurred copy of the picture).

fetch() takes:
- an X / Twitter post link: read through the fxtwitter API (x.com wants a login), drawn
  as a post card (name, @handle, text, its first photo), and its video, if it has one, as
  a second insert;
- a link to a picture (png, jpg, webp);
- anything else yt-dlp can download (YouTube, TikTok, a video post): up to MAX_VIDEO_S,
  from the link's t= when it has one."""

from __future__ import annotations

import io
import json
import re
import sqlite3
import subprocess
import tempfile
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from chatwatcher.clip.media import MediaError, probe
from chatwatcher.config import Config
from chatwatcher.edit import overlays as ovl

MAX_STILL_S = 15.0            # a picture's still video (a cutaway shows a part of it)
MAX_VIDEO_S = 30.0
MAX_BYTES = 60 * 1024 * 1024
MAX_LINKS = 3                 # fetched from one director's message
CARD_W = 1080
UA = "Mozilla/5.0 (compatible; chatwatcher)"
X_API = "https://api.fxtwitter.com/status/{id}"
_URL = re.compile(r"https?://[^\s<>\"'`]+")
_X = re.compile(r"^https?://(?:www\.|mobile\.)?(?:x|twitter|fxtwitter|vxtwitter|fixupx|fixvx)\.com/"
                r"(?:[^/?#]+|i(?:/web)?)/status(?:es)?/(\d+)", re.I)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]+")   # no font here draws them
_PICTURE = re.compile(r"\.(png|jpe?g|webp)(?:$|\?)", re.I)


class InsertError(Exception):
    pass


def links(text: str) -> list[str]:
    """The links in a message, in order, without repeats (at most MAX_LINKS)."""
    out: list[str] = []
    for m in _URL.finditer(text or ""):
        u = m.group(0).rstrip(".,;:!?)]}>")
        if u not in out:
            out.append(u)
    return out[:MAX_LINKS]


def is_link(text: str) -> bool:
    return bool(_URL.fullmatch((text or "").strip()))


def x_post_id(url: str) -> str | None:
    m = _X.match(url.strip())
    return m.group(1) if m else None


def for_moment(conn: sqlite3.Connection, moment_pk: int) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM overlays WHERE moment_id = ? ORDER BY added_at, id",
                                          (moment_pk,))]


def _get(url: str, limit: int = MAX_BYTES, timeout: float = 30) -> tuple[bytes, str]:
    """The body and its content type."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(limit + 1)
            kind = r.headers.get("Content-Type", "")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise InsertError(f"couldn't download {url}: {e}") from e
    if len(data) > limit:
        raise InsertError(f"{url} is bigger than {limit // 2 ** 20} MB")
    return data, kind


def _ffmpeg(args: list[str], timeout: float = 300) -> None:
    from chatwatcher.clip.media import _exe
    try:
        out = subprocess.run([_exe("ffmpeg"), "-v", "error", "-y", *args], capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise InsertError("making the insert took too long") from e
    if out.returncode != 0:
        raise InsertError(f"ffmpeg: {out.stderr.decode(errors='replace')[-300:]}")


# ── pictures ─────────────────────────────────────────────────────────────────

def _fonts() -> tuple[str | None, str | None]:
    """(regular, bold) font files that cover Latin and CJK when the system has them."""
    from chatwatcher.edit import captions as cap
    regular = ["/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    bold = ["/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", str(cap.FONT_DIR / cap.CJK_FILE)]
    pick = lambda xs: next((x for x in xs if Path(x).is_file()), None)   # noqa: E731
    return pick(regular) or pick(bold), pick(bold)


def _font(path: str | None, size: int):
    from PIL import ImageFont
    return ImageFont.truetype(path, size) if path else ImageFont.load_default(size)


def _wrap(draw, text: str, font, width: int) -> list[str]:
    out: list[str] = []
    for para in text.split("\n"):
        line = ""
        for word in re.findall(r"\S+\s*|\s+", para) or [""]:
            if draw.textlength(line + word, font=font) <= width or not line:
                line += word
                while draw.textlength(line, font=font) > width and len(line) > 1:   # one long word (or CJK)
                    cut = max(1, len(line) - 1)
                    while cut > 1 and draw.textlength(line[:cut], font=font) > width:
                        cut -= 1
                    out.append(line[:cut])
                    line = line[cut:]
            else:
                out.append(line.rstrip())
                line = word.lstrip()
        out.append(line.rstrip())
    return out


def post_card(name: str, handle: str, text: str, avatar: bytes | None = None, photo: bytes | None = None,
              when: str = "") -> bytes:
    """An X post drawn as a card (dark, like the app), PNG bytes."""
    from PIL import Image, ImageDraw
    regular, bold = _fonts()
    pad, av = 56, 110
    f_name, f_handle, f_text = _font(bold, 46), _font(regular, 38), _font(regular, 50)
    probe_img = Image.new("RGB", (10, 10))
    text = re.sub(r"[ \t]+", " ", _EMOJI.sub("", text)).strip()
    lines = _wrap(ImageDraw.Draw(probe_img), text, f_text, CARD_W - 2 * pad)[:14]
    line_h = 66
    pic = None
    if photo:
        try:
            pic = Image.open(io.BytesIO(photo)).convert("RGB")
            w = CARD_W - 2 * pad
            pic = pic.resize((w, max(1, round(pic.height * w / pic.width))))
            if pic.height > 900:
                top = (pic.height - 900) // 2
                pic = pic.crop((0, top, w, top + 900))
        except Exception:      # noqa: BLE001 — a bad photo just leaves the card without it
            pic = None
    h = pad + av + 34 + len(lines) * line_h + (24 + pic.height if pic else 0) + (64 if when else 0) + pad
    img = Image.new("RGB", (CARD_W, h), (0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, CARD_W - 5, h - 5), radius=36, outline=(47, 51, 54), width=3, fill=(0, 0, 0))
    if avatar:
        try:
            a = Image.open(io.BytesIO(avatar)).convert("RGB").resize((av, av))
            mask = Image.new("L", (av, av), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, av - 1, av - 1), fill=255)
            img.paste(a, (pad, pad), mask)
        except Exception:      # noqa: BLE001
            d.ellipse((pad, pad, pad + av, pad + av), fill=(51, 54, 57))
    else:
        d.ellipse((pad, pad, pad + av, pad + av), fill=(51, 54, 57))
    x = pad + av + 28
    d.text((x, pad + 6), _EMOJI.sub("", name).strip()[:40] or handle, font=f_name, fill=(231, 233, 234))
    d.text((x, pad + 62), f"@{handle}"[:40], font=f_handle, fill=(113, 118, 123))
    y = pad + av + 34
    for ln in lines:
        d.text((pad, y), ln, font=f_text, fill=(231, 233, 234))
        y += line_h
    if pic:
        y += 24
        mask = Image.new("L", pic.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, pic.width - 1, pic.height - 1), radius=28, fill=255)
        img.paste(pic, (pad, y), mask)
        y += pic.height
    if when:
        d.text((pad, y + 20), when, font=f_handle, fill=(113, 118, 123))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def _still(png: bytes, dest: Path) -> None:
    """A picture as a still mp4 MAX_STILL_S long (even size, at most 1920 on its long side)."""
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        f.write(png)
        f.flush()
        _ffmpeg(["-loop", "1", "-framerate", "30", "-t", f"{MAX_STILL_S:g}", "-i", f.name,
                 "-vf", "scale='min(1920,iw)':-2:force_original_aspect_ratio=decrease,"
                        "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
                 "-c:v", "libx264", "-tune", "stillimage", "-preset", "veryfast", "-crf", "20", "-an", str(dest)])


def _picture_png(data: bytes) -> bytes:
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:     # noqa: BLE001
        raise InsertError("that link isn't a picture this can read") from e
    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG")
    return out.getvalue()


# ── videos ───────────────────────────────────────────────────────────────────

def _start_of(url: str) -> float:
    """The t= of a link ("90", "1m30s", "90s"), 0 without one."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    t = (q.get("t") or q.get("start") or [""])[0]
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s?)?", t or "")
    if not t or not m:
        return 0.0
    h, mi, s = (float(g) if g else 0.0 for g in m.groups())
    return h * 3600 + mi * 60 + s


def _video(src: Path, dest: Path, start: float = 0.0) -> None:
    """Up to MAX_VIDEO_S of a video as an mp4, at most 720 tall, with its sound."""
    _ffmpeg(["-ss", f"{start:.3f}", "-i", str(src), "-t", f"{MAX_VIDEO_S:g}", "-map", "0:v:0", "-map", "0:a:0?",
             "-vf", "scale=-2:'min(720,ih)',scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac", "-b:a", "160k",
             "-movflags", "+faststart", str(dest)], timeout=600)


def _ytdlp(cfg: Config, url: str, folder: Path) -> Path:
    from chatwatcher.ingest.ytdlp import YtDlp, YtDlpError
    try:
        y = YtDlp(cfg.youtube)
        start = _start_of(url)
        args = ["-f", "bv*[height<=720]+ba/b[height<=720]/b", "--merge-output-format", "mp4",
                "--max-filesize", str(MAX_BYTES * 4), "-o", str(folder / "src.%(ext)s"), "--no-progress"]
        if start or "youtu" in url:
            args += ["--download-sections", f"*{start:.0f}-{start + MAX_VIDEO_S:.0f}"]
        y.run(args + [url], timeout=600)
    except YtDlpError as e:
        raise InsertError(f"yt-dlp couldn't get {url}: {str(e)[-300:]}") from e
    got = sorted(p for p in folder.glob("src.*") if p.suffix not in (".part", ".ytdl"))
    if not got:
        raise InsertError(f"yt-dlp found nothing to download at {url}")
    return got[0]


# ── storing ──────────────────────────────────────────────────────────────────

def _add(cfg: Config, conn: sqlite3.Connection, moment_pk: int, made: Path, *, name: str, description: str,
         url: str, tags: str) -> dict[str, Any]:
    """Move a made mp4 into the overlay folder and add it as this clip's insert."""
    try:
        info = probe(made)
    except MediaError as e:
        raise InsertError("the insert came out unreadable") from e
    oid = ovl.make_id(conn, name)
    folder = ovl.overlays_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / f"{oid}.mp4"
    made.replace(video)
    sound = folder / f"{oid}.flac"
    has_sound = bool(info.acodec) and ovl._level(video, sound)
    conn.execute(
        """INSERT INTO overlays (id, name, description, tags, source, file, sound, duration_s, width, height,
                                 key_color, original, moment_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)""",
        (oid, name[:80], description.strip()[:500], tags, url[:300], video.name, sound.name if has_sound else None,
         round(info.duration_s, 3), info.width, info.height, url[:200], moment_pk))
    return ovl.get_overlay(conn, oid) or {}


def fetch(cfg: Config, conn: sqlite3.Connection, moment_pk: int, url: str) -> list[dict[str, Any]]:
    """Download what a link points at into this clip's inserts (a link fetched before
    gives what it gave then). Raises InsertError."""
    url = url.strip()
    if not is_link(url):
        raise InsertError(f"not a link: {url[:80]}")
    had = [dict(r) for r in conn.execute("SELECT * FROM overlays WHERE moment_id = ? AND source = ? ORDER BY id",
                                         (moment_pk, url[:300]))]
    if had:
        return had
    with tempfile.TemporaryDirectory(prefix="insert_", dir=ovl.overlays_dir(cfg).parent
                                     if ovl.overlays_dir(cfg).parent.is_dir() else None) as tmp:
        folder = Path(tmp)
        pid = x_post_id(url)
        if pid:
            return _x_post(cfg, conn, moment_pk, url, pid, folder)
        if _PICTURE.search(urllib.parse.urlparse(url).path):
            data, _kind = _get(url)
            _still(_picture_png(data), folder / "made.mp4")
            return [_add(cfg, conn, moment_pk, folder / "made.mp4", name=f"picture {Path(urllib.parse.urlparse(url).path).stem}",
                         description="A picture from a link.", url=url, tags="insert, picture")]
        src = _ytdlp(cfg, url, folder)
        _video(src, folder / "made.mp4", 0.0)
        return [_add(cfg, conn, moment_pk, folder / "made.mp4", name=f"video {src.stem}"[:60],
                     description=f"A video from {urllib.parse.urlparse(url).netloc}.", url=url, tags="insert, video")]


def _x_post(cfg: Config, conn: sqlite3.Connection, moment_pk: int, url: str, pid: str,
            folder: Path) -> list[dict[str, Any]]:
    data, _ = _get(X_API.format(id=pid), limit=4 * 2 ** 20)
    try:
        post = json.loads(data)["tweet"]
    except (ValueError, KeyError, TypeError) as e:
        raise InsertError("that X post couldn't be read (deleted, private, or the post reader is down)") from e
    author = post.get("author") or {}
    handle, name = author.get("screen_name") or "?", author.get("name") or "?"
    text = str(post.get("text") or "")
    media = post.get("media") or {}
    photos, videos = media.get("photos") or [], media.get("videos") or []

    def grab(u: str | None, limit: int = 15 * 2 ** 20) -> bytes | None:
        if not u:
            return None
        try:
            return _get(u, limit=limit)[0]
        except InsertError:
            return None

    when = str(post.get("created_at") or "")
    when = " ".join(when.split()[1:3] + when.split()[-1:]) if when.count(" ") >= 4 else when
    still = (videos[0].get("thumbnail_url") if videos and not photos else (photos[0].get("url") if photos else None))
    card = post_card(name, handle, text, grab(author.get("avatar_url")), grab(still), when)
    _still(card, folder / "card.mp4")
    said = " ".join(text.split())
    extra = f" With {len(photos)} photo(s)." if photos else ""
    out = [_add(cfg, conn, moment_pk, folder / "card.mp4", name=f"X post @{handle}",
                description=f"X post by {name} (@{handle}): “{said}”.{extra} Shown as a post card.",
                url=url, tags="insert, x post")]
    if videos and videos[0].get("url"):
        raw = grab(videos[0]["url"], MAX_BYTES)
        if raw:
            (folder / "src.mp4").write_bytes(raw)
            _video(folder / "src.mp4", folder / "made.mp4")
            out.append(_add(cfg, conn, moment_pk, folder / "made.mp4", name=f"X video @{handle}",
                            description=f"The video in @{handle}'s X post “{said[:200]}”.", url=url,
                            tags="insert, x post, video"))
    return out


def fetch_all(cfg: Config, conn: sqlite3.Connection, moment_pk: int, urls: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch each link; (the inserts, what went wrong)."""
    got, failed = [], []
    for u in urls[:MAX_LINKS]:
        try:
            got += fetch(cfg, conn, moment_pk, u)
        except InsertError as e:
            failed.append(str(e))
    return got, failed
