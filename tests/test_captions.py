from pathlib import Path

import pytest

from chatwatcher.ingest.captions import (
    Segment, choose_track, parse_caption_file, read_transcript, write_transcript,
)

FIX = Path(__file__).parent / "fixtures"


def test_json3_merges_repeats_and_clamps_overlap():
    segs = parse_caption_file(FIX / "captions_sample.json3")
    assert segs == [
        Segment(1.0, 3.0, "こんにちは みなさん"),
        Segment(3.0, 9.0, "今日は歌枠です"),
    ]


def test_vtt_drops_rolling_duplicates():
    segs = parse_caption_file(FIX / "captions_sample.vtt")
    assert [(s.start, s.text) for s in segs] == [
        (1.0, "hello everyone"),
        (3.01, "welcome to karaoke"),
        (3600.0, "bye"),
    ]


def test_unsupported_format(tmp_path):
    p = tmp_path / "x.srt"
    p.write_text("1\n")
    with pytest.raises(ValueError):
        parse_caption_file(p)


def test_transcript_roundtrip(tmp_path):
    segs = [Segment(0.0, 1.5, "a"), Segment(1.5, 3.0, "び")]
    write_transcript(tmp_path / "t.jsonl", segs)
    assert read_transcript(tmp_path / "t.jsonl") == segs


AUTO = {"ja-orig": [], "ja": [], "en": [], "fr": []}


@pytest.mark.parametrize("info,expected", [
    ({"subtitles": {"live_chat": [], "en-US": []}, "automatic_captions": AUTO}, ("en-US", False)),
    ({"subtitles": {"live_chat": []}, "automatic_captions": AUTO}, ("ja-orig", True)),
    ({"subtitles": {"de": []}, "automatic_captions": AUTO}, ("ja-orig", True)),
    # real case: AI-dub 'en-orig' listed before the true 'ja-orig'
    ({"automatic_captions": {"en-orig": [], "ja-orig": [], "en": []}, "language": "ja"},
     ("ja-orig", True)),
    ({"automatic_captions": {"en-orig": [], "ko-orig": []}, "language": "ko"}, ("ko-orig", True)),
    ({"automatic_captions": {"de-orig": [], "en-orig": []}}, ("en-orig", True)),
    ({"automatic_captions": {"de-orig": []}}, ("de-orig", True)),
    ({"automatic_captions": {"ja": [], "en": []}, "language": "en"}, ("en", True)),
    ({"automatic_captions": {"fr": [], "ja": []}}, ("ja", True)),
    ({"subtitles": {"live_chat": []}}, None),
    ({}, None),
])
def test_choose_track(info, expected):
    assert choose_track(info, ["ja", "en"]) == expected
