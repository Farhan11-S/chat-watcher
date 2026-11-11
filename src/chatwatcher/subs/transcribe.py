"""Speech to text on one downloaded clip with Whisper (faster-whisper, on this computer).

Writes two files into the clip folder:
    words.json       every line with every word's start and end (clip seconds): the source
                     for subtitles
    transcript.txt   the same lines as "[mm:ss.s] text", replacing the YouTube auto-caption
                     excerpt (kept as transcript.youtube.txt), so the editor, "Analyse again"
                     and "Ask Claude" read Whisper's text

The audio is decoded with ffmpeg and handed to Whisper as samples. The model is loaded
once per process and reused; it lives in <data_dir>/models."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from chatwatcher.config import Config

WORDS_FILE = "words.json"
TRANSCRIPT_FILE = "transcript.txt"
YOUTUBE_FILE = "transcript.youtube.txt"
RATE = 16000

# Lines Whisper is known to invent over music or silence.
_HALLUCINATIONS = re.compile(
    r"^(thank(s| you) (so much )?for watching|please (like and )?subscribe|subtitles? (by|made by)|"
    r"transcribed by|ご視聴ありがとうございました|terima kasih (telah|sudah) menonton)", re.I)


class TranscribeError(Exception):
    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind          # missing (faster-whisper not installed) | no_audio | error


@dataclass
class Line:
    start: float
    end: float
    text: str
    words: list[list[Any]] = field(default_factory=list)   # [start, end, word, probability]
    language: str = ""
    no_speech: float = 0.0
    logprob: float = 0.0


class Engine(Protocol):
    name: str

    def run(self, audio: Any, language: str | None) -> tuple[list[Line], str, float]:
        """(lines, detected language, how sure it is)"""


def available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


class Whisper:
    """faster-whisper on the CPU, int8. One model per process, shared by every caller."""
    _lock = threading.Lock()
    _models: dict[tuple[str, int], Any] = {}

    def __init__(self, cfg: Config):
        if not available():
            raise TranscribeError("Whisper isn't installed. Run `pip install faster-whisper` in the "
                                  "app's environment.", "missing")
        self.name = cfg.transcribe.model
        self.threads = cfg.transcribe.threads
        self.models_dir = Path(cfg.paths.data_dir) / "models"

    def _model(self):
        key = (self.name, self.threads)
        with self._lock:
            if key not in self._models:
                from faster_whisper import WhisperModel
                self.models_dir.mkdir(parents=True, exist_ok=True)
                self._models[key] = WhisperModel(self.name, device="cpu", compute_type="int8",
                                                 cpu_threads=self.threads, download_root=str(self.models_dir))
            return self._models[key]

    def run(self, audio, language):
        model = self._model()
        with self._lock:          # one transcription at a time: it already uses every core
            segs, info = model.transcribe(
                audio, language=language or None, multilingual=not language, word_timestamps=True,
                vad_filter=True, beam_size=5, condition_on_previous_text=False)
            lines = [Line(s.start, s.end, s.text.strip(),
                          [[round(w.start, 3), round(w.end, 3), w.word, round(w.probability, 3)] for w in s.words or []],
                          getattr(s, "language", None) or info.language, s.no_speech_prob, s.avg_logprob)
                     for s in segs]
        return lines, info.language, info.language_probability


def decode_audio(path: Path) -> Any:
    """The clip's sound as 16 kHz mono float samples."""
    import numpy as np
    from chatwatcher.clip.media import _exe
    try:
        raw = subprocess.run([_exe("ffmpeg"), "-v", "error", "-nostdin", "-i", str(path), "-vn", "-ac", "1",
                              "-ar", str(RATE), "-f", "f32le", "-"], capture_output=True, check=True,
                             timeout=600).stdout
    except subprocess.CalledProcessError as e:
        raise TranscribeError(f"ffmpeg couldn't read the clip's audio: {e.stderr.decode(errors='replace')[-300:]}")
    return np.frombuffer(raw, dtype=np.float32)


def keep(line: Line) -> bool:
    """False for lines Whisper most likely made up (over music, noise or silence)."""
    if not line.text or not line.words:
        return False
    if line.no_speech > 0.6 and line.logprob < -1.0:
        return False
    if _HALLUCINATIONS.match(line.text) and line.logprob < -0.5:
        return False
    return sum(w[3] for w in line.words) / len(line.words) >= 0.2


_END = re.compile(r"[.?!。？！]$")


def sentences(line: dict[str, Any]) -> list[tuple[float, str]]:
    """One Whisper line split at sentence ends, each with its first word's time. Lines
    can run 10-30 s; the editor's transcript list (where you start or end a cut at a
    line) reads better one sentence at a time."""
    out, cur = [], []
    for w in line.get("words") or []:
        cur.append(w)
        if _END.search(w[2].strip()):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    if len(out) <= 1:
        return [(line["start"], line["text"])]
    return [(ws[0][0], "".join(w[2] for w in ws).strip()) for ws in out if "".join(w[2] for w in ws).strip()]


def _ts(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


def transcribe_clip(cfg: Config, folder: Path, clip: Path, *, engine: Engine | None = None,
                    language: str | None = None, say: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Transcribe clip.mp4 into words.json and transcript.txt. Returns words.json's contents."""
    say = say or (lambda _m: None)
    engine = engine or Whisper(cfg)
    t0 = time.monotonic()
    audio = decode_audio(clip)
    if len(audio) < RATE // 2:
        raise TranscribeError("The clip has no audio to transcribe.", "no_audio")
    say(f"Whisper {engine.name} on {len(audio) / RATE:.0f}s of audio…")
    lines, lang, sure = engine.run(audio, language or cfg.transcribe.language or None)
    kept = [ln for ln in lines if keep(ln)]
    data = {
        "model": engine.name, "language": lang, "language_p": round(float(sure or 0), 3),
        "duration_s": round(len(audio) / RATE, 3), "took_s": round(time.monotonic() - t0, 1),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "dropped": len(lines) - len(kept),
        "lines": [{"start": round(ln.start, 3), "end": round(ln.end, 3), "text": ln.text, "language": ln.language,
                   "words": ln.words} for ln in kept],
    }
    words, transcript = folder / WORDS_FILE, folder / TRANSCRIPT_FILE
    if transcript.is_file() and not words.is_file():
        transcript.replace(folder / YOUTUBE_FILE)        # the auto-caption excerpt, kept for reference
    try:                                                 # Whisper's word times are loose: pull them onto the speech
        from chatwatcher.subs import align
        say("Lining the words up with the speech…")
        data = align.write(folder, data, audio)
    except Exception:                                    # noqa: BLE001 — no VAD: keep Whisper's times
        tmp = folder / (WORDS_FILE + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(words)
    transcript.write_text("".join(f"[{_ts(t)}] {text}\n" for ln in data["lines"] for t, text in sentences(ln)),
                          encoding="utf-8")
    return data


def info(folder: Path) -> dict[str, Any] | None:
    """What's in words.json (without the lines), or None if the clip isn't transcribed."""
    p = folder / WORDS_FILE
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return {k: d.get(k) for k in ("model", "language", "language_p", "duration_s", "took_s", "created_at", "dropped")} \
        | {"lines": len(d.get("lines") or [])}


def forget(folder: Path) -> None:
    """The clip was downloaded again: its old words no longer match."""
    for name in (WORDS_FILE, YOUTUBE_FILE, "words.whisper.json"):
        (folder / name).unlink(missing_ok=True)
