"""Low-res previews → Claude analyses every moment → Keep → full-quality clip reuses it."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chatwatcher import db, jobs, store
from chatwatcher.analyze.service import analysis_matches, carry_analysis
from chatwatcher.config import Config, PathsConfig
from chatwatcher.discover import add_channel
from chatwatcher.pipeline import Deps
from chatwatcher.web.app import create_app
from chatwatcher.worker import Worker

from test_analyze import FakeRunner
from test_automation import FakeYouTube, entry


class RecordingYouTube(FakeYouTube):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.formats: list[str] = []

    def run(self, args, *, timeout, on_line=None):
        self.formats.append(args[args.index("-f") + 1])
        return super().run(args, timeout=timeout, on_line=on_line)


@pytest.fixture
def reviewed(tmp_path):
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data", output_dir=tmp_path / "out"))
    cfg.pipeline.auto_clip = False
    cfg.clip.frames_per_clip = 2
    conn = db.open_db(cfg.paths.db_path)
    yt = RecordingYouTube(tmp_path, [entry("streamone01")])
    add_channel(cfg, conn, "@usadapekora", backfill=1, ytdlp=yt)
    fr = FakeRunner()
    Worker(cfg, deps=Deps(ytdlp=yt, claude=fr)).run(once=True)
    yield cfg, conn, yt, fr
    conn.close()


def test_every_moment_is_previewed_and_analysed_before_review(reviewed):
    cfg, conn, yt, fr = reviewed
    stages = [r["stage"] for r in jobs.list_jobs(conn, include_done=True)]
    assert stages == ["ingest", "score", "frames", "analyze"]
    n = conn.execute("SELECT COUNT(*) FROM moments").fetchone()[0]
    assert n >= 2
    assert conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 0          # nothing at full quality
    assert conn.execute("SELECT COUNT(*) FROM previews").fetchone()[0] == n
    assert all(f.startswith("bv*[height<=360]") for f in yt.formats)               # low-res, video only
    for p in conn.execute("SELECT * FROM previews"):
        folder = Path(p["folder"])
        assert folder.is_relative_to(cfg.paths.data_dir)                            # not in the editing folder
        assert not list(folder.glob("preview.*"))                                   # video deleted, frames kept
        assert list(folder.glob("frames/*.jpg")) and (folder / "storyboard.md").is_file()
    analysed = conn.execute("SELECT analysis_json FROM analyses WHERE moment_id IS NOT NULL").fetchall()
    assert len(analysed) == n
    meta = json.loads(analysed[0]["analysis_json"])["_meta"]
    assert meta["source"] == "preview" and meta["range"] and "zero_s" in meta
    v = store.get_video(conn, "streamone01")
    assert (Path(v["output_dir"]) / "stream_digest.md").is_file()                  # digest covers every moment
    assert all(c["cwd"].is_relative_to(cfg.paths.data_dir) for c in fr.calls if c.get("allow_read"))


def test_clip_kept_downloads_full_quality_and_reuses_the_analysis(reviewed):
    cfg, conn, yt, _ = reviewed
    v = store.get_video(conn, "streamone01")
    m1 = conn.execute("SELECT id FROM moments WHERE rank = 1").fetchone()["id"]
    conn.execute("UPDATE moments SET status = 'approved' WHERE id = ?", (m1,))
    before = conn.execute("SELECT id FROM analyses WHERE moment_id = ?", (m1,)).fetchone()["id"]
    yt.formats.clear()
    jobs.enqueue(conn, v["id"], "clip", params={"kept_only": True})
    fr = FakeRunner()
    Worker(cfg, deps=Deps(ytdlp=yt, claude=fr)).run(once=True)
    assert fr.calls == []                                                           # Claude not asked again
    assert yt.formats and not yt.formats[0].startswith("bv*[height<=")             # best quality this time
    clip = conn.execute("SELECT * FROM clips").fetchall()
    assert [c["moment_id"] for c in clip] == [m1]
    folder = Path(clip[0]["folder"])
    assert folder.is_relative_to(cfg.paths.output_dir) and (folder / "clip.mp4").is_file()
    assert (folder / "storyboard.md").is_file() and (folder / "analysis.json").is_file()
    a = json.loads(conn.execute("SELECT analysis_json FROM analyses WHERE moment_id = ?", (m1,)).fetchone()[0])
    assert a["_meta"]["source"] == "clip"
    assert conn.execute("SELECT id FROM analyses WHERE moment_id = ? ORDER BY id DESC", (m1,)).fetchone()["id"] == before
    frames = [f["file"] for f in json.loads(clip[0]["frames_json"])]
    assert a["thumbnail"]["frame_file"] in frames

    # A re-cut makes the analysis stale: it no longer carries over.
    assert analysis_matches(conn, m1)
    conn.execute("UPDATE moments SET start_s = start_s - 30 WHERE id = ?", (m1,))
    assert not analysis_matches(conn, m1) and not carry_analysis(conn, m1, folder)


def test_carry_over_shifts_times_onto_the_new_file(reviewed):
    cfg, conn, yt, _ = reviewed
    m1 = conn.execute("SELECT id FROM moments WHERE rank = 1").fetchone()["id"]
    p = conn.execute("SELECT folder FROM previews WHERE moment_id = ?", (m1,)).fetchone()["folder"]
    a = json.loads(conn.execute("SELECT analysis_json FROM analyses WHERE moment_id = ?", (m1,)).fetchone()[0])
    # Pretend the full-quality file starts 2 s earlier in the stream than the preview did.
    meta = json.loads((Path(p) / "moment.json").read_text())
    meta["clip"]["zero_s"] -= 2
    meta["clip"]["duration_s"] += 2
    hq = cfg.paths.output_dir / "hq"
    hq.mkdir(parents=True)
    (hq / "moment.json").write_text(json.dumps(meta))
    assert carry_analysis(conn, m1, hq)
    b = json.loads((hq / "analysis.json").read_text())
    assert b["cut"]["long_in"] == pytest.approx(a["cut"]["long_in"] + 2)
    assert b["storyboard"][0]["start"] == pytest.approx(a["storyboard"][0]["start"] + 2)
    assert b["captions"][0]["at"] == pytest.approx(a["captions"][0]["at"] + 2)


def test_review_pages_use_the_preview(reviewed):
    cfg, conn, _, _ = reviewed
    client = TestClient(create_app(cfg, deps=Deps(claude=FakeRunner())))
    m1 = conn.execute("SELECT id FROM moments WHERE rank = 1").fetchone()["id"]
    page = client.get(f"/moment/{m1}").text
    assert "Download full quality now" in page and f"/media/preview/{m1}/" in page and "Storyboard" in page
    frame = page.split(f"/media/preview/{m1}/")[1].split('"')[0]
    assert client.get(f"/media/preview/{m1}/{frame}").headers["content-type"] == "image/jpeg"
    assert client.get(f"/media/preview/{m1}/..%2Fmoment.json").status_code == 404
    assert client.post(f"/moment/{m1}/ask", json={"question": "who?"}).json()["ok"]
    page = client.get("/video/streamone01").text
    assert 'class="w w9"' in page and "data-keep-picks" in page and "✓ preview" in page
