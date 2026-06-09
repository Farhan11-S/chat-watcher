"""
api.py — YouTube chat fetching via yt-dlp (VOD replay) and YouTube Data API v3 (live streams)
"""

import json
import subprocess
import tempfile
import time
from pathlib import Path

import requests

YT_API_BASE = "https://www.googleapis.com/youtube/v3"


class YouTubeAPIError(Exception):
    pass


def get_stream_info(video_id: str, api_key: str) -> dict:
    """
    Fetch video metadata and determine if it's a live stream or VOD.
    Returns:
        {
            "title": str,
            "start_time_iso": str | None,
            "is_live": bool,
            "live_chat_id": str | None,   # only set for active live streams
        }
    """
    resp = requests.get(
        f"{YT_API_BASE}/videos",
        params={
            "part": "liveStreamingDetails,snippet",
            "id": video_id,
            "key": api_key,
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    items = data.get("items", [])
    if not items:
        raise YouTubeAPIError(f"Video not found or not accessible: {video_id}")

    item = items[0]
    snippet = item.get("snippet", {})
    details = item.get("liveStreamingDetails", {})

    # activeLiveChatId is only present during an ongoing live stream
    live_chat_id = details.get("activeLiveChatId")
    is_live = bool(live_chat_id)

    start_time = details.get("actualStartTime") or details.get("scheduledStartTime")

    return {
        "title": snippet.get("title", "Unknown"),
        "start_time_iso": start_time,
        "is_live": is_live,
        "live_chat_id": live_chat_id,
    }


# ── VOD replay via yt-dlp ─────────────────────────────────────────────────────

def download_chat_replay(
    video_id: str,
    cookies_file: str = "",
    po_token: str = "",
    deno_path: str = "",
    output_dir: str | None = None,
) -> list[dict]:
    """
    Use yt-dlp to download the full chat replay for a VOD.
    Returns a list of chat message dicts sorted by time_in_seconds.

    Each message dict has:
        {
            "time_in_seconds": float,   # seconds from stream start
            "timestamp": int,           # unix epoch
            "author": str,
            "message": str,
            "amount": str | None,       # for superchats
        }
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    out_dir = output_dir or tempfile.mkdtemp(prefix="yt_chat_")

    print(f"  📥 Downloading chat replay via yt-dlp...")
    print(f"     This may take a moment for long streams...\n")

    cmd = [
        "yt-dlp",
        "--write-subs",
        "--sub-langs", "live_chat",
        "--skip-download",
        "--output", f"{out_dir}/%(id)s.%(ext)s",
    ]

    if cookies_file:
        cmd += ["--cookies", cookies_file]

    if po_token:
        cmd += ["--extractor-args", f"youtube:po_token=web+{po_token}"]

    if deno_path:
        cmd += ["--js-runtimes", f"deno:{deno_path}"]

    cmd.append(url)

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
    )

    if result.returncode != 0:
        raise YouTubeAPIError(
            f"yt-dlp failed to download chat:\n{result.stderr.strip()}"
        )

    # yt-dlp writes chat as <video_id>.live_chat.json
    chat_file = Path(out_dir) / f"{video_id}.live_chat.json"
    if not chat_file.exists():
        # Try finding any .live_chat.json in the dir
        matches = list(Path(out_dir).glob("*.live_chat.json"))
        if not matches:
            raise YouTubeAPIError(
                "Chat replay file not found. The VOD may not have chat replay enabled."
            )
        chat_file = matches[0]

    print(f"  ✅ Chat file downloaded: {chat_file.name}")
    return _parse_chat_file(chat_file)


def _parse_chat_file(chat_file: Path) -> list[dict]:
    """
    Parse yt-dlp's .live_chat.json file.
    Each line is a top-level JSON object with structure:
        {
            "replayChatItemAction": {
                "actions": [...],
                "videoOffsetTimeMsec": "12345"   # <-- stream offset for ALL actions on this line
            }
        }
    """
    messages = []

    with open(chat_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            replay = event.get("replayChatItemAction", {})

            # videoOffsetTimeMsec lives at the replayChatItemAction level
            offset_ms = replay.get("videoOffsetTimeMsec")
            time_in_seconds = int(offset_ms) / 1000 if offset_ms is not None else None

            # Skip pre-stream messages (negative or zero offset waiting room)
            if time_in_seconds is None or time_in_seconds < 0:
                continue

            for action in replay.get("actions", []):
                msg = _extract_message(action, time_in_seconds)
                if msg:
                    messages.append(msg)

    messages.sort(key=lambda m: m["time_in_seconds"])
    print(f"  📊 Parsed {len(messages):,} chat messages")
    return messages


def _extract_message(action: dict, time_in_seconds: float) -> dict | None:
    """Extract a normalized message dict from a single chat action."""
    item = action.get("addChatItemAction", {}).get("item", {})
    if not item:
        return None

    # Regular chat message
    renderer = item.get("liveChatTextMessageRenderer", {})
    amount = None

    # Superchat
    if not renderer:
        renderer = item.get("liveChatPaidMessageRenderer", {})
        if renderer:
            amount_runs = renderer.get("purchaseAmountText", {}).get("runs", [])
            amount = "".join(r.get("text", "") for r in amount_runs) or None

    if not renderer:
        return None

    # Extract message text (handles text runs and emoji)
    message_runs = renderer.get("message", {}).get("runs", [])
    parts = []
    for r in message_runs:
        if "text" in r:
            parts.append(r["text"])
        elif "emoji" in r:
            emoji = r["emoji"]
            # Prefer shortcut (e.g. ":bijou:") over raw emojiId (UC9p_lq...)
            shortcuts = emoji.get("shortcuts")
            if shortcuts:
                parts.append(shortcuts[0])
            # else: skip raw custom emoji IDs entirely — they're unreadable garbage
    message_text = "".join(parts)

    author = renderer.get("authorName", {}).get("simpleText", "")
    unix_ts = int(renderer.get("timestampUsec", 0)) // 1_000_000

    return {
        "time_in_seconds": time_in_seconds,
        "timestamp": unix_ts,
        "author": author,
        "message": message_text,
        "amount": amount,
    }


# ── Live stream polling via YouTube Data API v3 ───────────────────────────────

def fetch_chat_page(
    live_chat_id: str,
    api_key: str,
    page_token: str | None = None,
) -> dict:
    """
    Fetch one page of live chat messages (for active live streams only).
    Returns:
        {
            "messages": list[dict],
            "next_page_token": str | None,
            "poll_interval_seconds": float,
        }
    """
    params = {
        "part": "snippet,authorDetails",
        "liveChatId": live_chat_id,
        "key": api_key,
        "maxResults": 2000,
    }
    if page_token:
        params["pageToken"] = page_token

    resp = requests.get(
        f"{YT_API_BASE}/liveChat/messages",
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    return {
        "messages": data.get("items", []),
        "next_page_token": data.get("nextPageToken"),
        "poll_interval_seconds": data.get("pollingIntervalMillis", 5000) / 1000,
    }
