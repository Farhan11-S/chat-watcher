import numpy as np
import pytest

from chatwatcher.ingest.captions import Segment
from chatwatcher.ingest.chat_parser import ChatMessage
from chatwatcher.scoring.features import build_timeline, heatmap_bins, robust_z, to_usd
from chatwatcher.scoring.lexicons import Lexicon, canonical
from chatwatcher.scoring.moments import (
    Moment, ScoreParams, adaptive_threshold, clip_window, find_peaks, merge_overlapping,
    pick_moments, sentences, snap,
)
from chatwatcher.scoring.presets import applause_bursts, detect_type, weights_for

from scoring_helpers import background, burst, chat

lex = Lexicon()


# ── lexicons ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,cats", [
    ("草", {"laugh"}), ("ｗｗｗｗ", {"laugh"}), ("それなw", {"laugh"}), (":_kusa::_kusa:", {"laugh"}),
    ("lmao", {"laugh"}), ("wow", set()),
    ("８８８８８", {"applause"}), ("👏👏", {"applause"}), ("歌うますぎ", {"applause"}),
    ("てぇてぇ", {"cute"}), ("かわいい", {"cute"}),
    ("？？？", {"surprise"}), ("え", {"surprise"}), (":_きゃー:", {"surprise"}),
    ("ナイス！！！", {"hype"}), ("おめでとう", {"emotion"}), ("😭", {"emotion"}),
    ("ここすき", {"clip"}), ("切り抜き班たのむ", {"clip"}),
    ("なるほど", set()),
])
def test_categories(text, cats):
    assert set(lex.categories(text)) == cats


@pytest.mark.parametrize("text,greeting", [
    ("おつぺこ", True), ("乙です", True), ("こんばんは", True), ("こんぺこ！", True),
    ("hello!", True), ("初見です", True), ("こんなの草", False), ("今度やって", False),
    ("草", False), ("hiii", True), ("history", False),
])
def test_greetings(text, greeting):
    assert Lexicon.is_greeting(text) is greeting


def test_extra_keywords_and_validation():
    custom = Lexicon({"laugh": [":_myWarai:"]})
    assert custom.categories(":_mywarai:") == ["laugh"]
    with pytest.raises(ValueError):
        Lexicon({"funny": ["x"]})


def test_canonical():
    assert canonical("ｗｗｗｗｗｗ") == "www"
    assert canonical("8888888") == "888"
    assert canonical(" 草 ") == "草"


def test_to_usd():
    assert to_usd(1000, "¥") == pytest.approx(6.7)
    assert to_usd(5, "$") == 5
    assert to_usd(5, "XYZ") == 2.0 and to_usd(None, None) == 2.0


# ── features ─────────────────────────────────────────────────────────────────

def test_robust_z_flags_spike_not_baseline():
    x = np.full(200, 10.0)
    x[100] = 40
    z = robust_z(x, 30)
    assert z[100] > 8 and abs(z[50]) < 1e-9


def test_timeline_detects_laugh_burst_and_ignores_owner():
    msgs = chat(
        background(1800),
        burst(600, "草", n=50),
        burst(1200, "草", n=50, prefix="own", is_owner=True),  # streamer's own spam: ignored
    )
    tl = build_timeline(msgs, 1800)
    peak_bin = int(np.argmax(tl.z["laugh"]))
    assert 600 <= peak_bin * 5 <= 615
    assert tl.z["laugh"][int(1205 // 5)] < 2
    assert tl.z["rate"][peak_bin] > 3 and tl.z["chatters"][peak_bin] > 3


def test_greetings_excluded_from_rate():
    msgs = chat(background(1800), burst(900, "おつぺこ", n=80))
    tl = build_timeline(msgs, 1800)
    assert tl.z["rate"].max() < 4


def test_money_counts_superchats_and_gifts():
    msgs = chat(background(1800), [
        ChatMessage(t=700, id="sc", author="a", author_id="UCa", text="", kind="superchat",
                    amount=10000, currency="¥"),
        ChatMessage(t=701, id="g", author="b", author_id="UCb", text="", kind="gift", gift_count=20),
    ])
    tl = build_timeline(msgs, 1800)
    assert int(np.argmax(tl.z["money"])) * 5 in range(690, 711)


def test_heatmap_bins_zero_edges():
    h = heatmap_bins([[0, 100, 1.0], [100, 200, 0.5], [200, 300, 0.2]], 60, 5)
    assert h[0] == 0 and h[11] == 0           # first minute zeroed
    assert h[25] == 0.5 and h[59] == 0         # last minute zeroed
    assert heatmap_bins(None, 10, 5).sum() == 0


# ── moments ──────────────────────────────────────────────────────────────────

def test_find_peaks_respects_gap_and_edges():
    s = np.zeros(100)
    s[[5, 30, 33, 70]] = [9, 5, 4, 3]
    assert find_peaks(s, 1.0, gap_bins=10) == [5, 30, 70]
    assert find_peaks(s, 1.0, gap_bins=10, ignore_bins=10) == [30, 70]
    assert find_peaks(s, 4.5, gap_bins=10) == [5, 30]


def test_adaptive_threshold():
    flat = np.linspace(0, 0.8, 1000)
    assert adaptive_threshold(flat, 1.0) == pytest.approx(0.776, abs=0.01)
    assert adaptive_threshold(np.zeros(100), 1.0) == 0.6
    assert adaptive_threshold(np.linspace(0, 5, 100), 1.0) == 1.0


def test_sentences_merge_broken_lines():
    segs = [Segment(0, 2, "そう、なんかち、ちのさんという方が作ら"), Segment(2, 3, "れた"),
            Segment(3, 5, "曲なんだけど。"), Segment(8, 9, "次の話")]
    out = sentences(segs)
    assert [(s.start, s.end) for s in out] == [(0, 5), (8, 9)]
    assert out[0].text.endswith("曲なんだけど。") and "作られた曲" in out[0].text
    en = sentences([Segment(0, 2, "I hope you guys are going to"), Segment(2, 4, "go watch it."),
                    Segment(4, 5, "Can"), Segment(5, 6, "you believe it?"), Segment(6, 7, "草"), Segment(7, 8, "w")])
    assert [s.text for s in en] == ["I hope you guys are going to go watch it.", "Can you believe it?", "草w"]


def test_snap():
    sents = [Segment(10, 20, "a"), Segment(20, 40, "b")]
    assert snap(15, sents, back_s=10, fwd_s=0, to="start") == 10
    assert snap(35, sents, back_s=10, fwd_s=0, to="start") == 35   # too far back
    assert snap(35, sents, back_s=0, fwd_s=8, to="end") == 40
    assert snap(25, sents, back_s=0, fwd_s=8, to="end") == 25       # too far forward
    assert snap(50, sents, back_s=10, fwd_s=8, to="end") == 50       # outside any sentence


def test_clip_window_limits():
    p = ScoreParams(pre_roll_s=25, post_roll_s=5, min_clip_s=20, max_clip_s=180)
    assert clip_window(100, 95, 110, p, [], 1000) == (70.0, 115.0)
    s, e = clip_window(500, 300, 600, p, [], 1000)              # very long reaction
    assert e - s == 180 and s == pytest.approx(500 - 135)
    assert clip_window(10, 5, 8, p, [], 1000)[0] == 0.0          # clamped at 0
    assert clip_window(995, 990, 999, p, [], 1000)[1] == 1000    # clamped at end


def _m(start, end, score):
    return Moment(rank=0, score=score, peak_s=start, react_start_s=start, react_end_s=end,
                  start_s=start, end_s=end)


def test_merge_overlapping():
    out = merge_overlapping([_m(0, 60, 1), _m(50, 120, 2), _m(300, 360, 1.5)], 180)
    spans = sorted((m.start_s, m.end_s, m.score) for m in out)
    assert spans == [(0, 120, 2), (300, 360, 1.5)]
    # union too long → kept apart
    assert len(merge_overlapping([_m(0, 150, 1), _m(140, 300, 2)], 180)) == 2


def test_pick_moments_ranks_strongest_reaction_first():
    msgs = chat(background(3600), burst(900, "草", n=30), burst(2400, "草", n=90, prefix="c"),
                burst(3000, "なるほど", n=4, prefix="d"))
    tl = build_timeline(msgs, 3600)
    moments, score = pick_moments(tl, weights_for("talk"), msgs, [], 3600, ScoreParams())
    assert len(score) == tl.n_bins
    assert [m.rank for m in moments] == list(range(1, len(moments) + 1))
    top = moments[0]
    assert top.start_s < 2400 < top.end_s and top.kind == "reaction"
    assert any(m.start_s < 900 < m.end_s for m in moments)
    assert top.top_messages[0][0] == "草"
    assert top.reasons and top.reasons[0]["signal"] in ("laugh", "chatters", "rate")


def test_greeting_wave_is_penalized():
    msgs = chat(background(3600), burst(900, "草", n=40), burst(2000, "おつぺこ", n=150, prefix="g"),
                burst(2000, "草", n=40, prefix="h"))
    tl = build_timeline(msgs, 3600)
    moments, _ = pick_moments(tl, weights_for("talk"), msgs, [], 3600, ScoreParams())
    assert moments[0].start_s < 900 < moments[0].end_s  # the clean laugh wins


def _karaoke_chat(songs=5, every=360):
    parts = [background(songs * every + 300, per_5s=4)]
    for k in range(songs):
        end = 300 + k * every
        parts.append(burst(end, "8888", n=60, prefix=f"s{k}"))
        parts.append(burst(end - 120, "🎵うまい", n=8, span=60, prefix=f"m{k}"))
    parts.append(burst(300 + 2 * every + 150, "草", n=50, prefix="talk"))  # a talk moment
    return chat(*parts)


def test_songs_from_applause_waves():
    msgs = _karaoke_chat()
    dur = 5 * 360 + 300
    tl = build_timeline(msgs, dur)
    assert applause_bursts(tl) == 5
    moments, _ = pick_moments(tl, weights_for("karaoke"), msgs, [], dur, ScoreParams(), songs=True)
    songs = sorted((m for m in moments if m.kind == "song"), key=lambda m: m.start_s)
    assert len(songs) == 5
    for k, s in enumerate(songs):
        applause_at = 300 + k * 360
        assert s.start_s < applause_at - 200 and applause_at <= s.end_s <= applause_at + 40
    for a, b in zip(songs, songs[1:]):
        assert a.end_s <= b.start_s          # songs never overlap
    talk = [m for m in moments if m.kind == "reaction"]
    assert any(m.start_s < 300 + 2 * 360 + 150 < m.end_s for m in talk)


# ── presets ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("info,expected", [
    ({"title": "【歌枠】アニソン縛り"}, "karaoke"),
    ({"title": "【#KARAOKE】Singing stream"}, "karaoke"),
    ({"title": "【雑談】おはなし"}, "talk"),
    ({"title": "APEXかマイクラ雑談！！"}, "talk"),
    ({"title": "【APEX】ランク"}, "gaming"),
    ({"title": "【心霊物件】怖すぎるアパート", "categories": ["Gaming"],
      "tags": ["歌ってみた", "ゲーム実況"]}, "gaming"),
    ({"title": "なにか", "tags": ["歌ってみた"]}, "talk"),     # tags never imply karaoke
    ({"title": "なにか"}, "talk"),
])
def test_detect_type(info, expected):
    assert detect_type(info)[0] == expected


def test_detect_karaoke_from_chat_pattern():
    msgs = _karaoke_chat(songs=6)
    tl = build_timeline(msgs, 6 * 360 + 300)
    kind, reason = detect_type({"title": "夜のおしゃべり…?", "categories": ["Entertainment"]}, tl)
    assert kind == "karaoke" and "applause" in reason
    talk_tl = build_timeline(chat(background(3600), burst(900, "草")), 3600)
    assert detect_type({"title": "なにか"}, talk_tl)[0] == "talk"


def test_weights_override():
    w = weights_for("talk", {"talk": {"laugh": 5.0}})
    assert w["laugh"] == 5.0 and w["rate"] == 1.0
    with pytest.raises(ValueError):
        weights_for("talk", {"talk": {"lol": 1}})
    with pytest.raises(ValueError):
        weights_for("asmr")
