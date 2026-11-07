import json
import stat
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chatwatcher import db, jobs, store
from chatwatcher.cli import app
from chatwatcher.config import Config, DriveConfig, PathsConfig
from chatwatcher.pipeline import Deps, run_stage
from chatwatcher.upload.drive import Rclone, RcloneError, UploadError, classify, dest_path_for, upload_video
from chatwatcher.worker import Worker

from test_analyze import FakeRunner
from test_automation import FakeYouTube, entry
from test_score_service import VID

FAKE_RCLONE = r'''
import json, os, shutil, sys
args = sys.argv[1:]
root = os.environ["FAKE_DRIVE"]
fail = os.environ.get("FAKE_RCLONE_FAIL", "")
log = open(os.path.join(root, "_calls.log"), "a")
log.write(json.dumps(args) + "\n")
if fail == "auth":
    print("ERROR : couldn't fetch token: invalid_grant: maybe token expired?", file=sys.stderr); sys.exit(1)
if fail == "network":
    print("ERROR : dial tcp: lookup www.googleapis.com: no such host", file=sys.stderr); sys.exit(1)
if args[0] == "listremotes":
    print("" if fail == "noremote" else "gdrive:\nother:"); sys.exit(0)
if args[0] == "lsjson":
    # Like real Drive: --stat on a folder has no ID; a listing of its parent does.
    if "--stat" in args:
        print(json.dumps({"Path": "", "Name": "", "IsDir": True}))
    else:
        parent = os.path.join(root, args[-1].split(":", 1)[1])
        names = sorted(os.listdir(parent)) if os.path.isdir(parent) else []
        print(json.dumps([{"Name": n, "IsDir": True, "ID": "FOLDER123"} for n in names
                          if os.path.isdir(os.path.join(parent, n))]))
    sys.exit(0)
if args[0] == "copy":
    src, dest = args[1], args[2].split(":", 1)[1]
    excludes = [args[i + 1] for i, a in enumerate(args) if a == "--exclude"]
    target = os.path.join(root, dest)
    def skip(rel):
        import fnmatch
        return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(os.path.basename(rel), p) or
                   rel.split("/")[0].startswith(".dl_") for p in excludes)
    n = 0
    for d, _, files in os.walk(src):
        for f in files:
            rel = os.path.relpath(os.path.join(d, f), src)
            if skip(rel):
                continue
            os.makedirs(os.path.dirname(os.path.join(target, rel)), exist_ok=True)
            shutil.copy(os.path.join(d, f), os.path.join(target, rel)); n += 1
    print(f"Transferred: {n} / {n}, 100%, 1 MiB/s, ETA 0s", file=sys.stderr)
    sys.exit(0)
print("unknown command", file=sys.stderr); sys.exit(2)
'''


@pytest.fixture
def drive(tmp_path, monkeypatch):
    exe = tmp_path / "rclone"
    exe.write_text(f"#!{sys.executable}\n{FAKE_RCLONE}")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    remote = tmp_path / "drive"
    remote.mkdir()
    monkeypatch.setenv("FAKE_DRIVE", str(remote))
    monkeypatch.delenv("FAKE_RCLONE_FAIL", raising=False)
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data", output_dir=tmp_path / "out"),
                 drive=DriveConfig(enabled=True, rclone_path=str(exe)))
    cfg.clip.frames_per_clip = 1
    conn = db.open_db(cfg.paths.db_path)
    yield cfg, conn, remote
    conn.close()


def make_output(cfg, conn, vid=VID):
    out = cfg.paths.output_dir / "Pekora_Ch" / f"2025-03-22_{vid}_title"
    (out / "01_laugh_1h00m50s" / "frames").mkdir(parents=True)
    (out / "01_laugh_1h00m50s" / "clip.mp4").write_bytes(b"x" * 100)
    (out / "01_laugh_1h00m50s" / "frames" / "01.jpg").write_bytes(b"j")
    (out / "stream_digest.md").write_text("# digest")
    (out / ".dl_clip").mkdir()
    (out / ".dl_clip" / "clip.mkv.part").write_bytes(b"partial")
    store.upsert_video(conn, vid, output_dir=str(out))
    return out


@pytest.mark.parametrize("text,kind", [
    ("Failed to create file system: didn't find section in config file", "no_remote"),
    ("couldn't fetch token: invalid_grant", "auth"),
    ("googleapi: Error 403: The user's Drive storage quota has been exceeded., storageQuotaExceeded", "quota"),
    ("googleapi: Error 403: User rate limit exceeded., userRateLimitExceeded", "rate_limited"),
    ("dial tcp: lookup www.googleapis.com: no such host", "network"),
    ("something else", "error"),
])
def test_classify(text, kind):
    assert classify(text, "gdrive")[0] == kind


def test_missing_rclone(tmp_path):
    with pytest.raises(RcloneError) as e:
        Rclone(DriveConfig(rclone_path=str(tmp_path / "nope")))
    assert e.value.kind == "missing"


def test_check_remote(drive, monkeypatch):
    cfg, _, _ = drive
    rc = Rclone(cfg.drive)
    assert rc.remotes() == ["gdrive", "other"]
    rc.check()
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "noremote")
    with pytest.raises(RcloneError) as e:
        rc.check()
    assert e.value.kind == "no_remote" and "rclone config" in str(e.value)


def test_upload_mirrors_tree_and_skips_temp_files(drive):
    cfg, conn, remote = drive
    out = make_output(cfg, conn)
    assert dest_path_for(cfg, out) == f"chat-watcher/Pekora_Ch/2025-03-22_{VID}_title"
    r = upload_video(cfg, conn, VID)
    up = remote / "chat-watcher" / "Pekora_Ch" / f"2025-03-22_{VID}_title"
    assert (up / "01_laugh_1h00m50s" / "clip.mp4").read_bytes() == b"x" * 100
    assert (up / "01_laugh_1h00m50s" / "frames" / "01.jpg").is_file()
    assert (up / "stream_digest.md").is_file()
    assert not (up / ".dl_clip").exists()
    assert r.url == "https://drive.google.com/drive/folders/FOLDER123"
    assert r.dest.startswith("gdrive:chat-watcher/") and "Transferred" in r.stats
    assert store.get_video(conn, VID)["drive_url"] == r.url
    calls = [json.loads(line) for line in (remote / "_calls.log").read_text().splitlines()]
    assert calls[0][0] == "copy" and "sync" not in [c[0] for c in calls]   # never deletes on Drive


def test_upload_errors(drive, monkeypatch):
    cfg, conn, _ = drive
    store.upsert_video(conn, VID)
    with pytest.raises(UploadError) as e:
        upload_video(cfg, conn, VID)
    assert e.value.kind == "nothing"
    make_output(cfg, conn)
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "auth")
    with pytest.raises(UploadError) as e:
        upload_video(cfg, conn, VID)
    assert e.value.kind == "auth" and "rclone config reconnect gdrive:" in str(e.value)


def test_upload_stage_retry_rules(drive, monkeypatch):
    cfg, conn, _ = drive
    make_output(cfg, conn)
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "auth")
    out = run_stage(cfg, conn, VID, "upload")
    assert out.status == "retry" and out.retry_in == timedelta(hours=1)
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "network")
    out = run_stage(cfg, conn, VID, "upload", attempt=2)
    assert out.status == "retry" and out.retry_in == timedelta(minutes=20)
    monkeypatch.delenv("FAKE_RCLONE_FAIL")
    assert run_stage(cfg, conn, VID, "upload").status == "done"
    cfg.drive.enabled = False
    assert run_stage(cfg, conn, VID, "upload").status == "skip"


def test_worker_uploads_after_analysis(drive, tmp_path):
    cfg, conn, remote = drive
    from chatwatcher.discover import add_channel
    yt = FakeYouTube(tmp_path, [entry("streamone01")])
    add_channel(cfg, conn, "@usadapekora", backfill=1, ytdlp=yt)
    cfg.pipeline.analyze_top = 1
    n = Worker(cfg, deps=Deps(ytdlp=yt, claude=FakeRunner())).run(once=True)
    assert n == 5
    stages = [r["stage"] for r in jobs.list_jobs(conn, include_done=True)]
    assert stages == ["ingest", "score", "clip", "analyze", "upload"]
    v = store.get_video(conn, "streamone01")
    assert v["status"] == "done" and v["drive_url"].endswith("FOLDER123")
    assert list(remote.glob("chat-watcher/*/*/*/storyboard.md"))

    # Re-analysing one moment refreshes the Drive copy afterwards.
    m = conn.execute("SELECT id FROM moments WHERE rank = 1").fetchone()["id"]
    jobs.enqueue(conn, v["id"], "analyze", moment_pk=m)
    Worker(cfg, deps=Deps(ytdlp=yt, claude=FakeRunner())).run(once=True)
    upload_jobs = conn.execute("SELECT status FROM jobs WHERE stage = 'upload'").fetchall()
    assert [r["status"] for r in upload_jobs] == ["done"]
    assert conn.execute("SELECT attempts FROM jobs WHERE stage = 'upload'").fetchone()[0] == 1


def test_auto_upload_off_stops_before_upload(drive, tmp_path):
    cfg, conn, _ = drive
    cfg.drive.auto_upload = False
    from chatwatcher.discover import add_channel
    yt = FakeYouTube(tmp_path, [entry("streamone01")])
    add_channel(cfg, conn, "@usadapekora", backfill=1, ytdlp=yt)
    assert Worker(cfg, deps=Deps(ytdlp=yt, claude=FakeRunner())).run(once=True) == 4
    assert store.get_video(conn, "streamone01")["drive_url"] is None


def test_cli_upload(drive, tmp_path):
    cfg, conn, remote = drive
    make_output(cfg, conn)
    f = tmp_path / "config.toml"
    f.write_text(f'[paths]\ndata_dir = "{cfg.paths.data_dir}"\noutput_dir = "{cfg.paths.output_dir}"\n'
                 f'[drive]\nenabled = true\nrclone_path = "{cfg.drive.rclone_path}"\n')
    r = CliRunner().invoke(app, ["upload", VID, "-c", str(f)])
    assert r.exit_code == 0, r.output
    assert "gdrive:chat-watcher/Pekora_Ch" in r.output and "FOLDER123" in r.output
    r = CliRunner().invoke(app, ["upload", "zzzzzzzzzzz", "-c", str(f)])
    assert r.exit_code == 1
