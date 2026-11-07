"""Reaction vocabularies for VTuber chat (Japanese + English + a little ID/KR/ES).

Text is NFKC-normalized and lower-cased before matching, so full-width ｗ/８/？ become
w/8/?. Custom emotes arrive as their shortcut text (":_kusa:"), so emote names are
matched by the same patterns.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Reaction categories, in display order.
CATEGORIES = ("laugh", "hype", "surprise", "applause", "cute", "emotion", "clip")

_JP = r"぀-ヿ一-鿿"

PATTERNS: dict[str, list[str]] = {
    "laugh": [
        r"草", r"笑", r"わろ", r"ワロ", r"kusa", r"warai", r"wara",
        r"(?<![a-z])w{2,}(?![a-z])",            # www
        rf"(?<=[{_JP}])w(?![a-z])",             # それなw
        r"\blol+\b", r"\blmf?ao+\b", r"\brofl\b", r"kekw", r"omegalul", r"\bicant\b",
        r"\b(?:a?ha){2,}h?\b", r"\b(?:ja){2,}\b", r"\bwkwk", r"ㅋㅋ",
        r"🤣", r"😂", r"😆",
    ],
    "hype": [
        r"うお+", r"ｳｵ", r"おおお+", r"ナイス", r"ないす", r"\bnice+\b", r"\bpog(?:gers|u)?\b",
        r"let'?s ?go+", r"\bgg\b", r"神", r"かっこい", r"かっこよ", r"すご", r"すげ", r"つよ",
        r"\bclutch\b", r"\binsane\b", r"\bhype\b", r"🔥", r"💪", r"!{3,}",
    ],
    "surprise": [
        r"\?{2,}", r"!\?|\?!", r"え{2,}", r"^えっ?[!?…。]*$", r"えっ", r"^は\?*$", r"は\?",
        r"\bwtf\b", r"\bhuh\b", r"\bwhat+\b", r"\bwhy\b", r"\bomg\b", r"まじ", r"マジ",
        r"やば", r"ヤバ", r"ひえ", r"ヒエ", r"ひぃ", r"こわ", r"怖", r"びっくり", r"ビビ",
        r"ぎゃ", r"ギャ", r"きゃ+ー", r"キャ+ー", r"きゃあ", r"悲鳴", r"😱", r"😳", r"🤯", r"hatena",
    ],
    "applause": [
        r"(?<!\d)8{3,}(?!\d)", r"👏", r"🎵", r"🎶", r"🎤", r"拍手", r"パチパチ", r"ぱちぱち",
        r"hakushu", r"pachi", r"clap", r"歌うま", r"うま+い", r"上手", r"天才", r"鳥肌",
        r"美声", r"歌姫", r"\bbeautiful\b", r"\bchills\b", r"goosebumps", r"angel ?voice",
        r"アンコール", r"\bencore\b", r"💯",
    ],
    "cute": [
        r"かわい", r"可愛", r"kawaii", r"\bcute\b", r"てぇてぇ", r"てえてえ", r"tee ?tee",
        r"尊", r"とうとい", r"天使", r"えらい", r"偉い", r"💕", r"❤", r"♥", r"💖", r"💗",
        r"🥰", r"😍", r"\blove\b", r"\baww+\b",
    ],
    "emotion": [
        r"泣", r"涙", r"naki", r"😭", r"🥲", r"😢", r"感動", r"おめでと", r"\bおめ\b", r"^おめ",
        r"congrat", r"happy birthday", r"誕生日", r"\bproud\b",
    ],
    "clip": [
        r"\bclip", r"切り抜", r"切りぬ", r"ここすき", r"ここ好き", r"名場面", r"神回",
        r"this is going", r"永久保存",
    ],
}

# Greetings carry no reaction signal and flood chat at stream start/end. They are
# left out of the activity counts (rate, chatters, emotes).
GREETING = re.compile(
    # "こん…" covers VTuber greetings (こんぺこ, こんこよ) but not こんな/今度-style words.
    r"^(?:おつ|乙|お疲れ|おつかれ|otsu|こん(?![なだどごか])|ばんわ|おはよ|おやす|oyasumi|"
    r"hello|hi+\b|hey\b|初見|待機|taiki|ばいばい|バイバイ|またね|bye|good ?night|gn\b|"
    r"ただいま|おかえり)",
)

_REPEATS = re.compile(r"(.)\1{3,}")


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower().strip()


def canonical(text: str) -> str:
    """For counting identical reactions: 'ｗｗｗｗｗｗ' and 'wwww' → 'www'."""
    return _REPEATS.sub(r"\1\1\1", normalize(text))


@dataclass
class Lexicon:
    extra: dict[str, list[str]] = field(default_factory=dict)  # literal keywords per category

    def __post_init__(self) -> None:
        unknown = set(self.extra) - set(CATEGORIES)
        if unknown:
            raise ValueError(f"Unknown reaction categories in extra_keywords: {sorted(unknown)}")
        self._compiled = {
            cat: re.compile("|".join(PATTERNS[cat] + [re.escape(normalize(w)) for w in self.extra.get(cat, [])]))
            for cat in CATEGORIES
        }

    def categories(self, text: str) -> list[str]:
        t = normalize(text)
        if not t:
            return []
        return [cat for cat, rx in self._compiled.items() if rx.search(t)]

    @staticmethod
    def is_greeting(text: str) -> bool:
        return bool(GREETING.match(normalize(text)))
