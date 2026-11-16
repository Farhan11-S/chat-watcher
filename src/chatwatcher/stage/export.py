"""stage.mp4: the 3D stage exported by the '3d' job. The dashboard's own 3D page is opened
in headless Chrome (stage.browser) at the video's size and stepped through the cut one frame
at a time, in order (the hair and clothes physics needs that); each frame goes to ffmpeg as
a JPEG, and the cut's sound (stage_audio.m4a) is laid under them. The page draws the
captions and the "© COVER" credit itself, so they are in every frame.

It needs the dashboard running (cw serve), at [web] host:port of the config."""

from __future__ import annotations

import base64
import json
import math
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from chatwatcher.config import Config
from chatwatcher.edit import clipref as cr
from chatwatcher.stage import store, track as tk

FPS = 30
SIZES = {"short": (1080, 1920), "long": (1920, 1080)}          # stage.mp4
# What the page draws, scaled up by ffmpeg: headless Chrome draws WebGL on the CPU here
# (SwiftShader: ~0.9 s a frame at 1080×1920, ~0.6 s at 720×1280, on 4 cores).
DRAW = {"short": (720, 1280), "long": (1280, 720)}
SCREEN_DIR = ".stage_screen"      # the room's screen footage as stills, while an export runs


class ExportError(Exception):
    pass


def page_url(cfg: Config, moment_pk: int, w: int, h: int) -> str:
    host = cfg.web.host if cfg.web.host not in ("0.0.0.0", "::", "") else "127.0.0.1"
    return f"http://{host}:{cfg.web.port}/moment/{moment_pk}/3d?export=1&w={w}&h={h}"


def screen_frames(clip: cr.ClipRef, cut: list[list[float]], n: int) -> Path:
    """The cut's picture as numbered stills (000001.jpg is frame 0), for the room's screen:
    seeking the video in the page for every frame is far slower."""
    from chatwatcher.clip.media import _exe
    folder = clip.folder / SCREEN_DIR
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir()
    parts = [f"[s{i}]trim=start={c0:.3f}:end={c1:.3f},setpts=PTS-STARTPTS[p{i}]" for i, (c0, c1, _) in enumerate(cut)]
    graph = (f"[0:v]split={len(cut)}" + "".join(f"[s{i}]" for i in range(len(cut))) + ";" + ";".join(parts) + ";"
             + "".join(f"[p{i}]" for i in range(len(cut))) + f"concat=n={len(cut)}:v=1:a=0,fps={FPS},"
             "scale=w=1280:h=720:force_original_aspect_ratio=decrease[o]")
    try:
        subprocess.run([_exe("ffmpeg"), "-y", "-v", "error", "-i", str(clip.path), "-filter_complex", graph, "-map", "[o]",
                        "-frames:v", str(n), "-q:v", "4", str(folder / "%06d.jpg")], check=True, capture_output=True,
                       timeout=1800)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        shutil.rmtree(folder, ignore_errors=True)
        raise ExportError(f"Couldn't cut the screen's footage: {getattr(e, 'stderr', b'')[-300:]!r}") from e
    return folder


def export(cfg: Config, conn: sqlite3.Connection, moment_pk: int, *, say: Callable[[str], None] | None = None,
           size: tuple[int, int] | None = None, frames: int | None = None, url: str | None = None) -> dict[str, Any]:
    """Make stage.mp4 for the moment's stage. size / frames / url: for tests and trials."""
    from chatwatcher.clip.media import _exe
    from chatwatcher.stage.browser import Browser, BrowserError
    say = say or (lambda _m: None)
    st = store.get(conn, moment_pk)
    if st is None:
        raise ExportError("Set the stage up first (the 3D page).")
    if not st.get("model"):
        raise ExportError("Choose a model for the stage first.")
    try:
        tr = tk.ensure(cfg, conn, moment_pk, st, say)
    except tk.TrackError as e:
        raise ExportError(str(e)) from e
    clip = cr.clip_ref(conn, moment_pk)
    w, h = size or DRAW[st["shape"]]
    ow, oh = SIZES[st["shape"]] if size is None else size
    n = frames if frames is not None else math.ceil(tr["duration"] * FPS)
    out = clip.folder / store.STAGE_FILE
    tmp = clip.folder / (store.STAGE_FILE + ".part.mp4")
    started = time.monotonic()
    screens = None
    if st["set_name"] == "room":
        say("Cutting the screen's footage into stills…")
        screens = screen_frames(clip, tr["segments"], n)
        have = len(list(screens.glob("*.jpg")))

    def progress(k: float) -> None:
        conn.execute("UPDATE stages SET render_progress = ? WHERE moment_id = ?", (round(k, 3), moment_pk))

    progress(0)
    ff = subprocess.Popen(
        [_exe("ffmpeg"), "-y", "-v", "error", "-f", "image2pipe", "-framerate", str(FPS), "-c:v", "mjpeg", "-i", "-",
         "-i", str(clip.folder / tk.AUDIO_FILE), "-map", "0:v", "-map", "1:a", "-vf", f"scale={ow}:{oh}:flags=lanczos",
         "-c:v", "libx264", "-preset", "medium",
         "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
         str(tmp)], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert ff.stdin is not None
    try:
        with Browser(w, h) as b:
            say(f"Opening the 3D page at {w}×{h}…")
            try:
                page = b.open(url or page_url(cfg, moment_pk, w, h))
                title = page.wait_title()
            except (BrowserError, OSError) as e:
                raise ExportError(f"Couldn't open the 3D page ({e}). The dashboard (cw serve) must be running.") from e
            if title != "ready":
                msg = page.eval("document.getElementById('st-msg')?.textContent || ''")
                raise ExportError(f"The 3D page couldn't load: {msg}")
            say(f"Drawing {n} frames…")
            base = (url or page_url(cfg, moment_pk, w, h)).split("/moment/")[0]
            for i in range(n):
                dt = "undefined" if i == 0 else f"{1 / FPS:.6f}"
                screen = f"'{base}/media/stage/{moment_pk}/screen/{min(i, have - 1) + 1}'" if screens and have else "null"
                data = page.eval(f"stage3d.frame({i / FPS:.5f}, {dt}, {screen})")
                ff.stdin.write(base64.b64decode(data))
                if i % 15 == 0:
                    progress(0.97 * i / max(n, 1))
        ff.stdin.close()
        if ff.wait(timeout=600) != 0:
            raise ExportError(f"ffmpeg failed: {(ff.stderr.read() if ff.stderr else b'')[-400:]!r}")
    except BaseException:
        if ff.poll() is None:
            ff.kill()
        tmp.unlink(missing_ok=True)
        conn.execute("UPDATE stages SET render_progress = NULL WHERE moment_id = ?", (moment_pk,))
        raise
    finally:
        if screens:
            shutil.rmtree(screens, ignore_errors=True)
    tmp.replace(out)
    conn.execute("UPDATE stages SET rendered_json = ?, rendered_at = datetime('now'), render_progress = NULL "
                 "WHERE moment_id = ?", (json.dumps(store.rendered_key(st, tr["key"])), moment_pk))
    return {"duration_s": tr["duration"], "frames": n, "took_s": time.monotonic() - started,
            "size_bytes": out.stat().st_size, "path": str(out)}
