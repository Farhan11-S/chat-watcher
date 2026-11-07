"""Caption track selection and parsing (json3 preferred, WebVTT fallback)."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Segment:
    start: float
    end: float
    text: str

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def choose_track(info: dict[str, Any], prefs: list[str]) -> tuple[str, bool] | None:
    """Pick one caption track from yt-dlp metadata. Returns (lang key, is_auto) or None.

    Order: manual subs in a preferred language → the original-language auto track
    (yt-dlp marks it '<lang>-orig') → auto track in the video's language → auto track
    in a preferred language. Auto *translations* are skipped where possible: they're
    machine-translated from the ASR track and much worse.
    """
    manual = [k for k in (info.get("subtitles") or {}) if k != "live_chat"]
    auto = list(info.get("automatic_captions") or {})

    def match(keys: list[str], lang: str) -> str | None:
        for k in keys:
            if k == lang or k.startswith(f"{lang}-"):
                return k
        return None

    for lang in prefs:
        if k := match(manual, lang):
            return k, False
    language = info.get("language")
    # A video can list several '-orig' tracks (e.g. 'ja-orig' plus an AI-dub 'en-orig');
    # the one matching the video's spoken language is the real ASR track.
    orig = [k for k in auto if k.endswith("-orig")]
    for lang in ([language] if language else []) + prefs:
        if f"{lang}-orig" in orig:
            return f"{lang}-orig", True
    if orig:
        return orig[0], True
    if language and language in auto:
        return language, True
    for lang in prefs:
        if lang in auto:
            return lang, True
    return None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("​", "")).strip()


def _finish(segments: list[Segment]) -> list[Segment]:
    """Sort, drop empties/duplicates, and stop segments overlapping the next one
    (auto captions keep each line on screen while the next appears)."""
    segments = sorted((s for s in segments if s.text), key=lambda s: s.start)
    out: list[Segment] = []
    for s in segments:
        if out and s.text == out[-1].text and s.start <= out[-1].end + 0.5:
            out[-1].end = max(out[-1].end, s.end)
            continue
        out.append(s)
    for cur, nxt in zip(out, out[1:]):
        if nxt.start > cur.start and cur.end > nxt.start:
            cur.end = nxt.start
    return out


def parse_json3(data: dict[str, Any]) -> list[Segment]:
    segs: list[Segment] = []
    for ev in data.get("events", []):
        if "segs" not in ev:
            continue
        text = _clean("".join(s.get("utf8", "") for s in ev["segs"]))
        if not text:
            continue
        start = ev.get("tStartMs", 0) / 1000
        segs.append(Segment(round(start, 3), round(start + ev.get("dDurationMs", 0) / 1000, 3), text))
    return _finish(segs)


_VTT_TIME = re.compile(
    r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})\s+-->\s+(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})"
)
_VTT_TAG = re.compile(r"<[^>]+>")


def _secs(h: str | None, m: str, s: str, ms: str) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_vtt(text: str) -> list[Segment]:
    """WebVTT → segments. Auto-caption VTT repeats the previous line in every cue,
    so only lines that differ from the last emitted line are kept."""
    segs: list[Segment] = []
    last_line = ""
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = block.strip().split("\n")
        for i, line in enumerate(lines):
            m = _VTT_TIME.search(line)
            if not m:
                continue
            start, end = _secs(*m.groups()[:4]), _secs(*m.groups()[4:])
            for raw in lines[i + 1:]:
                clean = _clean(_VTT_TAG.sub("", raw))
                if clean and clean != last_line:
                    segs.append(Segment(round(start, 3), round(end, 3), clean))
                    last_line = clean
            break
    return _finish(segs)


def parse_caption_file(path: str | Path) -> list[Segment]:
    path = Path(path)
    content = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".json3":
        return parse_json3(json.loads(content))
    if path.suffix == ".vtt":
        return parse_vtt(content)
    raise ValueError(f"Unsupported caption format: {path.name}")


def write_transcript(path: str | Path, segments: list[Segment]) -> None:
    path = Path(path)
    with path.open("w", encoding="utf-8") as f:
        for s in segments:
            f.write(json.dumps(s.to_json(), ensure_ascii=False) + "\n")


def read_transcript(path: str | Path) -> list[Segment]:
    with Path(path).open(encoding="utf-8") as f:
        return [Segment(**json.loads(line)) for line in f if line.strip()]
