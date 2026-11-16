"""A clip's YouTube description in one fixed, formal format.

Claude writes only two parts in the analysis (prompts.MOMENT_SCHEMA): `yt_hook`, the one
funny opening sentence, and `yt_summary`, a short formal account of the moment. Everything
else (the source stream link at the moment's time, the talent's channel, the credits that
COVER's derivative-work guidelines ask for, the hashtags) is filled in here from what the
app already knows, so every clip's description has the same shape. It is built when the
page is shown, so a change to the format reaches old clips too; an analysis from before
the hook existed falls back to its summary.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from typing import Any

from chatwatcher.subs import speakers

RULE = "━━━━━━━━━━━━━━━━━━━━"
GUIDELINES_URL = "https://hololivepro.com/en/terms/"
MAX_HASHTAGS = 6


def stamp(seconds: float) -> str:
    s = max(0, int(seconds))
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def watch_url(video_id: str, seconds: float | None = None) -> str:
    url = f"https://www.youtube.com/watch?v={video_id}"
    return f"{url}&t={max(0, int(seconds))}s" if seconds is not None else url


def channel_url(channel: dict[str, Any] | None) -> str:
    if not channel:
        return ""
    if channel.get("handle"):
        h = channel["handle"]
        return f"https://www.youtube.com/{h if h.startswith('@') else '@' + h}"
    return f"https://www.youtube.com/channel/{channel['channel_id']}" if channel.get("channel_id") else ""


def stream_date(published_at: str | None) -> str:
    try:
        d = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return f"{d:%B} {d.day}, {d.year}"


def hashtag(text: str) -> str:
    """"Kobo Kanaeru" → "#KoboKanaeru"; "" when nothing usable is left."""
    words = re.findall(r"\w+", str(text or ""))
    tag = "".join(w[:1].upper() + w[1:] if w.isascii() else w for w in words)
    return f"#{tag}" if tag and len(tag) <= 30 else ""


def talent_name(channel_title: str | None, master: dict[str, dict[str, str]]) -> str:
    """The VTuber a channel belongs to: "Kobo Kanaeru Ch. hololive-ID" → "Kobo Kanaeru"."""
    title = str(channel_title or "").strip()
    if not title:
        return ""
    base = re.split(r"\s*(?:\bCh\.|\bhololive\b|\bholo(?:EN|ID|DEV_IS)\b|[【\[(|/])", title, maxsplit=1,
                    flags=re.I)[0].strip(" -・")
    hit = speakers.find(master, base) if base else None
    if hit:
        return hit["name"]
    low = title.lower()
    inside = [v["name"] for k, v in master.items() if k and k in low]
    return max(inside, key=len) if inside else (base or title)


def build(a: dict[str, Any], *, video: dict[str, Any], moment: dict[str, Any],
          channel: dict[str, Any] | None = None, talent: str = "", featuring: list[str] | None = None,
          hololive: bool = True) -> str:
    """The description. `a` is the analysis; `moment` its row (with `compilation` for a
    compilation); `featuring`: the other VTubers heard in the clip."""
    talent = talent or (channel or {}).get("title") or "the streamer"
    featuring = [f for f in dict.fromkeys(featuring or []) if f and f != talent]
    hook = " ".join(str(a.get("yt_hook") or "").split())
    summary = " ".join(str(a.get("yt_summary") or a.get("summary") or "").split())
    out = [x for x in (hook, summary) if x]
    if featuring:
        out.append(f"Featuring: {', '.join(featuring)}")

    vid = video.get("video_id") or ""
    src = [RULE, "▶ Original stream", str(video.get("title") or vid)]
    segs = ((moment.get("compilation") or {}).get("segments") or []) if moment.get("kind") == "compilation" else []
    if segs:
        src.append(watch_url(vid))
        src.extend(f"{stamp(s['start'])} {watch_url(vid, s['start'])}" for s in segs)
    else:
        src.append(watch_url(vid, moment.get("start_s")))
    if date := stream_date(video.get("published_at")):
        src.append(f"Streamed on {date}")
    src.append("")
    src.append(f"▶ {talent}")
    if url := channel_url(channel):
        src.append(url)
    out.append("\n".join(src))

    credits = [RULE, "This is an unofficial fan-made clip."]
    if hololive:
        credits += [f"All rights to the original content belong to {talent} and COVER Corporation "
                    "(hololive production).",
                    "This clip follows the hololive production Derivative Works Guidelines:", GUIDELINES_URL]
    else:
        credits.append(f"All rights to the original content belong to {talent}.")
    credits.append("Please support the talent by watching the full stream.")
    out.append("\n".join(credits))

    tags = ["#hololive"] if hololive else []
    for t in [talent, *featuring, *(a.get("tags") or [])]:
        h = hashtag(t)
        if h and h.lower() not in (x.lower() for x in tags):
            tags.append(h)
    if tags:
        out.append(" ".join(tags[:MAX_HASHTAGS]))
    return "\n\n".join(out)


def for_moment(conn: sqlite3.Connection, moment: dict[str, Any], video: dict[str, Any],
               analysis: dict[str, Any] | None) -> str:
    """The description for a moment's page ("" before it has an analysis)."""
    if not analysis:
        return ""
    ch = conn.execute("SELECT channel_id, handle, title FROM channels WHERE id = ?",
                      (video.get("channel_id"),)).fetchone() if video.get("channel_id") else None
    channel = dict(ch) if ch else None
    master = speakers.vtubers(conn)
    talent = talent_name(channel and channel["title"], master)
    voices = speakers.get(conn, moment["id"]) if moment.get("id") is not None else {}
    featuring = [v["name"] for v in voices.values()
                 if v.get("name") and speakers.find(master, v["name"]) and v["name"] != talent]
    hololive = "hololive" in str((channel or {}).get("title") or "").lower() or \
        speakers.find(master, talent) is not None
    return build(analysis, video=video, moment=moment, channel=channel, talent=talent,
                 featuring=featuring, hololive=hololive)

