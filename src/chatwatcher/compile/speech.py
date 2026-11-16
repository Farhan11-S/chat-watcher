"""The whole stream's speech from its audio, with Whisper on this computer ("Listen first").

YouTube's auto-captions are often missing, late or badly misheard on VTuber streams; this is
what the stream read (compile.service) reads instead when it is there. No Claude tokens.

The audio is downloaded once (audio only, small) into <vod>/listen/, then transcribed
cfg.compile.part_s at a time. Each finished part is saved there as it is done, so a restart,
or giving way to a render or another queued job, carries on from the next part. When every
part is done they are joined into speech.jsonl (the same lines as the captions'
transcript.jsonl), and the audio and the parts are deleted.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from chatwatcher.config import Config
from chatwatcher.ingest.captions import Segment, read_transcript, write_transcript
from chatwatcher.paths import VodFiles

PAD_S = 5.0                     # each part hears this much of the next, to finish its last sentence
META = "speech.json"            # in the listen folder: what made speech.jsonl
RATE = 16000


class SpeechError(Exception):
    """kind: missing (no Whisper) | no_audio | download | error"""

    def __init__(self, message: str, kind: str = "error", retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


def stream_transcript(files: VodFiles) -> tuple[list[Segment], str]:
    """The best text of what was said in the stream: (segments, "whisper" | "captions" | "")."""
    if files.speech.is_file():
        return read_transcript(files.speech), "whisper"
    if files.transcript.is_file():
        return read_transcript(files.transcript), "captions"
    return [], ""


def parts_total(duration: float, part_s: int) -> int:
    return max(1, math.ceil(duration / part_s)) if duration > 0 else 0


def progress(files: VodFiles, duration: float, part_s: int) -> dict[str, Any]:
    """For the stream page: {done, total, finished, meta}."""
    total = parts_total(duration, part_s)
    if files.speech.is_file():
        meta = {}
        try:
            meta = json.loads((files.listen / META).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        return {"done": total, "total": total, "finished": True, "meta": meta}
    done = len(list(files.listen.glob("part_*.json"))) if files.listen.is_dir() else 0
    return {"done": min(done, total), "total": total, "finished": False, "meta": {}}


def decode(audio: Path, start: float, length: float) -> Any:
    """length seconds of the audio from start, as 16 kHz mono float samples."""
    import numpy as np
    from chatwatcher.clip.media import _exe
    try:
        raw = subprocess.run([_exe("ffmpeg"), "-v", "error", "-nostdin", "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
                              "-i", str(audio), "-vn", "-ac", "1", "-ar", str(RATE), "-f", "f32le", "-"],
                             capture_output=True, check=True, timeout=900).stdout
    except subprocess.CalledProcessError as e:
        raise SpeechError(f"ffmpeg couldn't read the stream's audio: {e.stderr.decode(errors='replace')[-300:]}")
    return np.frombuffer(raw, dtype=np.float32)


def part_lines(lines: list[Any], start: float, keep_until: float | None) -> list[list[Any]]:
    """Whisper's lines for one part → [start, end, text] in stream time: invented lines
    dropped (subs.transcribe.keep / echo), and the lines that start in the next part's
    time (heard only so this part's last sentence ends) left to that part."""
    from chatwatcher.subs.transcribe import echo, keep
    out, kept = [], []
    for ln in lines:
        if not keep(ln) or echo(ln, kept[-1] if kept else None):
            continue
        kept.append(ln)
        if keep_until is not None and ln.start >= keep_until:
            continue
        out.append([round(start + ln.start, 2), round(start + ln.end, 2), " ".join(ln.text.split())])
    return out


def transcribe_stream(cfg: Config, files: VodFiles, video_id: str, duration: float, *, ytdlp: Any = None,
                      engine: Any = None, should_pause: Callable[[], bool] = lambda: False,
                      say: Callable[[str], None] = lambda _m: None) -> bool:
    """Make speech.jsonl. True when it is there; False when it stopped to let other work
    go first (call again to carry on). should_pause is asked between parts."""
    if files.speech.is_file():
        return True
    if duration <= 0:
        raise SpeechError("The stream's length is unknown, so it can't be split into parts.")
    c = cfg.compile
    total = parts_total(duration, c.part_s)
    folder = files.listen
    folder.mkdir(parents=True, exist_ok=True)
    todo = [i for i in range(total) if not (folder / f"part_{i:04d}.json").is_file()]
    if todo:
        audio = _audio(cfg, folder, video_id, ytdlp, say)
        if engine is None:
            from chatwatcher.subs.transcribe import TranscribeError, Whisper
            try:
                engine = Whisper(cfg, model=c.whisper_model or None, beam_size=c.beam_size)
            except TranscribeError as e:
                raise SpeechError(str(e), e.kind) from e
        language = cfg.transcribe.language or None
        for n, i in enumerate(todo):
            if n and should_pause():
                say(f"{total - len(todo) + n} of {total} parts done; giving way to other work")
                return False
            t0 = time.monotonic()
            start = i * c.part_s
            last = i == total - 1
            samples = decode(audio, start, c.part_s + (0 if last else PAD_S))
            lines, lang, _sure = engine.run(samples, language) if len(samples) >= RATE // 2 else ([], "", 0)
            rows = part_lines(lines, start, None if last else c.part_s)
            tmp = folder / f"part_{i:04d}.tmp"
            tmp.write_text(json.dumps({"start": start, "language": lang, "took_s": round(time.monotonic() - t0, 1),
                                       "lines": rows}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(folder / f"part_{i:04d}.json")
            say(f"part {i + 1}/{total}: {len(rows)} lines in {time.monotonic() - t0:.0f}s")
    _join(cfg, files, total, getattr(engine, "name", "") or (c.whisper_model or cfg.transcribe.model))
    return True


def _audio(cfg: Config, folder: Path, video_id: str, ytdlp: Any, say: Callable[[str], None]) -> Path:
    from chatwatcher.ingest.ytdlp import YtDlp, YtDlpError, audio_file
    found = audio_file(folder)
    if found is not None:
        return found
    say("downloading the stream's audio…")
    try:
        yt = ytdlp or YtDlp(cfg.youtube)
        path = yt.download_audio(video_id, folder, cfg.compile.audio_format)
    except YtDlpError as e:
        raise SpeechError(f"Couldn't download the stream's audio: {e}", "download", e.retryable) from e
    say(f"audio: {path.stat().st_size / 1_048_576:.0f} MB")
    return path


def _join(cfg: Config, files: VodFiles, total: int, model: str) -> None:
    folder = files.listen
    segments: list[Segment] = []
    took, langs = 0.0, {}
    for i in range(total):
        d = json.loads((folder / f"part_{i:04d}.json").read_text(encoding="utf-8"))
        took += d.get("took_s") or 0
        if d.get("language"):
            langs[d["language"]] = langs.get(d["language"], 0) + 1
        segments += [Segment(float(a), float(b), t) for a, b, t in d["lines"] if t]
    tmp = files.speech.with_suffix(".tmp")
    write_transcript(tmp, segments)
    tmp.replace(files.speech)
    (folder / META).write_text(json.dumps({
        "model": model, "beam_size": cfg.compile.beam_size, "lines": len(segments), "took_s": round(took),
        "language": max(langs, key=langs.get) if langs else "",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())}), encoding="utf-8")
    for p in folder.iterdir():
        if p.name.startswith("part_") or (p.name.startswith("audio.") and not cfg.compile.keep_audio):
            p.unlink(missing_ok=True) if p.is_file() else shutil.rmtree(p, ignore_errors=True)


def refresh_excerpts(conn: Any, video_pk: int, segments: list[Segment]) -> int:
    """Rewrite transcript.txt in the stream's preview and clip folders from the whole-stream
    transcript, so their next analysis reads Whisper's text instead of the captions'. A clip
    Whisper transcribed on its own (words.json) is left alone: that one is better still.
    Returns how many folders changed."""
    from chatwatcher.clip.package import transcript_lines, write_lines
    from chatwatcher.subs.transcribe import TRANSCRIPT_FILE, WORDS_FILE
    n = 0
    for r in conn.execute(
            """SELECT p.folder FROM previews p JOIN moments m ON m.id = p.moment_id WHERE m.video_id = ? AND m.kind != 'compilation'
               UNION SELECT c.folder FROM clips c JOIN moments m ON m.id = c.moment_id WHERE m.video_id = ? AND m.kind != 'compilation'""",
            (video_pk, video_pk)):
        folder = Path(r["folder"] or "")
        if not r["folder"] or not (folder / "moment.json").is_file() or (folder / WORDS_FILE).is_file():
            continue
        try:
            clip = json.loads((folder / "moment.json").read_text(encoding="utf-8"))["clip"]
            write_lines(folder / TRANSCRIPT_FILE, transcript_lines(segments, float(clip["zero_s"]), float(clip["duration_s"])))
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            continue
        n += 1
    return n


def forget(files: VodFiles) -> None:
    """Throw the whole-stream transcript away (to listen again from scratch)."""
    files.speech.unlink(missing_ok=True)
    shutil.rmtree(files.listen, ignore_errors=True)
