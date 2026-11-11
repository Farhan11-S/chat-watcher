"""Render an edit with ffmpeg: frame-exact cuts, a tiny fade at each cut, per-segment
volume, zooms, shakes and flashes, subtitles, sound effects, then the whole thing
levelled to a target loudness.

Each segment is encoded to its own file first and the files are joined without
re-encoding. (One big filter graph would have to hold decoded frames in memory when
segments play out of order, e.g. a cold open that shows the payoff first.) Audio stays
uncompressed until the last step, so the joins are sample-exact and never click.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from chatwatcher.clip.media import MediaError, _exe, probe
from chatwatcher.config import EditConfig
from chatwatcher.edit.motion import Visual, local, video_filter


@dataclass
class Rendered:
    path: Path
    duration_s: float
    size_bytes: int


@dataclass
class SoundCue:
    """One sound effect, placed on the edit's own timeline."""
    at: float                  # seconds into the edit
    path: Path
    gain_db: float = 0.0       # 0 = about as loud as the clip's talking
    duck_db: float = 0.0       # lower the clip's sound by this much while it plays
    length_s: float = 1.0


SOUND_LEVEL = -14.0            # sounds in the library are levelled to this (see edit.sounds)
_DUCK_RAMP = 0.06
JOIN_FADE = 0.008         # fade at a split that isn't a cut (no click, no dip)


def lengths(segments: list[list[float]], fps: float) -> list[float]:
    """Each segment's length in the edit: whole frames."""
    fps = fps or 30.0
    return [max(1, round((e - s) * fps)) / fps for s, e, *_ in segments]


def place(t: float, segments: list[list[float]], fps: float) -> list[float]:
    """Where clip time t lands in the edit (once per segment that contains it)."""
    out, offset = [], 0.0
    for (s, _e, *_), length in zip(segments, lengths(segments, fps)):
        if s <= t < s + length:
            out.append(round(offset + t - s, 4))
        offset += length
    return out


def _mix_graph(cues: list[SoundCue], first_input: int, offset_db: float, total: float) -> str:
    """[0:a] with the sounds mixed in → [mix]. Under each sound with duck_db < 0, the
    clip's own sound is lowered, with short ramps so it doesn't click."""
    factors = []
    for c in cues:
        if c.duck_db < 0:
            g = 10 ** (c.duck_db / 20)
            a, b, r = c.at, min(c.at + c.length_s, total), _DUCK_RAMP
            factors.append(f"(1-{1 - g:.4f}*clip((t-{a - r:.3f})/{r},0,1)*clip(({b + r:.3f}-t)/{r},0,1))")
    main = "[0:a]" + (f"volume='{'*'.join(factors)}':eval=frame" if factors else "anull") + "[main]"
    parts, labels = [main], ["[main]"]
    for k, c in enumerate(cues):
        vol = max(-40.0, min(12.0, c.gain_db + offset_db))
        ms = int(round(c.at * 1000))
        parts.append(f"[{first_input + k}:a]aresample=48000,aformat=channel_layouts=stereo,"
                     f"volume={vol:.2f}dB,adelay={ms}:all=1[s{k}]")
        labels.append(f"[s{k}]")
    parts.append("".join(labels) + f"amix=inputs={len(labels)}:normalize=0:duration=first:dropout_transition=0[mix]")
    return ";".join(parts)


def _ffmpeg(args: list[str], *, timeout: float, total_s: float = 0.0,
            on_progress: Callable[[float], None] | None = None, cwd: Path | None = None) -> str:
    """Run ffmpeg; report 0..1 of total_s as it goes. Returns stderr."""
    cmd = [_exe("ffmpeg"), "-hide_banner", "-nostdin", "-y", *args]
    if on_progress and total_s > 0:
        cmd[1:1] = ["-progress", "pipe:1", "-nostats"]
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True, cwd=cwd)
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if on_progress and line.startswith("out_time_us="):
                    try:
                        on_progress(min(1.0, max(0.0, int(line[12:]) / 1e6 / total_s)))
                    except ValueError:
                        pass
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as e:
            proc.kill()
            raise MediaError(f"ffmpeg timed out after {timeout:.0f}s") from e
        err.seek(0)
        stderr = err.read()
    if proc.returncode != 0:
        tail = "\n".join(stderr.strip().splitlines()[-4:])
        raise MediaError(f"ffmpeg failed: {tail[:500]}")
    return stderr


def _measure_loudness(inputs: list[str], target: float, timeout: float, graph: str | None = None) -> dict[str, str] | None:
    """loudnorm's first pass over the first input's audio, or over [mix] of `graph`.
    None if the audio is silent (nothing to level)."""
    measure = f"loudnorm=I={target}:TP=-1.5:LRA=11:print_format=json"
    args = ["-v", "info", *inputs]
    args += (["-filter_complex", f"{graph};[mix]{measure}[out]", "-map", "[out]"] if graph
             else ["-vn", "-af", measure])
    err = _ffmpeg(args + ["-f", "null", "-"], timeout=timeout)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", err)
    if not m:
        return None
    d = json.loads(m.group(0))
    try:
        if float(d["input_i"]) < -70:          # -inf or near-silent
            return None
    except (ValueError, KeyError):
        return None
    return d


def render(cfg: EditConfig, src: Path, segments: list[list[float]], dest: Path, *, fps: float,
           has_audio: bool = True, sounds: list[SoundCue] | None = None,
           visuals: list[Visual] | None = None, subs: list[dict] | None = None,
           sub_style: dict | None = None, size: tuple[int, int] = (0, 0), font: str | None = None,
           intro: Path | None = None, intro_s: float = 0.0, layout: str | list[str] | None = None, title: str = "",
           on_progress: Callable[[float], None] | None = None) -> Rendered:
    """segments: [[start, end, gain_db], …] in src time, in play order. sounds: effects
    placed on the edit's timeline (ignored if the clip has no audio). visuals: zooms,
    shakes and flashes in src time, so they stay on the same moment wherever it plays.
    subs: subtitle cues in src time, burned in over the picture (after any zoom), in
    sub_style; size: the video's width and height, for the subtitles' scale. intro: a video
    played first (scaled to size, re-encoded like the segments), intro_s of it; sounds
    must already be placed after it. layout: an ffmpeg graph from [0:v] to [base] that
    builds a Short's picture (edit.layout.graph); size is then the Short's, the visuals are
    in its frame, and title shows in its top band. layout and visuals may instead be lists
    with one entry per segment (a Short whose picture changes between parts)."""
    from chatwatcher.subs import lines
    if not segments:
        raise MediaError("nothing to render")
    fps = fps or 30.0
    report = on_progress or (lambda _f: None)
    tmp = dest.parent / f".render_{dest.stem}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    fade = cfg.fade_ms / 1000
    try:
        # Whole frames per segment, and audio cut to exactly the same length, so sound and
        # picture can't drift apart over many cuts.
        seg_lengths = lengths(segments, fps)
        intro_s = max(1, round(intro_s * fps)) / fps if intro else 0.0
        total = sum(seg_lengths) + intro_s
        encode_share = 0.85 if has_audio and cfg.loudness_lufs else 0.97
        done = 0.0
        parts = []
        if intro:
            part = tmp / "intro.mkv"
            w, h = size if size[0] and size[1] else (probe(src).width, probe(src).height)
            args = ["-v", "error", "-i", str(intro), "-t", f"{intro_s:.6f}", "-map", "0:v:0",
                    "-vf", f"scale={w}:{h}:flags=lanczos,setsar=1,fps={fps:.6g}",
                    "-c:v", "libx264", "-preset", cfg.preset, "-crf", str(cfg.crf), "-pix_fmt", "yuv420p"]
            if has_audio:
                args += ["-map", "0:a:0", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
                         "-af", f"apad,atrim=0:{intro_s:.6f}"]
            _ffmpeg(args + [str(part)], timeout=600)
            parts.append(part)
            done = intro_s
            report(done / total * encode_share)
        per_layout = isinstance(layout, list)
        per_visual = bool(visuals) and isinstance(visuals[0], list)
        for i, ((s, _e, gain), length) in enumerate(zip(segments, seg_lengths)):
            part = tmp / f"part{i:03d}.mkv"
            seg_layout = layout[i] if per_layout else layout
            seg_visuals = (visuals[i] if per_visual else visuals) or []
            # Parts that play on from the one before (a split, not a cut) only get a tiny fade.
            joined_before = i > 0 and abs(segments[i - 1][1] - s) < 0.002
            joined_after = i + 1 < len(segments) and abs(segments[i + 1][0] - _e) < 0.002
            args = ["-v", "error", "-ss", f"{s:.3f}", "-i", str(src), "-t", f"{length:.6f}",
                    "-map", "0:v:0", "-c:v", "libx264", "-preset", cfg.preset, "-crf", str(cfg.crf),
                    "-pix_fmt", "yuv420p"]
            vf = video_filter(local(seg_visuals, s, length), fps, length)
            shown = lines.local(subs or [], s, length)
            if shown or title:       # relative name: ffmpeg runs in tmp, so no path escaping in the filter
                (tmp / f"part{i:03d}.ass").write_text(
                    lines.ass(shown, sub_style or {}, *size, font=font or lines.FONT, title=title, title_until=length),
                    encoding="utf-8")
                vf = (vf + "," if vf else "") + f"ass=part{i:03d}.ass"
            script = tmp / f"part{i:03d}.vf"      # in a file: the expressions can be longer than a command line allows
            if seg_layout:
                script.write_text(f"{seg_layout};[base]{vf or 'null'}[v]", encoding="utf-8")
                args[args.index("0:v:0")] = "[v]"
                args += ["-/filter_complex", str(script)]
            elif vf:
                script.write_text(vf, encoding="utf-8")
                args += ["-/vf", str(script)]
            if has_audio:
                f = min(fade, length / 3)
                fi, fo = (min(f, JOIN_FADE) if joined_before else f), (min(f, JOIN_FADE) if joined_after else f)
                chain = [f"volume={gain}dB"] if gain else []
                if fi > 0:
                    chain.append(f"afade=t=in:d={fi:.3f}")
                if fo > 0:
                    chain.append(f"afade=t=out:st={length - fo:.6f}:d={fo:.3f}")
                args += ["-map", "0:a:0", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"]
                if chain:
                    args += ["-af", ",".join(chain)]
            base = done

            def seg_progress(frac: float, base: float = base, length: float = length) -> None:
                report((base + frac * length) / total * encode_share)

            _ffmpeg(args + [str(part)], timeout=max(300.0, length * 30), total_s=length,
                    on_progress=seg_progress, cwd=tmp)
            parts.append(part)
            done += length
            report(done / total * encode_share)

        listing = tmp / "parts.txt"
        listing.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
        joined = tmp / "joined.mkv"
        # Relative names, run inside tmp: the concat demuxer resolves the parts against the
        # list's path as if it were a URL, so a "#" (or "?") in a folder name breaks it.
        _ffmpeg(["-v", "error", "-f", "concat", "-safe", "0", "-i", listing.name, "-c", "copy", joined.name], cwd=tmp,
                timeout=600)

        out = tmp / "out.mp4"
        slow = max(300.0, total * 5)
        cues = [c for c in (sounds or []) if c.path.is_file() and 0 <= c.at < total] if has_audio else []
        inputs = ["-i", str(joined)]
        for c in cues:
            inputs += ["-i", str(c.path)]
        final = ["-v", "error", *inputs]
        if has_audio:
            graph = None
            if cues:
                # Sounds are levelled to SOUND_LEVEL; move them to this clip's level, so gain 0
                # means "about as loud as her talking" whatever the stream's volume was.
                clip_level = _measure_loudness(["-i", str(joined)], -14.0, slow)
                offset = float(clip_level["input_i"]) - SOUND_LEVEL if clip_level else 0.0
                graph = _mix_graph(cues, 1, max(-20.0, min(10.0, offset)), total)
            level = _measure_loudness(inputs, cfg.loudness_lufs, slow, graph) if cfg.loudness_lufs else None
            report(0.93)
            chain = ""
            if level:
                chain = (f"loudnorm=I={cfg.loudness_lufs}:TP=-1.5:LRA=11"
                         f":measured_I={level['input_i']}:measured_TP={level['input_tp']}"
                         f":measured_LRA={level['input_lra']}:measured_thresh={level['input_thresh']}"
                         f":offset={level['target_offset']}:linear=true,aresample=48000")
            if graph:
                final += ["-filter_complex", f"{graph};[mix]{chain or 'anull'}[aout]", "-map", "0:v", "-map", "[aout]"]
            else:
                final += ["-map", "0"] + (["-af", chain] if chain else [])
            final += ["-c:a", "aac", "-b:a", cfg.audio_bitrate]
        else:
            final += ["-map", "0"]
        final += ["-c:v", "copy", "-movflags", "+faststart"]
        _ffmpeg(final + [str(out)], timeout=slow)
        out.replace(dest)
        report(1.0)
        pr = probe(dest)
        return Rendered(dest, pr.duration_s, pr.size_bytes)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
