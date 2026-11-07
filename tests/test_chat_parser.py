import gzip
import shutil
from pathlib import Path

import pytest

from chatwatcher.ingest.chat_parser import (
    chat_stats, parse_amount, parse_chat_file, read_messages, write_messages,
)

FIXTURE = Path(__file__).parent / "fixtures" / "live_chat_sample.json"


@pytest.fixture(scope="module")
def msgs():
    return parse_chat_file(FIXTURE)


def by_id(msgs, mid):
    return next(m for m in msgs if m.id == mid)


def test_filters_dedupes_and_sorts(msgs):
    ids = [m.id for m in msgs]
    assert ids == ["m1", "m2", "late", "sc1", "sc2", "st1", "mem1", "mem2", "gift1", "jg1", "own"]
    assert "pre" not in ids          # negative offset (waiting room)
    assert "red1" not in ids         # gift redemption is noise
    assert "ph1" not in ids          # placeholder
    assert [m.t for m in msgs] == sorted(m.t for m in msgs)


def test_text_emoji_and_badges(msgs):
    m1 = by_id(msgs, "m1")
    assert m1.text == "こんばんは:_kusa::_kusa:"
    assert m1.emotes == [":_kusa:", ":_kusa:"]
    assert m1.is_member and not m1.is_mod
    assert m1.author == "Alice" and m1.author_id == "UC_alice"
    assert m1.ts_usec == 1700000001000000
    assert m1.t == 1.5

    m2 = by_id(msgs, "m2")
    assert m2.text == "lol 😂"      # unicode emoji kept, custom emoji without shortcut dropped
    assert m2.is_mod

    own = by_id(msgs, "own")
    assert own.is_owner and own.is_verified


def test_paid_and_membership_items(msgs):
    sc1 = by_id(msgs, "sc1")
    assert (sc1.kind, sc1.amount, sc1.currency, sc1.text) == ("superchat", 1000.0, "¥", "草")
    sc2 = by_id(msgs, "sc2")
    assert (sc2.amount, sc2.currency, sc2.text) == (5.0, "CA$", "")
    st1 = by_id(msgs, "st1")
    assert (st1.kind, st1.amount, st1.currency) == ("sticker", 2.0, "$")

    assert by_id(msgs, "mem1").kind == "member_new"
    mem2 = by_id(msgs, "mem2")
    assert (mem2.kind, mem2.text) == ("member_milestone", "half a year!")

    gift = by_id(msgs, "gift1")
    assert (gift.kind, gift.gift_count, gift.author, gift.author_id) == ("gift", 5, "Gina", "UC_gina")


def test_stats(msgs):
    s = chat_stats(msgs)
    assert s["messages"] == 11
    assert s["unique_authors"] == 10
    assert s["by_kind"] == {"chat": 4, "superchat": 2, "sticker": 1, "member_new": 1,
                            "member_milestone": 1, "gift": 1, "jewel_gift": 1}
    assert (s["first_t"], s["last_t"]) == (1.5, 9.0)


def test_gz_input_and_roundtrip(tmp_path, msgs):
    gz = tmp_path / "chat.json.gz"
    with FIXTURE.open("rb") as fin, gzip.open(gz, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    assert [m.id for m in parse_chat_file(gz)] == [m.id for m in msgs]

    out = tmp_path / "chat.jsonl.gz"
    write_messages(out, msgs)
    assert read_messages(out) == msgs


@pytest.mark.parametrize("text,expected", [
    ("¥1,000", (1000.0, "¥")),
    ("$5.00", (5.0, "$")),
    ("CA$5.00", (5.0, "CA$")),
    ("₩10,000", (10000.0, "₩")),
    ("PHP 100.00", (100.0, "PHP")),
    ("100.00 PHP", (100.0, "PHP")),
    ("", (None, None)),
    (None, (None, None)),
    ("free", (None, None)),
])
def test_parse_amount(text, expected):
    assert parse_amount(text) == expected


def test_jewel_gift(msgs):
    g = by_id(msgs, "jg1")
    assert (g.kind, g.author, g.text, g.t) == ("jewel_gift", "@gifter", "Corgi", 8.5)
