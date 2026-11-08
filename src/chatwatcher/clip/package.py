"""Output folder naming, still frames, and the text files that go next to each clip."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any

from chatwatcher.clip.download import ClipFile
from chatwatcher.clip.media import grab_frame
from chatwatcher.ingest.captions import Segment
from chatwatcher.ingest.chat_parser import ChatMessage
from chatwatcher.scoring.moments import fmt_ts, sentences

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def slug(text: str, max_len: int = 50) -> str:
    """Folder-safe name that keeps Japanese text (works on Linux, Windows and Drive)."""
    t = unicodedata.normalize("NFKC", text or "")
    t = _BAD.sub("", t)
    t = re.sub(r"\s+", "_", t.strip())
    t = re.sub(r"_+", "_", t)[:max_len].strip("._ ")
    return t or "untitled"


def video_dir(output_root: Path, info: dict[str, Any], video_id: str,
              tz: tzinfo | None = timezone.utc) -> Path:
    """output/<channel>/<date>_<id>_<title>; the date is the stream's start in tz."""
    channel = slug(info.get("channel") or info.get("uploader_id") or "unknown", 40)
    ts = info.get("release_timestamp") or info.get("timestamp")
    if ts:
        day = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz).strftime("%Y-%m-%d")
    else:
        d = info.get("upload_date") or ""
        day = f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else "undated"
    return output_root / channel / f"{day}_{video_id}_{slug(info.get('title') or '', 40)}"


def _hms(seconds: float) -> str:
    s = int(seconds)
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f"{h}h{m:02d}m{sec:02d}s" if h else f"{m:02d}m{sec:02d}s"


def moment_dirname(rank: int, kind: str, reasons: list[dict], start_s: float) -> str:
    label = "song" if kind == "song" else (reasons[0]["signal"] if reasons else "moment")
    return f"{rank:02d}_{label}_{_hms(start_s)}"


# ── frames ───────────────────────────────────────────────────────────────────

def extract_frames(clip: ClipFile, out_dir: Path, n: int, peak_s: float | None = None) -> list[dict]:
    """n evenly spaced stills plus one at the chat peak. Times are recorded both in clip
    time and stream time so later steps can line them up with chat and transcript."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.jpg"):
        old.unlink()
    dur = clip.duration_s
    times = [dur * (i + 0.5) / n for i in range(n)]
    if peak_s is not None:
        pt = clip.to_clip_time(peak_s)
        if 0 <= pt < dur and all(abs(pt - t) > 1.0 for t in times):
            times.append(pt)
    frames = []
    for idx, t in enumerate(sorted(times), 1):
        name = f"{idx:02d}_{_hms(t)}.jpg"
        if grab_frame(clip.path, t, out_dir / name):
            frames.append({"file": f"{out_dir.name}/{name}", "clip_s": round(t, 2),
                           "stream_s": round(clip.zero_s + t, 2),
                           "is_peak": peak_s is not None and abs(clip.zero_s + t - peak_s) < 1.0})
    return frames


# ── text files ───────────────────────────────────────────────────────────────

def _clip_ts(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


def write_chat_excerpt(path: Path, messages: list[ChatMessage], clip: ClipFile) -> int:
    """Every chat message during the clip, timed in clip time."""
    end = clip.zero_s + clip.duration_s
    lines = []
    for m in messages:
        if clip.zero_s <= m.t <= end:
            tag = {"superchat": f" [SC {m.amount_text or ''}]", "sticker": f" [sticker {m.amount_text or ''}]",
                   "member_new": " [new member]", "member_milestone": " [member milestone]",
                   "gift": f" [gifted {m.gift_count}]", "jewel_gift": " [gift]"}.get(m.kind, "")
            role = " (streamer)" if m.is_owner else " (mod)" if m.is_mod else ""
            lines.append(f"[{_clip_ts(clip.to_clip_time(m.t))}] {m.author}{role}{tag}: {m.text}")
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return len(lines)


def write_transcript_excerpt(path: Path, transcript: list[Segment], clip: ClipFile) -> int:
    end = clip.zero_s + clip.duration_s
    sents = [s for s in sentences(transcript) if s.end > clip.zero_s and s.start < end]
    lines = [f"[{_clip_ts(clip.to_clip_time(s.start))}] {s.text}" for s in sents]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return len(lines)


def write_moment_json(path: Path, *, video_id: str, title: str, stream_type: str,
                      moment: dict[str, Any], clip: ClipFile, frames: list[dict]) -> None:
    data = {
        "video_id": video_id, "title": title, "stream_type": stream_type,
        "rank": moment["rank"], "kind": moment["kind"], "score": moment["score"],
        "youtube_url": f"https://youtu.be/{video_id}?t={int(clip.zero_s)}",
        "stream_range": [fmt_ts(clip.zero_s), fmt_ts(clip.zero_s + clip.duration_s)],
        "clip": {"file": clip.path.name, "zero_s": clip.zero_s, "duration_s": clip.duration_s,
                 "resolution": f"{clip.width}x{clip.height}" if clip.width else None,
                 "fps": clip.fps, "vcodec": clip.vcodec, "acodec": clip.acodec,
                 "format_id": clip.format_id, "size_bytes": clip.size_bytes},
        "peak_clip_s": clip.to_clip_time(moment["peak_s"]),
        "reasons": moment.get("reasons", []),
        "top_chat": moment.get("top_messages", []),
        "frames": frames,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
