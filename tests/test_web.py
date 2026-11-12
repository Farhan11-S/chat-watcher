import json
from pathlib import Path

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
    assert "<svg" in r.text and "Most replayed" in r.text and "stream digest" not in r.text
    assert f'href="/moment/{mid(conn)}"' in r.text and "9/10" in r.text
    r = client.get(f"/moment/{mid(conn)}")
    assert r.status_code == 200
    for part in ("<video", "ぺこら爆笑", "What happens", "Re-cut clip", "/media/frame/", "Analyse again"):
        assert part in r.text
    for gone in ("Storyboard", "Ask Claude", 'name="model"', "/approve"):
        assert gone not in r.text
    assert client.get("/channels").status_code == 200
    assert client.get("/jobs").status_code == 200
    assert "Health check" in client.get("/settings").text                  # merged into Storage
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
    assert client.post(f"/moment/{m}/reject", follow_redirects=False).status_code == 400   # Keep/Skip: review panel only

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
    job = conn.execute("SELECT * FROM jobs WHERE moment_id = ? AND stage = 'analyze'", (m,)).fetchone()
    assert job["params_json"] is None                                       # config's model and effort


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
    assert len(fr.calls) == 1                                # re-analysis of this moment only
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
    assert client.post(f"/video/{VID}/analyze").status_code == 400              # analysis runs by itself


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
    assert "Upload all" not in client.get(f"/video/{VID}").text      # drive off
    r = client.post(f"/video/{VID}/upload", follow_redirects=False)
    assert "error=" in r.headers["location"]
    cfg.drive.enabled = True
    store.upsert_video(conn, VID, drive_url="https://drive.google.com/drive/folders/XYZ")
    page = client.get(f"/video/{VID}").text
    assert "Upload all" in page and 'href="https://drive.google.com/drive/folders/XYZ"' in page
    assert "upload" in client.get("/").text
    client.post(f"/video/{VID}/upload")
    assert any(j["stage"] == "upload" for j in jobs.list_jobs(conn))


def test_drive_login_banner(site, monkeypatch):
    from chatwatcher.upload.login import LoginNotice
    cfg, conn, client, _ = site
    assert "sign-in" not in client.get("/").text
    monkeypatch.setattr("chatwatcher.upload.login.drive_login_notice",
                        lambda cfg, conn, rclone=None: LoginNotice("warn", "Google Drive sign-in expires in 20 hours.",
                                                                   "rclone config reconnect gdrive:"))
    cfg2 = cfg.model_copy(deep=True)
    page = TestClient(create_app(cfg2)).get("/").text
    assert 'class="flash warn"' in page and "expires in 20 hours" in page and "config reconnect gdrive:" in page


def test_auto_upload_switch_and_clip_upload_button(site):
    from chatwatcher import prefs
    cfg, conn, client, _ = site
    m = mid(conn)
    assert "Upload this clip" not in client.get(f"/moment/{m}").text      # drive off
    cfg.drive.enabled = True
    assert "auto-upload on" in client.get(f"/video/{VID}").text
    client.post("/storage/auto-upload", data={})
    assert prefs.auto_upload(cfg, conn) is False
    assert "auto-upload off" in client.get(f"/video/{VID}").text
    assert "Turn on" in client.get("/storage").text
    client.post("/storage/auto-upload", data={"on": "1"})
    assert prefs.auto_upload(cfg, conn) is True

    assert "Upload this clip to Drive" in client.get(f"/moment/{m}").text
    client.post(f"/moment/{m}/upload")
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE stage = 'upload' AND moment_id = ?", (m,)).fetchone()[0] == 1
    r = client.post(f"/video/{VID}/upload", data={"kept_only": "1"}, follow_redirects=False)
    assert "error=" in r.headers["location"]                              # nothing kept yet
    client.post(f"/moment/{m}/status", json={"status": "approved"})
    assert "Upload kept (<span data-kept-clipped>1</span>)" in client.get(f"/video/{VID}").text
    client.post(f"/video/{VID}/upload", data={"kept_only": "1"})
    job = conn.execute("SELECT * FROM jobs WHERE stage = 'upload' AND moment_id IS NULL").fetchone()
    assert json.loads(job["params_json"]) == {"kept_only": True}


def test_storage_page(site):
    cfg, conn, client, _ = site
    page = client.get("/storage").text
    assert "This computer" in page and "free on this disk" in page and "By channel" in page
    assert "Google Drive" not in page                                      # drive off
    assert 'class="segbar"' in page and "Delete streams" in page and "【雑談】テスト" in page
    cfg.drive.enabled = True
    assert "data-drive-refresh" in client.get("/storage").text            # asks Drive in the background


def test_times_are_shown_in_the_configured_zone(site):
    cfg, conn, client, _ = site
    cfg2 = cfg.model_copy(deep=True)
    cfg2.general.timezone = "Asia/Jakarta"
    c2 = TestClient(create_app(cfg2))
    m = mid(conn)
    conn.execute("UPDATE analyses SET created_at = '2026-11-08 05:51:21' WHERE moment_id = ?", (m,))
    assert "at 2026-11-08 12:51 WIB" in c2.get(f"/moment/{m}").text
    jobs.enqueue(conn, 1, "clip", run_after=jobs.now())
    page = c2.get("/jobs").text
    assert "When (WIB)" in page and " WIB" in page and "UTC" not in page


def test_delete_streams_from_storage_page(site, tmp_path):
    from pathlib import Path
    from chatwatcher import discover
    cfg, conn, client, deps = site
    v = store.get_video(conn, VID)
    out = Path(v["output_dir"])
    key = f"{out.parent.name}/{out.name}"
    m = mid(conn)
    assert f'value="{key}"' in client.get("/storage").text

    # Nothing ticked, bad mode, and keys that try to leave output/ are refused.
    assert "error=" in client.post("/storage/delete", data={"mode": "all"}, follow_redirects=False).headers["location"]
    assert client.post("/storage/delete", data={"key": key, "mode": "nuke"}).status_code == 400
    for bad in ("../data", f"{out.parent.name}/../../data", out.parent.name, f"{key}/01_x", "/etc/passwd"):
        r = client.post("/storage/delete", data={"key": bad, "mode": "all"}, follow_redirects=False)
        assert "error=" in r.headers["location"]
    assert out.is_dir() and cfg.paths.data_dir.is_dir()

    # Busy streams are left alone.
    jid = jobs.enqueue(conn, v["id"], "clip")
    conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (jid,))
    r = client.post("/storage/delete", data={"key": key, "mode": "all"}, follow_redirects=False)
    assert "busy" in r.headers["location"] and out.is_dir()
    conn.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (jid,))

    # Videos only: clip files go, everything else stays, and the pages say so.
    clips = list(out.glob("*/clip.mp4"))
    assert clips
    r = client.post("/storage/delete", data={"key": key, "mode": "videos"}, follow_redirects=False)
    assert "Freed" in r.headers["location"]
    assert not any(c.exists() for c in clips) and list(out.glob("*/analysis.json")) and list(out.glob("*/frames/*.jpg"))
    assert "video deleted" in client.get(f"/video/{VID}").text
    page = client.get(f"/moment/{m}").text
    assert "Download it again" in page and "<video" not in page and "What happens" in page
    # ...and "Clip kept" brings back the kept ones without asking Claude again.
    conn.execute("UPDATE moments SET status = 'approved' WHERE id IN (SELECT moment_id FROM clips)")
    client.post(f"/video/{VID}/clip")
    deps.ytdlp = FakeYt(make_video(tmp_path / "again.mp4", 20))
    fr = FakeRunner()
    deps.claude = fr
    Worker(cfg, deps=deps).run(once=True)
    assert all(c.is_file() for c in clips) and fr.calls == []

    # Everything: folder, working data and dashboard entry go; the watcher won't fetch it again.
    r = client.post("/storage/delete", data={"key": key, "mode": "all"}, follow_redirects=False)
    assert "Freed" in r.headers["location"]
    assert not out.exists() and not (cfg.paths.data_dir / "vods" / VID).exists()
    assert store.get_video(conn, VID) is None
    assert conn.execute("SELECT COUNT(*) FROM moments").fetchone()[0] == 0
    seen = conn.execute("SELECT action, reason FROM discovered WHERE video_id = ?", (VID,)).fetchone()
    assert seen["action"] == "skipped" and "deleted" in seen["reason"]
    conn.execute("INSERT INTO channels (id, channel_id, title) VALUES (9, 'UCx', 'Pekora')")
    res = discover._process(cfg, conn, conn.execute("SELECT * FROM channels WHERE id = 9").fetchone(),
                            {"entries": [{"id": VID, "live_status": "was_live", "duration": 3600}]}, backfill=None)
    assert res.queued == []
    assert client.get(f"/video/{VID}").status_code == 404


def test_review_on_the_stream_page(site, tmp_path):
    cfg, conn, client, deps = site
    page = client.get(f"/video/{VID}").text
    assert 'id="yt-player"' in page and 'id="moments-data"' in page and "Clip kept (<span data-kept-count>0</span>)" in page
    data = json.loads(page.split('id="moments-data">')[1].split("</script>")[0])
    assert [m["start"] for m in data] == sorted(m["start"] for m in data)          # stream order
    assert {"id", "rank", "start", "end", "status", "title", "summary", "chat"} <= set(data[0])
    assert all(f'data-goto="{m["id"]}"' in page for m in data)                      # strip, table and chart
    assert "</script><" not in page.split('id="moments-data">')[1].split("</script>")[0]

    # Keep / Skip / Undo without a page load, with the counts the buttons need.
    a, b = data[0]["id"], data[1]["id"]
    r = client.post(f"/moment/{a}/status", json={"status": "approved"}).json()
    assert r == {"ok": True, "status": "approved", "kept": 1, "kept_clipped": 1, "to_review": len(data) - 1}
    assert client.post(f"/moment/{b}/status", json={"status": "rejected"}).json()["kept"] == 1
    assert client.post(f"/moment/{b}/status", json={"status": "delete"}).status_code == 400
    assert client.post("/moment/99999/status", json={"status": "new"}).status_code == 404
    page = client.get(f"/video/{VID}").text
    assert "Clip kept (<span data-kept-count>1</span>)" in page

    # "Clip kept" downloads only kept moments that aren't downloaded yet.
    conn.execute("DELETE FROM clips")
    client.post(f"/moment/{a}/status", json={"status": "new"})
    r = client.post(f"/video/{VID}/clip", follow_redirects=False)
    assert "error=" in r.headers["location"]                                        # nothing kept
    client.post(f"/moment/{a}/status", json={"status": "approved"})
    r = client.post(f"/video/{VID}/clip", follow_redirects=False)
    assert "Downloading%201%20kept" in r.headers["location"]
    deps.ytdlp = FakeYt(make_video(tmp_path / "kept.mp4", 20))
    fr = FakeRunner()
    deps.claude = fr
    Worker(cfg, deps=deps).run(once=True)
    clipped = [r["moment_id"] for r in conn.execute("SELECT moment_id FROM clips")]
    assert clipped == [a]
    stages = [r["stage"] for r in conn.execute(
        "SELECT stage FROM jobs WHERE video_id = (SELECT id FROM videos WHERE video_id = ?) "
        "AND moment_id IS NULL AND status = 'done' ORDER BY updated_at, id", (VID,))]
    assert stages[-3:] == ["clip", "frames", "analyze"]                             # analysed after clipping


def test_home_shows_what_is_left_to_review(site):
    _, conn, client, _ = site
    n = conn.execute("SELECT COUNT(*) FROM moments").fetchone()[0]
    conn.execute("UPDATE moments SET status = 'approved' WHERE rank = 1")
    assert f"1 ✓ · 0 ✗ · <b>{n - 1} to review</b>" in client.get("/").text
    conn.execute("UPDATE moments SET status = 'rejected' WHERE rank > 1")
    page = client.get("/").text
    assert f"1 ✓ · {n - 1} ✗" in page and "to review</b>" not in page


def test_storage_page_after_deleting_everything(site, monkeypatch):
    from pathlib import Path
    from chatwatcher import storage
    cfg, conn, client, _ = site
    out = Path(store.get_video(conn, VID)["output_dir"])
    key = f"{out.parent.name}/{out.name}"
    client.post("/storage/delete", data={"key": key, "mode": "all"})
    page = client.get("/storage").text
    assert "Nothing from chat-watcher takes space on this computer" in page
    assert "data-delete-form" not in page and "Only on Google Drive" not in page

    # The stream is still on Drive: list it on its own, by its title, without delete boxes.
    cfg.drive.enabled = True
    fake = storage.DriveUsage(checked_at=__import__("time").time(), total=10**12, used=10**9, other=0,
                              free=10**12 - 10**9, ours=123_000, files=3,
                              by_path={out.parent.name: 123_000, key: 123_000})
    monkeypatch.setattr(storage, "drive_usage", lambda cfg, rclone=None: fake)
    client.post("/storage/drive")
    page = client.get("/storage").text
    assert "Only on Google Drive" in page and "【雑談】テスト" in page and f"https://youtu.be/{VID}" in page
    assert out.name not in page                     # no raw folder name
    assert 'name="key"' not in page and "none here" in page


def test_folder_label():
    from chatwatcher.storage import folder_label
    assert folder_label("2026-11-07_nMtDSls-ZAo_【UNDERTALE】_I_chose_violence", {"nMtDSls-ZAo": "【UNDERTALE】 I chose violence."}) \
        == ("【UNDERTALE】 I chose violence. (2026-11-07)", "nMtDSls-ZAo")
    assert folder_label("2026-11-08_-k8EZv9wkAs_some_title", {}) == ("some title (2026-11-08)", "-k8EZv9wkAs")
    assert folder_label("odd folder", {}) == ("odd folder", None)


def test_storage_page_with_a_stream_that_just_started(site, monkeypatch):
    from pathlib import Path
    from chatwatcher import storage
    cfg, conn, client, _ = site
    out = Path(store.get_video(conn, VID)["output_dir"])
    fresh = out.parent / "2026-11-08_abcdefghijk_new_stream"
    fresh.mkdir()                                   # folder made, nothing downloaded yet
    cfg.drive.enabled = True                        # Drive on, usage not loaded yet
    r = client.get("/storage")
    assert r.status_code == 200 and "Only on Google Drive" not in r.text
    fake = storage.DriveUsage(checked_at=__import__("time").time(), total=10**12, used=10**9, other=0,
                              free=10**12 - 10**9, ours=0, files=0, by_path={})
    monkeypatch.setattr(storage, "drive_usage", lambda cfg, rclone=None: fake)
    client.post("/storage/drive")                   # loaded: the new stream has 0 bytes there too
    r = client.get("/storage")
    assert r.status_code == 200 and "Only on Google Drive" not in r.text


def test_remove_stream_from_the_queue(site):
    """A stream that failed before it had a folder (e.g. members-only) can be removed with its jobs."""
    cfg, conn, client, _ = site
    v = conn.execute("INSERT INTO videos (video_id, title, status) VALUES ('membersonly', 'Members stream', 'error')").lastrowid
    conn.execute("INSERT INTO jobs (video_id, stage, status, error) VALUES (?, 'ingest', 'error', 'Members-only video')", (v,))
    page = client.get("/jobs").text
    assert "/video/membersonly/remove" in page and "Remove stream" in page
    r = client.post("/video/membersonly/remove", data={"back_to": "jobs"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/jobs?msg=Removed")
    assert not conn.execute("SELECT 1 FROM videos WHERE video_id = 'membersonly'").fetchone()
    assert not conn.execute("SELECT 1 FROM jobs WHERE video_id = ?", (v,)).fetchone()
    assert conn.execute("SELECT action FROM discovered WHERE video_id = 'membersonly'").fetchone()["action"] == "skipped"

    # A processed stream: its folder goes too, and a busy one is refused.
    row = conn.execute("SELECT id, output_dir FROM videos WHERE video_id = ?", (VID,)).fetchone()
    assert "Remove stream" in client.get(f"/video/{VID}").text
    conn.execute("INSERT INTO jobs (video_id, stage, status) VALUES (?, 'upload', 'running')", (row["id"],))
    assert "error=" in client.post(f"/video/{VID}/remove", follow_redirects=False).headers["location"]
    conn.execute("DELETE FROM jobs WHERE status = 'running'")
    r = client.post(f"/video/{VID}/remove", follow_redirects=False)
    assert r.headers["location"].startswith("/?msg=Removed")
    assert not Path(row["output_dir"]).exists() and client.get(f"/video/{VID}").status_code == 404


def test_home_filters_streams_by_local_day_and_shows_the_top_rating(site):
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    from chatwatcher import clock
    cfg, conn, client, _ = site
    tz = clock.zone(cfg)
    now = datetime.now(timezone.utc)
    conn.execute("UPDATE videos SET published_at = ? WHERE video_id = ?", (now.strftime("%Y-%m-%dT%H:%M:%SZ"), VID))
    page = client.get("/?day=today").text
    assert f'href="/video/{VID}"' in page and "· today · 1" in page
    assert f'href="/video/{VID}"' not in client.get("/?day=yesterday").text
    # Three days ago: only a range that covers it shows it.
    old = now - timedelta(days=3)
    conn.execute("UPDATE videos SET published_at = ? WHERE video_id = ?", (old.strftime("%Y-%m-%dT%H:%M:%SZ"), VID))
    day = old.astimezone(tz).date()
    assert f'href="/video/{VID}"' not in client.get("/?day=today").text
    hit = client.get("/", params={"from": (day - timedelta(days=1)).isoformat(), "to": day.isoformat()}).text
    assert f'href="/video/{VID}"' in hit
    swapped = client.get("/", params={"from": day.isoformat(), "to": (day - timedelta(days=1)).isoformat()}).text
    assert f'href="/video/{VID}"' in swapped                                     # from/to the wrong way round
    assert f'href="/video/{VID}"' in client.get("/", params={"from": day.isoformat()}).text    # open-ended
    assert "No streams in that range" in client.get("/", params={"from": "2001-01-01", "to": "2001-01-02"}).text
    bad = client.get("/", params={"from": "nope"})
    assert bad.status_code == 200 and "Dates must look like" in bad.text and f'href="/video/{VID}"' in bad.text
    # The top rating links to the moment Claude rated highest.
    page = client.get("/").text
    best = conn.execute("""SELECT m.id FROM moments m JOIN analyses a ON a.moment_id = m.id
                           ORDER BY json_extract(a.analysis_json, '$.clip_worthiness') DESC, m.rank LIMIT 1""").fetchone()["id"]
    assert "Top rating" in page and f'href="/moment/{best}" title="Moment #' in page and "<small>/10</small>" in page
    # Local days: a stream at 00:30 WIB is that day, though it's the day before in UTC.
    lo, hi = clock.local_days(day, day, ZoneInfo("Asia/Jakarta"))
    assert lo.endswith("17:00:00") and hi > lo


def test_home_search_filters_sort_and_pages(site):
    cfg, conn, client, _ = site
    for i in range(45):                                  # 46 streams: three pages of 20
        store.upsert_video(conn, f"filler{i:05d}", title=f"Filler stream {i}", status="done")
    page = client.get("/").text
    assert 'aria-label="Pages"' in page and "1–20 of 46 streams" in page and 'href="/?page=2"' in page
    assert page.count('class="stream"') == 20
    last = client.get("/?page=3").text
    assert "41–46 of 46 streams" in last and last.count('class="stream"') == 6
    assert client.get("/?page=99").text.count('class="stream"') == 6      # past the end: the last page
    assert client.get("/?page=x&channel=").status_code == 200              # junk and empty selects are ignored

    hit = client.get("/", params={"q": "テスト"}).text                      # the title, not the fillers
    assert f'href="/video/{VID}"' in hit and "Filler stream" not in hit and "Clear filters" in hit
    assert "No streams match these filters" in client.get("/", params={"q": "nothing like this"}).text
    assert "Filler stream 7<" in client.get("/", params={"q": "stream 7"}).text
    assert client.get("/", params={"q": "100%_"}).status_code == 200       # LIKE wildcards are plain text

    review = client.get("/?status=review").text                            # only the scored stream has moments
    assert f'href="/video/{VID}"' in review and "Filler stream" not in review
    assert f'href="/video/{VID}"' in client.get("/?status=clips").text
    conn.execute("UPDATE moments SET status = 'approved'")
    assert f'href="/video/{VID}"' not in client.get("/?status=review").text
    assert f'href="/video/{VID}"' in client.get("/?status=reviewed").text
    best = client.get("/?sort=rating").text                                # the rated stream comes first
    assert best.index(f'href="/video/{VID}"') < best.index("Filler stream")
    # the pager keeps the other filters
    assert 'href="/?q=Filler&amp;page=2"' in client.get("/", params={"q": "Filler"}).text


def test_clips_page_lists_moments_of_every_stream(site):
    cfg, conn, client, _ = site
    n = conn.execute("SELECT COUNT(*) FROM moments").fetchone()[0]
    page = client.get("/clips").text
    assert page.count('class="clip-card') == n and f'href="/moment/{mid(conn)}"' in page
    assert "ぺこら爆笑" in page and "/media/frame/" in page                  # Claude's title, the clip's peak frame
    assert f'All moments <span class="n">{n}</span>' in page and f'Downloaded <span class="n">{n}</span>' in page
    assert 'Kept <span class="n">0</span>' in page
    assert "No moments match" in client.get("/clips?show=kept").text
    client.post(f"/moment/{mid(conn)}/status", json={"status": "approved"})
    kept = client.get("/clips?show=kept").text
    assert kept.count('class="clip-card') == 1 and ">kept<" in kept
    assert client.get("/clips?rating=9").text.count('class="clip-card') == n      # FakeRunner rates 9
    assert "No moments match" in client.get("/clips?rating=9&category=nope").text
    assert client.get("/clips", params={"q": "ぺこら"}).text.count('class="clip-card') == n
    assert "No moments match" in client.get("/clips", params={"q": "zzzz"}).text
    assert client.get("/clips?sort=new&page=5&channel=x").status_code == 200


def test_queue_tabs_search_and_pages(site):
    cfg, conn, client, _ = site
    for i in range(40):                                  # one job per stream and stage
        pk = store.upsert_video(conn, f"queued{i:05d}", title=f"Queued {i}", status="done")
        conn.execute("INSERT INTO jobs (video_id, stage, status) VALUES (?, 'score', 'done')", (pk,))
    bad = store.upsert_video(conn, "brokenvid01", title="Broken one", status="error")
    conn.execute("INSERT INTO jobs (video_id, stage, status, error) VALUES (?, 'ingest', 'error', 'boom')", (bad,))
    conn.commit()
    page = client.get("/jobs").text
    assert 'All <span class="n">41</span>' in page and 'Failed <span class="n">1</span>' in page
    assert "1–30 of 41 jobs" in page and "boom" in page                    # failures come first
    failed = client.get("/jobs?status=error").text
    assert "Broken one" in failed and failed.count("<tr>") == 2            # the header and the one job
    assert "Broken one" not in client.get("/jobs?status=done").text
    assert "Broken one" in client.get("/jobs", params={"q": "broken"}).text
    assert "No jobs match" in client.get("/jobs?stage=upload").text
    assert "31–41 of 41 jobs" in client.get("/jobs?page=2").text


def test_seen_streams_are_paged_and_filtered(site):
    cfg, conn, client, _ = site
    for i in range(25):
        conn.execute("INSERT INTO discovered (video_id, title, action, reason, seen_at) VALUES (?, ?, ?, 'test', datetime('now', ?))",
                     (f"seen{i:07d}", f"Seen stream {i}", "queued" if i % 5 == 0 else "skipped", f"-{i} minutes"))
    conn.commit()
    page = client.get("/channels").text
    assert "Recently seen streams" in page and "1–20 of 25 streams" in page and 'href="/channels?sp=2#seen"' in page
    assert "Seen stream 24" in client.get("/channels?sp=2").text
    queued = client.get("/channels?seen=queued").text
    assert "Seen stream 20" in queued and "Seen stream 21" not in queued
    assert "Seen stream 13" in client.get("/channels", params={"sq": "stream 13"}).text
    assert "No seen streams match" in client.get("/channels", params={"sq": "zzzz"}).text
