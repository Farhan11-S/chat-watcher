import json

import pytest
from typer.testing import CliRunner

from chatwatcher import db, store
from chatwatcher.cli import app
from chatwatcher.config import Config, PathsConfig
from chatwatcher.ingest.captions import Segment, write_transcript
from chatwatcher.ingest.chat_parser import write_messages
from chatwatcher.paths import vod_files
from chatwatcher.scoring.service import ScoreError, score_video

from scoring_helpers import background, burst, chat

VID = "abcdefghijk"


@pytest.fixture
def env(tmp_path):
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data", output_dir=tmp_path / "out"))
    conn = db.open_db(cfg.paths.db_path)
    yield cfg, conn
    conn.close()


def ingest_fake(cfg, conn, title="【雑談】テスト", duration=3600, messages=None, heatmap=None,
                transcript=True):
    f = vod_files(cfg, VID)
    f.root.mkdir(parents=True)
    f.info.write_text(json.dumps({"id": VID, "title": title, "duration": duration}))
    msgs = messages if messages is not None else chat(
        background(duration), burst(1200, "草", n=60), burst(2500, "かわいい", n=50, prefix="c"))
    if msgs:
        write_messages(f.chat, msgs)
    if transcript:
        write_transcript(f.transcript, [Segment(1150, 1170, "それでさ、"), Segment(1170, 1185, "こうなったの。")])
    if heatmap:
        f.heatmap.write_text(json.dumps(heatmap))
    store.upsert_video(conn, VID, title=title, data_dir=str(f.root))
    return f


def test_score_saves_moments_and_files(env):
    cfg, conn = env
    f = ingest_fake(cfg, conn)
    r = score_video(cfg, conn, VID)
    assert r.stream_type == "talk" and r.type_reason == "title: 雑談"
    assert r.moments and r.warnings == []
    rows = conn.execute("SELECT * FROM moments ORDER BY rank").fetchall()
    assert len(rows) == len(r.moments) and rows[0]["rank"] == 1
    feats = json.loads(rows[0]["features_json"])
    assert feats["reasons"] and feats["top_messages"]
    assert any(row["start_s"] < 1200 < row["end_s"] for row in rows)
    v = store.get_video(conn, VID)
    assert v["detected_type"] == "talk" and v["scored_at"]
    scores = json.loads(f.scores.read_text())
    assert scores["n_bins"] == 720 and len(scores["score"]) == 720 and "laugh" in scores["z"]
    assert json.loads(f.moments.read_text())["moments"][0]["rank"] == 1


def test_rescore_replaces_but_guards_reviewed(env):
    cfg, conn = env
    ingest_fake(cfg, conn)
    score_video(cfg, conn, VID)
    n = conn.execute("SELECT COUNT(*) FROM moments").fetchone()[0]
    score_video(cfg, conn, VID)
    assert conn.execute("SELECT COUNT(*) FROM moments").fetchone()[0] == n

    conn.execute("UPDATE moments SET status = 'approved' WHERE rank = 1")
    with pytest.raises(ScoreError, match="--force"):
        score_video(cfg, conn, VID)
    score_video(cfg, conn, VID, force=True)
    assert conn.execute("SELECT COUNT(*) FROM moments WHERE status='approved'").fetchone()[0] == 0


def test_type_override_order(env):
    cfg, conn = env
    ingest_fake(cfg, conn)
    assert score_video(cfg, conn, VID, stream_type="gaming").stream_type == "gaming"
    store.upsert_video(conn, VID, stream_type="karaoke")
    r = score_video(cfg, conn, VID)
    assert (r.stream_type, r.type_reason) == ("karaoke", "set on video")
    with pytest.raises(ScoreError):
        score_video(cfg, conn, VID, stream_type="asmr")


def test_heatmap_only(env):
    cfg, conn = env
    hm = [[i * 36, (i + 1) * 36, 0.9 if i == 50 else 0.1 + 0.001 * i] for i in range(100)]
    ingest_fake(cfg, conn, messages=[], heatmap=hm, transcript=False)
    r = score_video(cfg, conn, VID)
    assert any("heatmap only" in w for w in r.warnings)
    assert r.moments and r.moments[0].start_s < 1818 < r.moments[0].end_s + 36


def test_errors(env):
    cfg, conn = env
    with pytest.raises(ScoreError, match="ingest"):
        score_video(cfg, conn, VID)
    ingest_fake(cfg, conn, messages=[], transcript=False)
    with pytest.raises(ScoreError, match="Nothing to score"):
        score_video(cfg, conn, VID)


def test_bad_config_reported(env):
    cfg, conn = env
    ingest_fake(cfg, conn)
    cfg.scoring.extra_keywords = {"funny": ["x"]}
    with pytest.raises(ScoreError, match="Unknown reaction"):
        score_video(cfg, conn, VID)


def test_cli_score(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('[paths]\ndata_dir = "data"\noutput_dir = "out"\n')
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data", output_dir=tmp_path / "out"))
    conn = db.open_db(cfg.paths.db_path)
    ingest_fake(cfg, conn)
    conn.close()
    r = CliRunner().invoke(app, ["score", VID, "-c", str(f), "--top", "3"])
    assert r.exit_code == 0, r.output
    assert "talk" in r.output and " 1. " in r.output and f"youtu.be/{VID}?t=" in r.output
    r = CliRunner().invoke(app, ["score", "zzzzzzzzzzz", "-c", str(f)])
    assert r.exit_code == 1
