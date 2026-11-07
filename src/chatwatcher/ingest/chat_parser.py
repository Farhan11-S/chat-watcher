"""Parse yt-dlp's <id>.live_chat.json (one JSON object per line) into flat messages.

Kept: normal chat, Super Chats, Super Stickers, new members, membership milestones,
gifted memberships, jewel gifts. Skipped: ticker duplicates, gift redemptions, system/placeholder
items, and anything before the stream started (negative offset).
"""

from __future__ import annotations

import gzip
import io
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO, Any, Iterable, Iterator

KINDS = ("chat", "superchat", "sticker", "member_new", "member_milestone", "gift", "jewel_gift")


@dataclass(slots=True)
class ChatMessage:
    t: float                    # seconds from stream start (video offset)
    id: str
    author: str
    author_id: str              # UC… channel id; stable across name changes
    text: str
    kind: str = "chat"
    ts_usec: int = 0            # wall-clock time (unix µs)
    emotes: list[str] = field(default_factory=list)  # custom emote shortcuts, e.g. ":_kusa:"
    amount: float | None = None
    currency: str | None = None
    amount_text: str | None = None
    gift_count: int | None = None
    is_member: bool = False
    is_mod: bool = False
    is_owner: bool = False
    is_verified: bool = False

    def to_json(self) -> dict[str, Any]:
        """Compact dict: drop empty/false/None fields."""
        return {k: v for k, v in asdict(self).items() if v not in (None, False, [], "")} | {
            "t": self.t, "kind": self.kind,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "ChatMessage":
        return cls(**{"id": "", "author": "", "author_id": "", "text": ""} | d)


# ── helpers ──────────────────────────────────────────────────────────────────

_AMOUNT = re.compile(r"^\s*(?P<pre>[^\d\s.,]*)\s*(?P<num>\d[\d.,]*)\s*(?P<post>[^\d\s.,]*)\s*$")


def parse_amount(text: str | None) -> tuple[float | None, str | None]:
    """'¥1,000' → (1000.0, '¥'); 'CA$5.00' → (5.0, 'CA$'). yt-dlp asks for English text,
    so ',' is a thousands separator and '.' the decimal point."""
    if not text:
        return None, None
    m = _AMOUNT.match(text.replace("\xa0", " "))
    if not m:
        return None, None
    try:
        value = float(m["num"].replace(",", ""))
    except ValueError:
        return None, None
    currency = (m["pre"] or m["post"] or "").strip() or None
    return value, currency


def _simple_text(obj: dict | None) -> str:
    if not obj:
        return ""
    if "simpleText" in obj:
        return obj["simpleText"]
    return "".join(r.get("text", "") for r in obj.get("runs", []))


def _runs_text(message: dict | None) -> tuple[str, list[str]]:
    """Message runs → (text, custom emote shortcuts). Unicode emoji stay as characters."""
    parts: list[str] = []
    emotes: list[str] = []
    for run in (message or {}).get("runs", []):
        if "text" in run:
            parts.append(run["text"])
        elif "emoji" in run:
            emoji = run["emoji"]
            shortcuts = emoji.get("shortcuts") or []
            if emoji.get("isCustomEmoji"):
                if shortcuts:
                    parts.append(shortcuts[0])
                    emotes.append(shortcuts[0])
            elif emoji.get("emojiId"):
                parts.append(emoji["emojiId"])  # the emoji character itself, e.g. 😂
            elif shortcuts:
                parts.append(shortcuts[0])
    return "".join(parts).strip(), emotes


def _badges(renderer: dict) -> dict[str, bool]:
    flags = {"is_member": False, "is_mod": False, "is_owner": False, "is_verified": False}
    for b in renderer.get("authorBadges", []):
        badge = b.get("liveChatAuthorBadgeRenderer", {})
        icon = badge.get("icon", {}).get("iconType", "")
        if icon == "MODERATOR":
            flags["is_mod"] = True
        elif icon == "OWNER":
            flags["is_owner"] = True
        elif icon == "VERIFIED":
            flags["is_verified"] = True
        elif "customThumbnail" in badge:  # member badges use the channel's custom image
            flags["is_member"] = True
    return flags


def _base(renderer: dict, t: float, text: str, emotes: list[str], kind: str) -> ChatMessage:
    return ChatMessage(
        t=round(t, 3),
        id=renderer.get("id", ""),
        author=_simple_text(renderer.get("authorName")),
        author_id=renderer.get("authorExternalChannelId", ""),
        text=text,
        kind=kind,
        ts_usec=int(renderer.get("timestampUsec", 0) or 0),
        emotes=emotes,
        **_badges(renderer),
    )


def _from_item(item: dict, t: float) -> ChatMessage | None:
    if r := item.get("liveChatTextMessageRenderer"):
        text, emotes = _runs_text(r.get("message"))
        return _base(r, t, text, emotes, "chat")

    if r := item.get("liveChatPaidMessageRenderer"):
        text, emotes = _runs_text(r.get("message"))
        msg = _base(r, t, text, emotes, "superchat")
        msg.amount_text = _simple_text(r.get("purchaseAmountText")) or None
        msg.amount, msg.currency = parse_amount(msg.amount_text)
        return msg

    if r := item.get("liveChatPaidStickerRenderer"):
        msg = _base(r, t, "", [], "sticker")
        msg.amount_text = _simple_text(r.get("purchaseAmountText")) or None
        msg.amount, msg.currency = parse_amount(msg.amount_text)
        return msg

    if r := item.get("liveChatMembershipItemRenderer"):
        text, emotes = _runs_text(r.get("message"))
        # Milestones carry a chat message and/or a "Member for N months" header.
        header = _simple_text(r.get("headerPrimaryText"))
        kind = "member_milestone" if (text or header) else "member_new"
        msg = _base(r, t, text, emotes, kind)
        msg.is_member = True
        return msg

    if r := item.get("giftMessageViewModel"):
        # Newer "jewel" gifts: only a display name and the gift name, no price or channel id.
        name = (r.get("authorName") or {}).get("content", "").strip()
        label = (r.get("text") or {}).get("content", "")
        return ChatMessage(t=round(t, 3), id=r.get("id", ""), author=name, author_id="",
                           text=label.removeprefix("sent ").strip(), kind="jewel_gift")

    if r := item.get("liveChatSponsorshipsGiftPurchaseAnnouncementRenderer"):
        header = r.get("header", {}).get("liveChatSponsorshipsHeaderRenderer", {})
        msg = _base(r | {"authorName": header.get("authorName"),
                         "authorBadges": header.get("authorBadges", [])},
                    t, "", [], "gift")
        count = re.search(r"\d+", _simple_text(header.get("primaryText")))
        msg.gift_count = int(count.group()) if count else 1
        return msg

    return None


def _offset_seconds(event: dict, replay: dict) -> float | None:
    raw = replay.get("videoOffsetTimeMsec", event.get("videoOffsetTimeMsec"))
    if raw is None:
        return None
    try:
        return int(raw) / 1000
    except (TypeError, ValueError):
        return None


def iter_messages(lines: Iterable[str]) -> Iterator[ChatMessage]:
    """Yield messages in file order. Malformed lines are skipped."""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        replay = event.get("replayChatItemAction") or {}
        t = _offset_seconds(event, replay)
        if t is None or t < 0:
            continue
        for action in replay.get("actions", []):
            item = action.get("addChatItemAction", {}).get("item")
            if item and (msg := _from_item(item, t)):
                yield msg


def _open_text(path: Path) -> IO[str]:
    if path.suffix == ".gz":
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def parse_chat_file(path: str | Path) -> list[ChatMessage]:
    """Parse a raw live_chat.json (or .json.gz). Deduplicated by id, sorted by time."""
    seen: set[str] = set()
    out: list[ChatMessage] = []
    with _open_text(Path(path)) as f:
        for msg in iter_messages(f):
            if msg.id:
                if msg.id in seen:
                    continue
                seen.add(msg.id)
            out.append(msg)
    out.sort(key=lambda m: m.t)  # stable: keeps file order within the same offset
    return out


# ── normalized storage (chat.jsonl.gz) ───────────────────────────────────────

def write_messages(path: str | Path, messages: Iterable[ChatMessage]) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        for m in messages:
            f.write(json.dumps(m.to_json(), ensure_ascii=False, separators=(",", ":")))
            f.write("\n")
    tmp.replace(path)


def read_messages(path: str | Path) -> list[ChatMessage]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [ChatMessage.from_json(json.loads(line)) for line in f if line.strip()]


def chat_stats(messages: list[ChatMessage]) -> dict[str, Any]:
    kinds = Counter(m.kind for m in messages)
    return {
        "messages": len(messages),
        "unique_authors": len({m.author_id or m.author for m in messages}),
        "by_kind": {k: kinds.get(k, 0) for k in KINDS if kinds.get(k)},
        "first_t": messages[0].t if messages else None,
        "last_t": messages[-1].t if messages else None,
    }
