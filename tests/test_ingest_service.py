import json
import shutil
from pathlib import Path

import pytest

from chatwatcher import db, store
from chatwatcher.config import Config, PathsConfig
from chatwatcher.ingest.service import IngestError, ingest_video
from chatwatcher.ingest.ytdlp import YtDlpError

FIX = Path(__file__).parent / "fixtures"
VID = "abcdefghijk"


def make_info(**over):
    info = {
        "id": VID, "title": "【歌枠】karaoke night", "channel": "Test Ch", "channel_id": "UC_owner",
        "uploader_id": "@testch", "duration": 7200, "live_status": "was_live",
        "release_timestamp": 1700000000,
        "subtitles": {"live_chat": [{"ext": "json"}]},
        "automatic_captions": {"ja-orig": [{"ext": "json3"}], "en": [{"ext": "json3"}]},
        "heatmap": [{"start_time": 0.0, "end_time": 72.0, "value": 1.0},
                    {"start_time": 72.0, "end_time": 144.0, "value": 0.3}],
        "formats": [{"format_id": "x"}] * 50,
    }
    info.update(over)
    return info


class FakeYtDlp:
    def __init__(self, info=None, info_error=None, caption_error=None):
        self._info = info or make_info()
        self.info_error = info_error
        self.caption_error = caption_error
        self.chat_calls = 0
        self.caption_calls = 0

    def info(self, video_id):
        if self.info_error:
            raise self.info_error
        return self._info

    def download_chat(self, video_id, dest, on_line=None):
        self.chat_calls += 1
        dest.mkdir(parents=True, exist_ok=True)
        if on_line:
            on_line("[download] 100%")
        return Path(shutil.copy(FIX / "live_chat_sample.json", dest / f"{video_id}.live_chat.json"))

    def download_captions(self, video_id, lang, auto, dest):
        self.caption_calls += 1
        if self.caption_error:
            raise self.caption_error
        dest.mkdir(parents=True, exist_ok=True)
        return Path(shutil.copy(FIX / "captions_sample.json3", dest / f"{video_id}.{lang}.json3"))

    def version(self):
        return "2026.10.26"


@pytest.fixture
def env(tmp_path):
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data", output_dir=tmp_path / "out"))
    conn = db.open_db(cfg.paths.db_path)
    yield cfg, conn
    conn.close()


def test_full_ingest(env):
    cfg, conn = env
    yt = FakeYtDlp()
    steps = []
    r = ingest_video(cfg, conn, f"https://youtu.be/{VID}", ytdlp=yt, on_step=steps.append)

    f = r.files
    for p in (f.info, f.chat, f.chat_raw, f.transcript, f.heatmap, f.manifest,
              f.root / "captions.ja-orig.json3"):
        assert p.is_file(), p
    assert not f.downloads.exists()
    assert not (f.root / f"{VID}.live_chat.json").exists()

    info = json.loads(f.info.read_text())
    assert "formats" not in info and info["has_chat_replay"]
    assert info["caption_tracks"]["auto_orig"] == ["ja-orig"]
    assert json.loads(f.heatmap.read_text()) == [[0.0, 72.0, 1.0], [72.0, 144.0, 0.3]]
    manifest = json.loads(f.manifest.read_text())
    assert manifest["chat"]["messages"] == 11 and manifest["ytdlp_version"] == "2026.10.26"

    assert r.chat["messages"] == 11
    assert (r.caption_lang, r.caption_auto, r.caption_segments) == ("ja-orig", True, 2)
    assert r.heatmap_points == 2 and r.warnings == []

    v = store.get_video(conn, VID)
    assert v["status"] == "new" and v["error"] is None
    assert v["message_count"] == 11 and v["caption_lang"] == "ja-orig" and v["has_heatmap"] == 1
    assert v["published_at"] == "2023-11-14T22:13:20Z" and v["ingested_at"]
    ch = conn.execute("SELECT * FROM channels WHERE id = ?", (v["channel_id"],)).fetchone()
    assert (ch["channel_id"], ch["handle"], ch["enabled"]) == ("UC_owner", "@testch", 0)


def test_rerun_reuses_downloads_unless_forced(env):
    cfg, conn = env
    yt = FakeYtDlp()
    ingest_video(cfg, conn, VID, ytdlp=yt)
    r = ingest_video(cfg, conn, VID, ytdlp=yt)
    assert (yt.chat_calls, yt.caption_calls) == (1, 1)
    assert r.chat_reused and r.chat["messages"] == 11 and r.caption_segments == 2

    ingest_video(cfg, conn, VID, ytdlp=yt, force=True)
    assert (yt.chat_calls, yt.caption_calls) == (2, 2)
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 1


def test_reparses_raw_chat_without_downloading(env):
    cfg, conn = env
    yt = FakeYtDlp()
    r = ingest_video(cfg, conn, VID, ytdlp=yt)
    r.files.chat.unlink()
    r2 = ingest_video(cfg, conn, VID, ytdlp=yt)
    assert yt.chat_calls == 1 and r2.chat["messages"] == 11 and not r2.chat_reused


@pytest.mark.parametrize("status,kind", [("is_upcoming", "not_finished"),
                                         ("is_live", "not_finished"),
                                         ("post_live", "processing")])
def test_unfinished_streams_are_retryable(env, status, kind):
    cfg, conn = env
    with pytest.raises(IngestError) as e:
        ingest_video(cfg, conn, VID, ytdlp=FakeYtDlp(make_info(live_status=status)))
    assert e.value.kind == kind and e.value.retryable
    v = store.get_video(conn, VID)
    assert v["status"] == "error" and v["live_status"] == status


def test_info_failure_recorded(env):
    cfg, conn = env
    err = YtDlpError("bot check", kind="bot_check", retryable=True)
    with pytest.raises(IngestError) as e:
        ingest_video(cfg, conn, VID, ytdlp=FakeYtDlp(info_error=err))
    assert e.value.kind == "bot_check"
    assert store.get_video(conn, VID)["status"] == "error"


def test_bad_input(env):
    cfg, conn = env
    with pytest.raises(IngestError) as e:
        ingest_video(cfg, conn, "not a url", ytdlp=FakeYtDlp())
    assert e.value.kind == "bad_input"


def test_no_chat_no_heatmap(env):
    cfg, conn = env
    yt = FakeYtDlp(make_info(subtitles={}, heatmap=None))
    r = ingest_video(cfg, conn, VID, ytdlp=yt)
    assert r.chat is None and yt.chat_calls == 0 and r.heatmap_points == 0
    assert any("No chat replay" in w for w in r.warnings)
    v = store.get_video(conn, VID)
    assert v["message_count"] is None and v["has_heatmap"] == 0 and v["status"] == "new"


def test_caption_failure_is_not_fatal(env):
    cfg, conn = env
    yt = FakeYtDlp(caption_error=YtDlpError("subs broke", kind="error"))
    r = ingest_video(cfg, conn, VID, ytdlp=yt)
    assert r.caption_lang is None and r.chat["messages"] == 11
    assert any("Caption download failed" in w for w in r.warnings)
    assert store.get_video(conn, VID)["caption_lang"] is None


def test_caption_bot_check_is_fatal(env):
    cfg, conn = env
    yt = FakeYtDlp(caption_error=YtDlpError("bot", kind="bot_check", retryable=True))
    with pytest.raises(IngestError):
        ingest_video(cfg, conn, VID, ytdlp=yt)
    assert store.get_video(conn, VID)["status"] == "error"


def test_skip_captions(env):
    cfg, conn = env
    yt = FakeYtDlp()
    r = ingest_video(cfg, conn, VID, ytdlp=yt, want_captions=False)
    assert yt.caption_calls == 0 and r.caption_lang is None


def test_switching_caption_track_removes_old_file(env):
    cfg, conn = env
    yt = FakeYtDlp(make_info(automatic_captions={"en-orig": []}, language=None))
    r = ingest_video(cfg, conn, VID, ytdlp=yt)
    assert (r.files.root / "captions.en-orig.json3").is_file()

    yt._info = make_info(language="ja")  # now ja-orig is the right track
    r = ingest_video(cfg, conn, VID, ytdlp=yt)
    assert r.caption_lang == "ja-orig"
    assert sorted(p.name for p in r.files.root.glob("captions.*")) == ["captions.ja-orig.json3"]
