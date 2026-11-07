import json

import pytest
from fastapi.testclient import TestClient

from chatwatcher import db, jobs, store
from chatwatcher.analyze.service import analyze_video
from chatwatcher.clip.service import clip_video
from chatwatcher.config import Config, PathsConfig
from chatwatcher.pipeline import Deps
from chatwatcher.scoring.service import score_video
from chatwatcher.web import charts
from chatwatcher.web.app import _parse_ts, create_app
from chatwatcher.worker import Worker

from scoring_helpers import background, burst, chat
from test_analyze import FakeRunner
from test_automation import FakeYouTube, entry
from test_clip import FakeYt, make_video
from test_score_service import VID, ingest_fake


@pytest.fixture
def site(tmp_path, monkeypatch):
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data", output_dir=tmp_path / "out"))
    cfg.clip.frames_per_clip = 2
    conn = db.open_db(cfg.paths.db_path)
    hm = [[i * 36, (i + 1) * 36, 0.2 + (0.7 if i == 33 else 0)] for i in range(100)]
    ingest_fake(cfg, conn, heatmap=hm, messages=chat(background(3600), burst(1200, "草", n=60),
                                                      burst(2500, "かわいい", n=50, prefix="c")))
    store.upsert_video(conn, VID, duration_s=3600, status="done")
    score_video(cfg, conn, VID)
    src = make_video(tmp_path / "src.mp4", 20)
    clip_video(cfg, conn, VID, ytdlp=FakeYt(src))
    analyze_video(cfg, conn, VID, runner=FakeRunner())
    monkeypatch.setattr("chatwatcher.doctor.run_checks", lambda cfg: [])
    deps = Deps(ytdlp=FakeYouTube(tmp_path, [entry("newest00001")]), claude=FakeRunner())
    client = TestClient(create_app(cfg, deps=deps))
    yield cfg, conn, client, deps
    conn.close()


def mid(conn, rank=1):
    return conn.execute("SELECT id FROM moments WHERE rank = ?", (rank,)).fetchone()["id"]


def test_pages_render(site):
    cfg, conn, client, _ = site
    r = client.get("/")
    assert r.status_code == 200 and "Streams" in r.text and "【雑談】テスト" in r.text
    r = client.get(f"/video/{VID}")
    assert r.status_code == 200
    assert "<svg" in r.text and "Most replayed" in r.text and "Claude's stream digest" in r.text
    assert f'href="/moment/{mid(conn)}"' in r.text and "9/10" in r.text
    r = client.get(f"/moment/{mid(conn)}")
    assert r.status_code == 200
    for part in ("<video", "ぺこら爆笑", "Storyboard", "Captions", "Re-cut clip", "/media/frame/"):
        assert part in r.text
    assert client.get("/channels").status_code == 200
    assert client.get("/jobs").status_code == 200
    assert client.get("/settings").status_code == 200
    assert client.get("/video/zzzzzzzzzzz").status_code == 404
    assert client.get("/moment/99999").status_code == 404


def test_media_and_path_safety(site):
    _, conn, client, _ = site
    clip = conn.execute("SELECT * FROM clips ORDER BY id LIMIT 1").fetchone()
    r = client.get(f"/media/clip/{clip['id']}", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100
    frame = json.loads(clip["frames_json"])[0]["file"].split("/")[-1]
    assert client.get(f"/media/frame/{clip['id']}/{frame}").headers["content-type"] == "image/jpeg"
    assert client.get(f"/media/frame/{clip['id']}/..%2Fclip.mp4").status_code == 404
    assert client.get(f"/media/frame/{clip['id']}/moment.json").status_code == 404
    assert client.get("/media/clip/9999").status_code == 404


def test_moment_actions(site):
    _, conn, client, _ = site
    m = mid(conn)
    r = client.post(f"/moment/{m}/reject", follow_redirects=False)
    assert r.status_code == 303
    assert conn.execute("SELECT status FROM moments WHERE id = ?", (m,)).fetchone()[0] == "rejected"
    client.post(f"/moment/{m}/reset")
    assert conn.execute("SELECT status FROM moments WHERE id = ?", (m,)).fetchone()[0] == "new"

    r = client.post(f"/moment/{m}/cut", data={"start": "19:50", "end": "20:40"}, follow_redirects=False)
    assert "New%20cut" in r.headers["location"]
    row = conn.execute("SELECT start_s, end_s FROM moments WHERE id = ?", (m,)).fetchone()
    assert (row["start_s"], row["end_s"]) == (1190.0, 1240.0)
    job = conn.execute("SELECT * FROM jobs WHERE moment_id = ?", (m,)).fetchone()
    assert job["stage"] == "clip" and job["status"] == "pending"

    r = client.post(f"/moment/{m}/cut", data={"start": "20:00", "end": "20:02"}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    r = client.post(f"/moment/{m}/cut", data={"start": "abc", "end": "1:00"}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    client.post(f"/moment/{m}/reanalyze")
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE moment_id = ? AND stage = 'analyze'", (m,)).fetchone()[0] == 1


def test_recut_runs_clip_then_analysis(site, tmp_path):
    cfg, conn, client, deps = site
    m = mid(conn)
    old_folder = conn.execute("SELECT folder FROM clips WHERE moment_id = ?", (m,)).fetchone()["folder"]
    client.post(f"/moment/{m}/cut", data={"start": "19:00", "end": "20:30"})
    deps.ytdlp = FakeYt(make_video(tmp_path / "src2.mp4", 20))
    fr = FakeRunner()
    deps.claude = fr
    Worker(cfg, deps=deps).run(once=True)
    clip = conn.execute("SELECT * FROM clips WHERE moment_id = ?", (m,)).fetchone()
    assert clip["req_start_s"] == 1140.0 and clip["folder"].endswith("_19m00s")
    assert clip["folder"] != old_folder                      # renamed, not duplicated
    assert len(fr.calls) == 2                                # re-analysis + digest
    stages = [r["stage"] for r in conn.execute("SELECT stage FROM jobs WHERE moment_id = ? AND status = 'done'", (m,))]
    assert sorted(stages) == ["analyze", "clip"]


def test_video_and_queue_actions(site):
    _, conn, client, _ = site
    r = client.post("/add", data={"url": "https://youtu.be/abcdefghij2"}, follow_redirects=False)
    assert r.headers["location"].startswith("/video/abcdefghij2")
    assert any(j["yt_id"] == "abcdefghij2" for j in jobs.list_jobs(conn))
    assert "error=" in client.post("/add", data={"url": "nope"}, follow_redirects=False).headers["location"]

    r = client.post(f"/video/{VID}/rescore", data={"stream_type": "gaming"}, follow_redirects=False)
    assert "gaming" in r.headers["location"]
    assert store.get_video(conn, VID)["stream_type"] == "gaming"
    assert any(j["stage"] == "clip" and j["yt_id"] == VID for j in jobs.list_jobs(conn))

    job = jobs.list_jobs(conn)[0]
    assert client.post(f"/jobs/{job['id']}/retry", follow_redirects=False).status_code == 303
    v1 = client.get("/api/version").json()["v"]
    client.post(f"/video/{VID}/analyze")
    assert client.get("/api/version").json()["v"] != v1


def test_channel_pages(site):
    _, conn, client, _ = site
    r = client.post("/channels", data={"channel": "@usadapekora", "stream_type": "karaoke", "backfill": "1"},
                    follow_redirects=False)
    assert "Watching" in r.headers["location"]
    row = conn.execute("SELECT * FROM channels WHERE enabled = 1").fetchone()
    assert row["default_type"] == "karaoke"
    client.post(f"/channels/{row['id']}/type", data={"stream_type": "gaming"})
    client.post(f"/channels/{row['id']}/disable")
    row = conn.execute("SELECT * FROM channels WHERE id = ?", (row["id"],)).fetchone()
    assert (row["default_type"], row["enabled"]) == ("gaming", 0)
    assert "newest00001" in client.get("/channels").text
    client.post(f"/channels/{row['id']}/remove")
    assert conn.execute("SELECT COUNT(*) FROM channels WHERE id = ?", (row["id"],)).fetchone()[0] == 0


def test_charts():
    scores = {"bin_s": 5, "score": [0.1] * 200 + [2.0] * 4 + [0.1] * 296}
    moments = [{"id": 7, "rank": 1, "start_s": 990, "end_s": 1040, "score": 2.0, "kind": "song", "status": "new"}]
    c = charts.score_timeline(scores, moments, 2500, 1.0)
    assert c["svg"].startswith("<svg") and 'href="/moment/7"' in c["svg"] and "#1 ♪" in c["svg"]
    hover = json.loads(c["hover"])
    assert max(v for _, v in hover["points"]) == 2.0         # peaks survive downsampling
    h = charts.heatmap_strip([[0, 25, 0.1], [25, 50, 1.0]], 50)
    assert "series-line s2" in h["svg"]


@pytest.mark.parametrize("text,secs", [("1:02:03", 3723), ("62:03", 3723), ("3723", 3723), ("90.5", 90.5)])
def test_parse_ts(text, secs):
    assert _parse_ts(text) == secs


def test_parallel_requests(site):
    """Browsers fetch many frames at once; requests must not share/cross threads badly."""
    from concurrent.futures import ThreadPoolExecutor
    _, conn, client, _ = site
    clip = conn.execute("SELECT * FROM clips ORDER BY id LIMIT 1").fetchone()
    names = [f["file"].split("/")[-1] for f in json.loads(clip["frames_json"])] * 6
    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(lambda n: client.get(f"/media/frame/{clip['id']}/{n}").status_code, names))
    assert set(codes) == {200}


def test_drive_link_and_upload_button(site):
    cfg, conn, client, _ = site
    assert "Upload to Drive" not in client.get(f"/video/{VID}").text      # drive off
    r = client.post(f"/video/{VID}/upload", follow_redirects=False)
    assert "error=" in r.headers["location"]
    cfg.drive.enabled = True
    store.upsert_video(conn, VID, drive_url="https://drive.google.com/drive/folders/XYZ")
    page = client.get(f"/video/{VID}").text
    assert "Upload to Drive" in page and 'href="https://drive.google.com/drive/folders/XYZ"' in page
    assert "upload" in client.get("/").text
    client.post(f"/video/{VID}/upload")
    assert any(j["stage"] == "upload" for j in jobs.list_jobs(conn))
