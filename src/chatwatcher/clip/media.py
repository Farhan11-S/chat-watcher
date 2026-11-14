"""ffprobe / ffmpeg helpers. Everything here is stream copy (no re-encoding, no quality loss),
except concat when the files it joins are not alike."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from chatwatcher.tools import resolve_exe


class MediaError(Exception):
    pass


# Decoding a clip's sound on its own, ffmpeg starts at the first audio sample. In clips cut
# from a stream the audio often starts 1-3 s after the picture (the cut lands on a video
# keyframe before the audio), and a player puts it there, so samples read without this
# would be early by that much: captions before the words, the waveform off. This pads the
# start with silence (or trims) so sample 0 is clip time 0.
AUDIO_FROM_ZERO = ["-af", "aresample=async=1:first_pts=0"]


def _exe(name: str) -> str:
    exe = resolve_exe(name)
    if not exe:
        raise MediaError(f"{name} not found (install ffmpeg)")
    return exe


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise MediaError(f"{Path(cmd[0]).name} timed out after {timeout:.0f}s") from e


@dataclass
class Probe:
    duration_s: float
    width: int | None
    height: int | None
    fps: float | None
    vcodec: str | None
    acodec: str | None
    first_video_pts: float     # start of the first video frame; > 0 = audio-only lead-in
    size_bytes: int


def probe(path: str | Path) -> Probe:
    path = Path(path)
    out = _run([_exe("ffprobe"), "-v", "error", "-show_entries",
                "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate",
                "-of", "json", str(path)], timeout=60)
    if out.returncode != 0:
        raise MediaError(f"ffprobe failed on {path.name}: {out.stderr.strip()[:300]}")
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), {})
    fps = None
    if v.get("avg_frame_rate") and v["avg_frame_rate"] != "0/0":
        fps = round(float(Fraction(v["avg_frame_rate"])), 3)

    first = _run([_exe("ffprobe"), "-v", "error", "-select_streams", "v:0", "-read_intervals",
                  "%+#1", "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path)], timeout=60)
    try:
        first_pts = float(first.stdout.strip().splitlines()[0].strip(","))
    except (ValueError, IndexError):
        first_pts = 0.0

    return Probe(
        duration_s=float(data.get("format", {}).get("duration") or 0.0),
        width=v.get("width"), height=v.get("height"), fps=fps,
        vcodec=v.get("codec_name"), acodec=a.get("codec_name"),
        first_video_pts=first_pts, size_bytes=path.stat().st_size,
    )


def remux(src: Path, dest_stem: Path, *, trim_start: float = 0.0) -> Path:
    """Copy streams into mp4 (mkv if the codecs don't fit mp4), optionally dropping the
    first `trim_start` seconds. trim_start must be a video keyframe time, so the cut is
    exact even without re-encoding. Returns the written file."""
    base = [_exe("ffmpeg"), "-v", "error", "-y"]
    if trim_start > 0:
        base += ["-ss", f"{trim_start:.3f}"]
    base += ["-i", str(src), "-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero"]
    last_err = ""
    for ext, extra in ((".mp4", ["-movflags", "+faststart"]), (".mkv", [])):
        dest = dest_stem.with_suffix(ext)
        out = _run(base + extra + [str(dest)], timeout=600)
        if out.returncode == 0 and dest.is_file() and dest.stat().st_size > 0:
            return dest
        dest.unlink(missing_ok=True)
        last_err = out.stderr.strip()[:300]
    raise MediaError(f"ffmpeg remux failed for {src.name}: {last_err}")


def alike(probes: list[Probe]) -> bool:
    """Can these files be joined by stream copy? (same codecs, picture size and frame rate)"""
    a = probes[0]
    return all(p.vcodec == a.vcodec and p.acodec == a.acodec and p.width == a.width and p.height == a.height
               and abs((p.fps or 0) - (a.fps or 0)) < 0.5 for p in probes[1:])


def concat(parts: list[Path], dest: Path, *, copy: bool = True) -> Path:
    """Join files end to end into dest (.mp4). copy: the picture is stream-copied, for files
    cut from the same stream in the same format; otherwise (or when the copy fails or comes
    out the wrong length) everything is re-encoded once at high quality, at the first
    file's size. Either way the sound runs from 0 to the end with no holes."""
    probes = [probe(p) for p in parts]
    total = sum(p.duration_s for p in probes)
    dest.unlink(missing_ok=True)
    if copy and alike(probes):
        # The picture is copied. The sound is encoded once more, because in a file cut from a
        # stream it starts a second or so after the picture (see AUDIO_FROM_ZERO): copied,
        # every join would leave a hole in the sound's timestamps, and whatever reads the
        # sound alone would hear the later parts early.
        sound = ["-c:a", "aac", "-b:a", "192k", *AUDIO_FROM_ZERO] if any(p.acodec for p in probes) else []
        listing = dest.with_suffix(".parts.txt")
        listing.write_text("".join("file '" + str(p.resolve()).replace("'", "'\\''") + "'\n" for p in parts),
                           encoding="utf-8")
        try:
            out = _run([_exe("ffmpeg"), "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                        "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy", *sound, "-movflags", "+faststart",
                        str(dest)], timeout=900)
        finally:
            listing.unlink(missing_ok=True)
        if out.returncode == 0 and dest.is_file() and abs(probe(dest).duration_s - total) < 1.0:
            return dest
        dest.unlink(missing_ok=True)
    a = probes[0]
    w, h, fps = a.width or 1920, a.height or 1080, a.fps or 30
    sound = all(p.acodec for p in probes)         # one silent part: the joined clip is silent
    cmd, graph = [_exe("ffmpeg"), "-v", "error", "-y"], []
    for i, p in enumerate(parts):
        cmd += ["-i", str(p)]
        graph.append(f"[{i}:v:0]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                     f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps:g},format=yuv420p[v{i}]")
        if sound:
            graph.append(f"[{i}:a:0]aresample=48000:async=1:first_pts=0,aformat=channel_layouts=stereo[a{i}]")
    pads = "".join(f"[v{i}]" + (f"[a{i}]" if sound else "") for i in range(len(parts)))
    graph.append(f"{pads}concat=n={len(parts)}:v=1:a={int(sound)}[v]" + ("[a]" if sound else ""))
    cmd += ["-filter_complex", ";".join(graph), "-map", "[v]"] + (["-map", "[a]", "-c:a", "aac", "-b:a", "192k"] if sound else [])
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "16", "-movflags", "+faststart", str(dest)]
    out = _run(cmd, timeout=max(1800.0, total * 20))
    if out.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        raise MediaError(f"ffmpeg could not join the {len(parts)} parts: {out.stderr.strip()[-300:]}")
    return dest


def grab_frame(src: Path, t: float, dest: Path, max_height: int = 720) -> bool:
    """Save one JPEG at time t (seconds into the clip), scaled down to max_height."""
    out = _run([_exe("ffmpeg"), "-v", "error", "-y", "-ss", f"{max(0.0, t):.3f}", "-i", str(src),
                "-frames:v", "1", "-vf", f"scale=-2:'min({max_height},ih)'", "-q:v", "3",
                str(dest)], timeout=60)
    return out.returncode == 0 and dest.is_file() and dest.stat().st_size > 0
