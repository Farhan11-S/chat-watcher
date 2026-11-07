"""Stream-type detection and per-type signal weights."""

from __future__ import annotations

import re
from typing import Any

from chatwatcher.scoring.features import FEATURES, Timeline

STREAM_TYPES = ("talk", "gaming", "karaoke")

# Weight of each signal per stream type. Activity signals (rate, chatters) say "something
# happened"; reaction signals say what kind of thing, which is what makes a clip.
PRESETS: dict[str, dict[str, float]] = {
    "talk": {
        "rate": 1.0, "chatters": 1.0, "emotes": 0.6,
        "laugh": 2.0, "hype": 0.5, "surprise": 1.0, "applause": 0.2,
        "cute": 1.0, "emotion": 0.8, "clip": 1.5,
        "money": 0.5, "replay": 1.5,
    },
    "gaming": {
        "rate": 1.0, "chatters": 1.0, "emotes": 0.6,
        "laugh": 1.5, "hype": 2.0, "surprise": 1.5, "applause": 0.3,
        "cute": 0.5, "emotion": 0.6, "clip": 1.5,
        "money": 0.5, "replay": 1.5,
    },
    "karaoke": {
        "rate": 0.8, "chatters": 0.8, "emotes": 0.6,
        "laugh": 1.0, "hype": 0.8, "surprise": 0.3, "applause": 2.0,
        "cute": 0.8, "emotion": 1.0, "clip": 1.0,
        "money": 1.0, "replay": 2.0,
    },
}

_KARAOKE = re.compile(
    r"歌枠|うたわく|カラオケ|karaoke|singing|歌ってみ|歌雑|歌回|弾き語り|アカペラ|"
    r"\bsing\b|\bsongs?\b|utawaku|🎤", re.I)
_TALK = re.compile(r"雑談|zatsudan|freetalk|free talk|\bchat(?:ting)?\b|おはなし|お話|"
                   r"マシュマロ|marshmallow|質問|asmr|晩酌|朝活", re.I)
_GAMING = re.compile(
    r"ゲーム|game|gaming|実況|playing|\bplay\b|プレイ|初見プレイ|攻略|rta|"
    r"マイクラ|minecraft|apex|valorant|ヴァロ|マリカ|mario|ポケモン|pok[eé]mon|スプラ|splatoon|"
    r"スト6|street fighter|原神|genshin|モンハン|monster hunter|elden|エルデン|ホラ|horror|"
    r"phasmophobia|fortnite|フォートナイト|lethal company|リーサル|\bark\b|雀魂|麻雀|"
    r"8番出口|exit 8|ゼルダ|zelda|ドラクエ|\bff\d+|ファイナルファンタジー|太鼓|taiko", re.I)


def applause_bursts(tl: Timeline, min_z: float = 4.0, gap_s: float = 90) -> int:
    """Count big applause bursts (8888/👏 waves). Karaoke streams have one per song,
    ~10/hour; talk and gaming streams have at most one or two."""
    bs = tl.bin_s
    applause, rate = tl.raw["applause"], tl.raw["rate"]
    z = tl.z["applause"]
    gap = max(1, round(gap_s / bs))
    order = sorted((i for i in range(len(z)) if z[i] >= min_z), key=lambda i: -z[i])
    picked: list[int] = []
    for i in order:
        big = applause[i] >= max(8.0, 0.2 * rate[i])  # applause is a real share of chat
        if big and all(abs(i - j) >= gap for j in picked):
            picked.append(i)
    return len(picked)


def detect_type(info: dict[str, Any], tl: Timeline | None = None) -> tuple[str, str]:
    """Guess talk / gaming / karaoke. Returns (type, reason).

    Order: title → chat applause pattern → YouTube category → tags. Within the title,
    karaoke wins (歌枠 titles often name games too) and 雑談 beats game words
    ("APEXかマイクラ雑談" is mostly talk). Tags never imply karaoke: many channels tag
    every video with 歌ってみた.
    """
    title = info.get("title") or ""
    if m := _KARAOKE.search(title):
        return "karaoke", f"title: {m.group()}"
    if m := _TALK.search(title):
        return "talk", f"title: {m.group()}"
    if m := _GAMING.search(title):
        return "gaming", f"title: {m.group()}"
    if tl is not None:
        hours = max(tl.n_bins * tl.bin_s / 3600, 0.25)
        n = applause_bursts(tl)
        if n >= 3 and n / hours >= 4:
            return "karaoke", f"chat: {n} applause waves"
    if "Gaming" in (info.get("categories") or []):
        return "gaming", "category: Gaming"
    tags = " ".join(info.get("tags") or [])
    if m := _TALK.search(tags):
        return "talk", f"tags: {m.group()}"
    if m := _GAMING.search(tags):
        return "gaming", f"tags: {m.group()}"
    return "talk", "default"


def weights_for(stream_type: str, overrides: dict[str, dict[str, float]] | None = None) -> dict[str, float]:
    if stream_type not in PRESETS:
        raise ValueError(f"Unknown stream type {stream_type!r}; expected one of {STREAM_TYPES}")
    w = dict(PRESETS[stream_type])
    extra = (overrides or {}).get(stream_type, {})
    unknown = set(extra) - set(FEATURES)
    if unknown:
        raise ValueError(f"Unknown signals in scoring.weights.{stream_type}: {sorted(unknown)}")
    w.update(extra)
    return w
