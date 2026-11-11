"""The editor's copy of a clip (edit_proxy.mp4): the same picture and sound, made to seek fast.

The edit preview plays the cut by jumping the <video> from one segment to the next. A
stream's clip.mp4 (VP9, 1080-1440p, 60 fps) has a keyframe only every 2-7 s, so every
jump decodes up to a few hundred full-size frames first and playback stalls at each cut.
This copy is H.264 (the browser decodes it in hardware), at most 1080p, with a keyframe
every KEY_S, so a jump decodes a handful of frames. Its sound starts at clip time 0 (see
clip.media.AUDIO_FROM_ZERO), so its timeline is the clip's.

Only the editor plays it; renders and uploads use clip.mp4."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from chatwatcher.clip.media import AUDIO_FROM_ZERO, MediaError, _exe

PROXY_FILE = "edit_proxy.mp4"
KEY_S = 0.5               # a keyframe this often
MAX_HEIGHT = 1080         # enough for the Short preview's tight face crops
CRF = 24


def fresh(folder: Path, clip: Path) -> Path | None:
    """The proxy, if it was made from this clip.mp4 (not an older download)."""
    p = Path(folder) / PROXY_FILE
    try:
        return p if p.stat().st_mtime >= clip.stat().st_mtime and p.stat().st_size > 0 else None
    except OSError:
        return None


def make(clip: Path, folder: Path | None = None, say: Callable[[str], None] | None = None,
         threads: int = 0) -> Path:
    """Write the proxy next to clip.mp4 (or in folder). Returns its path."""
    folder = Path(folder or clip.parent)
    out = folder / PROXY_FILE
    tmp = folder / (PROXY_FILE + ".tmp.mp4")
    (say or (lambda _m: None))("Making the editor's copy (fast seeking)…")
    cmd = [_exe("ffmpeg"), "-v", "error", "-nostdin", "-y", "-i", str(clip),
           "-map", "0:v:0", "-map", "0:a:0?",
           "-vf", f"scale=-2:'min({MAX_HEIGHT},ih)':flags=bicubic",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", str(CRF), "-pix_fmt", "yuv420p",
           "-force_key_frames", f"expr:gte(t,n_forced*{KEY_S})", "-sc_threshold", "0",
           *(["-threads", str(threads)] if threads else []),
           "-c:a", "aac", "-b:a", "128k", *AUDIO_FROM_ZERO,
           "-movflags", "+faststart", str(tmp)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=3600)
    except subprocess.TimeoutExpired as e:
        tmp.unlink(missing_ok=True)
        raise MediaError("making the editor's copy timed out") from e
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise MediaError("ffmpeg couldn't make the editor's copy: " + r.stderr.decode(errors="replace")[-300:])
    tmp.replace(out)
    return out


def forget(folder: Path) -> None:
    (Path(folder) / PROXY_FILE).unlink(missing_ok=True)
