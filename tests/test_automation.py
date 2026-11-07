import json
import shutil
import stat
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chatwatcher import db, jobs, store
from chatwatcher.analyze.service import AnalyzeRun, AnalyzeOutcome
from chatwatcher.cli import app
from chatwatcher.config import Config, PathsConfig
from chatwatcher.discover import DiscoverError, add_channel, check_channel, due_channels, find_channel, streams_url
from chatwatcher.ingest.service import IngestError
from chatwatcher.pipeline import Deps, run_stage, stage_enabled, until_utc_midnight, usage_reset_delay
from chatwatcher.worker import Worker, WorkerLocked, run_now

from scoring_helpers import background, burst, chat
from test_analyze import FakeRunner
from test_clip import make_video

UC = "UC1DCedRgGHBdm81E1llLhOQ"


@pytest.fixture
def env(tmp_path):
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data", output_dir=tmp_path / "out"))
    cfg.clip.frames_per_clip = 1
    conn = db.open_db(cfg.paths.db_path)
    yield cfg, conn
    conn.close()


def _video(conn, vid="abcdefghijk"):
    return store.upsert_video(conn, vid)


# ── job queue ────────────────────────────────────────────────────────────────

def test_queue_lifecycle(env):
    _, conn = env
    a, b = _video(conn, "aaaaaaaaaaa"), _video(conn, "bbbbbbbbbbb")
    ja = jobs.enqueue(conn, a, "ingest")
    jobs.enqueue(conn, b, "ingest", run_after=jobs.now() + timedelta(hours=1))  # not due yet
    job = jobs.claim(conn)
    assert job["id"] == ja and job["status"] == "running" and job["attempts"] == 1
    assert jobs.claim(conn) is None
    assert jobs.enqueue(conn, a, "ingest") is None            # running jobs are left alone

    jobs.finish(conn, ja, ok=False, message="later", retry_in=timedelta(minutes=30))
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (ja,)).fetchone()
    assert row["status"] == "pending" and row["run_after"] > jobs.sql_time(jobs.now())
    assert jobs.claim(conn) is None

    assert jobs.retry_video(conn, a) == 1
    job = jobs.claim(conn)
    assert job["id"] == ja and job["attempts"] == 1           # retry resets attempts
    jobs.finish(conn, ja, ok=False, message="boom")
    assert conn.execute("SELECT status, error FROM jobs WHERE id = ?", (ja,)).fetchone()[:] == ("error", "boom")
    jobs.enqueue(conn, a, "score")
    jobs.finish(conn, jobs.claim(conn)["id"], ok=True, message="ok")
    assert [r["stage"] for r in jobs.list_jobs(conn)] == ["ingest", "ingest"]
    assert len(jobs.list_jobs(conn, include_done=True)) == 3


def test_reset_running_and_next_stage(env):
    _, conn = env
    jobs.enqueue(conn, _video(conn), "clip")
    jobs.claim(conn)
    assert jobs.reset_running(conn) == 1 and jobs.claim(conn) is not None
    assert jobs.next_stage("ingest") == "score" and jobs.next_stage("analyze") == "upload"
    assert jobs.next_stage("upload") is None
    with pytest.raises(ValueError):
        jobs.enqueue(conn, 1, "publish")
    with pytest.raises(ValueError):
        jobs.enqueue(conn, 1, "upload", moment_pk=1)   # uploads are per stream


# ── retry rules ──────────────────────────────────────────────────────────────

def test_usage_reset_parsing():
    soon = int(jobs.now().timestamp()) + 3600
    d = usage_reset_delay(f"claude: Claude AI usage limit reached|{soon}")
    assert timedelta(minutes=59) < d < timedelta(minutes=65)
    assert usage_reset_delay("no time here") == timedelta(hours=1)
    assert timedelta(0) < until_utc_midnight() <= timedelta(days=1, minutes=5)


@pytest.mark.parametrize("kind,retryable,attempt,status,wait", [
    ("not_finished", True, 1, "retry", timedelta(minutes=30)),
    ("processing", True, 95, "retry", timedelta(minutes=30)),
    ("processing", True, 96, "error", None),
    ("bot_check", True, 1, "retry", timedelta(hours=2)),
    ("network", True, 2, "retry", timedelta(minutes=10)),
    ("members_only", False, 1, "error", None),
])
def test_ingest_retry_rules(env, monkeypatch, kind, retryable, attempt, status, wait):
    cfg, conn = env

    def boom(*a, **k):
        raise IngestError("nope", kind=kind, retryable=retryable)
    monkeypatch.setattr("chatwatcher.ingest.service.ingest_video", boom)
    out = run_stage(cfg, conn, "abcdefghijk", "ingest", attempt=attempt)
    assert out.status == status and out.retry_in == wait


def test_analyze_retry_rules(env, monkeypatch):
    cfg, conn = env
    soon = int(jobs.now().timestamp()) + 7200

    def deferred(*a, **k):
        r = AnalyzeRun("abcdefghijk")
        r.outcomes = [AnalyzeOutcome(1, "done", title="t"),
                      AnalyzeOutcome(2, "deferred", f"Claude usage limit reached — run again later (x|{soon})")]
        return r
    monkeypatch.setattr("chatwatcher.analyze.service.analyze_video", deferred)
    out = run_stage(cfg, conn, "abcdefghijk", "analyze")
    assert out.status == "retry" and timedelta(hours=1, minutes=59) < out.retry_in < timedelta(hours=2, minutes=5)

    cfg.claude.enabled = False
    assert run_stage(cfg, conn, "abcdefghijk", "analyze").status == "skip"
    assert not stage_enabled(cfg, "analyze") and stage_enabled(cfg, "clip")


# ── channel watcher ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,url", [
    ("@usadapekora", "https://www.youtube.com/@usadapekora/streams"),
    (UC, f"https://www.youtube.com/channel/{UC}/streams"),
    ("https://www.youtube.com/@AZKi/videos", "https://www.youtube.com/@AZKi/streams"),
    (f"https://youtube.com/channel/{UC}?si=x", f"https://www.youtube.com/channel/{UC}/streams"),
])
def test_streams_url(text, url):
    assert streams_url(text) == url


def test_streams_url_rejects():
    with pytest.raises(DiscoverError):
        streams_url("pekora")


def entry(vid, status="was_live", duration=7200, title=None):
    return {"id": vid, "live_status": status, "duration": duration, "title": title or f"stream {vid}"}


class FakeYouTube:
    """Channel listing + ingest + section download, all fake but realistic."""

    def __init__(self, tmp: Path, entries: list[dict], *, live_status="was_live"):
        self.entries = entries
        self.tmp = tmp
        self.live_status = live_status
        self.sample = make_video(tmp / "sample.mp4", 20)
        self.listings = 0

    # discover
    def channel_streams(self, url, n=15):
        self.listings += 1
        return {"id": UC, "channel_id": UC, "channel": "Pekora Ch.", "uploader_id": "@usadapekora",
                "entries": self.entries[:n]}

    # ingest
    def info(self, video_id):
        return {"id": video_id, "title": f"【雑談】stream {video_id}", "channel": "Pekora Ch.",
                "channel_id": UC, "uploader_id": "@usadapekora", "duration": 3600,
                "live_status": self.live_status, "release_timestamp": 1700000000,
                "subtitles": {"live_chat": [{}]}, "automatic_captions": {}, "formats": []}

    def download_chat(self, video_id, dest, on_line=None):
        dest.mkdir(parents=True, exist_ok=True)
        msgs = chat(background(3600), burst(900, "草", n=60), burst(2400, "かわいい", n=50, prefix="c"))
        p = dest / f"{video_id}.live_chat.json"
        with p.open("w", encoding="utf-8") as f:
            for m in msgs:
                item = {"liveChatTextMessageRenderer": {
                    "id": m.id, "message": {"runs": [{"text": m.text}]},
                    "authorName": {"simpleText": m.author}, "authorExternalChannelId": m.author_id,
                    "timestampUsec": "1"}}
                f.write(json.dumps({"replayChatItemAction": {
                    "actions": [{"addChatItemAction": {"item": item}}],
                    "videoOffsetTimeMsec": str(int(m.t * 1000))}}, ensure_ascii=False) + "\n")
        return p

    def download_captions(self, *a, **k):
        return None

    def version(self):
        return "fake"

    # clip
    def run(self, args, *, timeout, on_line=None):
        dest = Path(args[args.index("-P") + 1])
        shutil.copy(self.sample, dest / "clip.mp4")
        return ""


def test_add_channel_backfill_and_later_checks(env, tmp_path):
    cfg, conn = env
    yt = FakeYouTube(tmp_path, [entry("upcoming001", "is_upcoming", None), entry("liveliveliv", "is_live", None),
                                entry("newest00001"), entry("shortstream", duration=120), entry("older000001"),
                                entry("older000002")])
    row, res = add_channel(cfg, conn, "@usadapekora", stream_type="gaming", backfill=1, ytdlp=yt)
    assert row["enabled"] == 1 and row["default_type"] == "gaming" and row["last_checked_at"]
    assert res.queued == ["newest00001"] and res.waiting == 2
    assert dict(res.skipped) == {"shortstream": "shorter than 10 min",
                                 "older000001": "older than when the channel was added",
                                 "older000002": "older than when the channel was added"}
    v = store.get_video(conn, "newest00001")
    assert v["stream_type"] == "gaming" and v["channel_id"] == row["id"]
    assert [j["stage"] for j in jobs.list_jobs(conn)] == ["ingest"]

    # Later: the upcoming stream finished. Old skipped ones stay skipped.
    yt.entries[0] = entry("upcoming001")
    res2 = check_channel(cfg, conn, find_channel(conn, "@usadapekora"), ytdlp=yt)
    assert res2.queued == ["upcoming001"] and res2.waiting == 1 and not res2.skipped
    assert check_channel(cfg, conn, row, ytdlp=yt).queued == []

    assert due_channels(conn, 30) == []
    conn.execute("UPDATE channels SET last_checked_at = datetime('now', '-31 minutes')")
    assert len(due_channels(conn, 30)) == 1


def test_check_failure_is_recorded(env, tmp_path):
    cfg, conn = env
    from chatwatcher.ingest.ytdlp import YtDlpError

    class Broken(FakeYouTube):
        def channel_streams(self, url, n=15):
            raise YtDlpError("ERROR: HTTP Error 429", kind="rate_limited")
    yt = FakeYouTube(tmp_path, [])
    row, _ = add_channel(cfg, conn, UC, ytdlp=yt)
    res = check_channel(cfg, conn, row, ytdlp=Broken(tmp_path, []))
    assert res.error and "429" in res.error


# ── worker ───────────────────────────────────────────────────────────────────

def test_worker_runs_whole_pipeline(env, tmp_path):
    cfg, conn = env
    cfg.pipeline.analyze_top = 2
    yt = FakeYouTube(tmp_path, [entry("streamone01")])
    add_channel(cfg, conn, "@usadapekora", backfill=1, ytdlp=yt)
    fr = FakeRunner()
    n = Worker(cfg, deps=Deps(ytdlp=yt, claude=fr)).run(once=True)
    assert n == 4                                            # ingest, score, clip, analyze
    v = store.get_video(conn, "streamone01")
    assert v["status"] == "done" and v["error"] is None
    assert [r["stage"] for r in jobs.list_jobs(conn, include_done=True)] == ["ingest", "score", "clip", "analyze"]
    assert all(r["status"] == "done" for r in jobs.list_jobs(conn, include_done=True))
    folders = [Path(r["folder"]) for r in conn.execute("SELECT folder FROM clips")]
    assert folders and all((f / "clip.mp4").is_file() for f in folders)
    assert sum((f / "storyboard.md").is_file() for f in folders) == 2   # analyze_top

    # Nothing left to do; a second pass is a no-op.
    assert Worker(cfg, deps=Deps(ytdlp=yt, claude=fr)).run(once=True) == 0


def test_worker_waits_for_unfinished_archive(env, tmp_path):
    cfg, conn = env
    yt = FakeYouTube(tmp_path, [entry("streamone01")], live_status="post_live")
    add_channel(cfg, conn, UC, ytdlp=yt)
    assert Worker(cfg, deps=Deps(ytdlp=yt)).run(once=True) == 1
    job = jobs.list_jobs(conn)[0]
    assert job["status"] == "pending" and job["stage"] == "ingest" and "processing" in job["error"]
    v = store.get_video(conn, "streamone01")
    assert v["status"] == "processing" and "waiting" in v["error"]


def test_worker_survives_crash_and_skips_disabled_stages(env, tmp_path, monkeypatch):
    cfg, conn = env
    cfg.pipeline.auto_clip = False
    yt = FakeYouTube(tmp_path, [entry("streamone01")])
    add_channel(cfg, conn, UC, ytdlp=yt)
    calls = []

    def flaky(cfg_, conn_, vid, stage, **kw):
        calls.append(stage)
        if stage == "score":
            raise RuntimeError("bug")
        from chatwatcher.pipeline import StageOutcome
        return StageOutcome("done", "ok")
    monkeypatch.setattr("chatwatcher.worker.run_stage", flaky)
    Worker(cfg, deps=Deps(ytdlp=yt)).run(once=True)
    assert calls == ["ingest", "score"]
    assert store.get_video(conn, "streamone01")["status"] == "error"

    # With score fixed, clip is skipped (auto_clip off) and analyze is queued next.
    monkeypatch.setattr("chatwatcher.worker.run_stage",
                        lambda *a, **k: __import__("chatwatcher.pipeline", fromlist=["x"]).StageOutcome("done", "ok"))
    jobs.retry_video(conn, store.get_video(conn, "streamone01")["id"])
    Worker(cfg, deps=Deps(ytdlp=yt)).run(once=True)
    assert [r["stage"] for r in jobs.list_jobs(conn, include_done=True)] == ["ingest", "score", "analyze"]


def test_single_worker_lock(env):
    cfg, _ = env
    a, b = Worker(cfg), Worker(cfg)
    a.acquire_lock()
    with pytest.raises(WorkerLocked):
        b.acquire_lock()
    with pytest.raises(WorkerLocked):
        run_now(cfg, None, 1)
    a.release_lock()
    b.acquire_lock()
    b.release_lock()


def test_run_now_foreground(env, tmp_path):
    cfg, conn = env
    yt = FakeYouTube(tmp_path, [])
    pk = store.upsert_video(conn, "manualvid01")
    assert run_now(cfg, conn, pk, deps=Deps(ytdlp=yt, claude=FakeRunner())) == "done"
    assert store.get_video(conn, "manualvid01")["status"] == "done"
    yt.live_status = "is_live"
    pk2 = store.upsert_video(conn, "manualvid02")
    assert run_now(cfg, conn, pk2, deps=Deps(ytdlp=yt)) == "waiting"


# ── CLI (fake yt-dlp executable for the channel listing) ─────────────────────

def test_cli_channels_and_queue(tmp_path):
    listing = json.dumps({"id": UC, "channel_id": UC, "channel": "Pekora Ch.", "uploader_id": "@usadapekora",
                          "entries": [entry("newest00001"), entry("older000001")]})
    fake = tmp_path / "yt-dlp"
    fake.write_text(f"#!{sys.executable}\nprint({listing!r})\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    f = tmp_path / "config.toml"
    f.write_text(f'[paths]\ndata_dir = "data"\noutput_dir = "out"\n[youtube]\nytdlp_path = "{fake}"\n')
    cli = CliRunner()

    r = cli.invoke(app, ["channel", "add", "@usadapekora", "-c", str(f), "--type", "karaoke"])
    assert r.exit_code == 0, r.output
    assert "Watching Pekora Ch." in r.output and "queued 1" in r.output
    r = cli.invoke(app, ["channel", "list", "-c", str(f)])
    assert "karaoke" in r.output and "@usadapekora" in r.output
    r = cli.invoke(app, ["jobs", "-c", str(f)])
    assert "newest00001 ingest" in r.output
    r = cli.invoke(app, ["add", "https://youtu.be/manualvid01", "-c", str(f)])
    assert "Queued manualvid01" in r.output
    r = cli.invoke(app, ["retry", "manualvid01", "-c", str(f)])
    assert r.exit_code == 0 and "queued to run now" in r.output
    r = cli.invoke(app, ["channel", "disable", "@usadapekora", "-c", str(f)])
    assert "disabled" in r.output
    assert "No channels" in cli.invoke(app, ["channel", "list", "-c", str(f)]).output
    r = cli.invoke(app, ["channel", "remove", "usadapekora", "-c", str(f)])
    assert "Removed" in r.output
    r = cli.invoke(app, ["channel", "enable", "nobody", "-c", str(f)])
    assert r.exit_code == 1


def test_cli_accepts_video_ids_starting_with_dash(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('[paths]\ndata_dir = "data"\noutput_dir = "out"\n')
    r = CliRunner().invoke(app, ["add", "-k8EZv9wkAs", "-c", str(f)])
    assert r.exit_code == 0, r.output
    assert "Queued -k8EZv9wkAs" in r.output
    r = CliRunner().invoke(app, ["retry", "-k8EZv9wkAs", "-c", str(f)])
    assert r.exit_code == 0 and "-k8EZv9wkAs" in r.output


def test_members_only_skipped_without_cookies(env, tmp_path):
    cfg, conn = env
    yt = FakeYouTube(tmp_path, [entry("memberonly1", title="【メン限】月イチ雑談"), entry("normalone01")])
    _, res = add_channel(cfg, conn, UC, backfill=5, ytdlp=yt)
    assert res.queued == ["normalone01"]
    assert res.skipped[0][0] == "memberonly1" and "members-only" in res.skipped[0][1]
