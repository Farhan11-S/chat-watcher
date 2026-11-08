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
if args[0] == "config" and args[1:] == ["dump"]:
    tok = open(os.path.join(root, "_token")).read().strip() if os.path.exists(os.path.join(root, "_token")) else ""
    print(json.dumps({"gdrive": {"type": "drive", "token": json.dumps({"refresh_token": tok}) if tok else ""}}))
    sys.exit(0)
if args[0] == "listremotes":
    print("" if fail == "noremote" else "gdrive:\nother:"); sys.exit(0)
if args[0] == "about":
    print(json.dumps({"total": 15000000000, "used": 5000000000, "trashed": 1000, "other": 2000000000,
                      "free": 8000000000})); sys.exit(0)
if args[0] == "lsjson" and "-R" in args:
    base = os.path.join(root, args[-1].split(":", 1)[1])
    if not os.path.isdir(base):
        print("ERROR : error listing: directory not found", file=sys.stderr); sys.exit(3)
    out = []
    for d, _, files in os.walk(base):
        for f in files:
            p = os.path.join(d, f)
            out.append({"Path": os.path.relpath(p, base), "Name": f, "Size": os.path.getsize(p), "IsDir": False})
    print(json.dumps(out)); sys.exit(0)
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
    filters = [args[i + 1] for i, a in enumerate(args) if a == "--filter"]
    if filters and excludes:
        print("don't mix --exclude and --filter", file=sys.stderr); sys.exit(2)
    target = os.path.join(root, dest)
    def glob_re(pat):
        # rclone globs: leading / anchors at the root, ** crosses folders, * doesn't, \x escapes
        import re
        anchored = pat.startswith("/")
        out, i = "", 0
        pat = pat.lstrip("/")
        while i < len(pat):
            c = pat[i]
            if c == "\\":
                out += re.escape(pat[i + 1]); i += 2; continue
            if pat.startswith("**", i):
                out += ".*"; i += 2; continue
            out += "[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c); i += 1
        return re.compile(("^" if anchored else "(^|.*/)") + out + "$")
    def skip(rel):
        import fnmatch
        if filters:
            for rule in filters:
                sign, pat = rule[0], rule[2:]
                if glob_re(pat).match(rel):
                    return sign == "-"
            return False
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
    assert n == 6
    stages = [r["stage"] for r in jobs.list_jobs(conn, include_done=True)]
    assert stages == ["ingest", "score", "clip", "frames", "analyze", "upload"]
    v = store.get_video(conn, "streamone01")
    assert v["status"] == "done" and v["drive_url"].endswith("FOLDER123")
    assert list(remote.glob("chat-watcher/*/*/*/storyboard.md"))

    assert conn.execute("SELECT COUNT(*) FROM clips WHERE uploaded_at IS NULL").fetchone()[0] == 0

    # Re-analysing one moment refreshes just that clip on Drive afterwards.
    m = conn.execute("SELECT id FROM moments WHERE rank = 1").fetchone()["id"]
    jobs.enqueue(conn, v["id"], "analyze", moment_pk=m)
    Worker(cfg, deps=Deps(ytdlp=yt, claude=FakeRunner())).run(once=True)
    upload_jobs = conn.execute("SELECT moment_id, status, attempts FROM jobs WHERE stage = 'upload' "
                               "ORDER BY id").fetchall()
    assert [tuple(r) for r in upload_jobs] == [(None, "done", 1), (m, "done", 1)]


def test_auto_upload_off_stops_before_upload(drive, tmp_path):
    cfg, conn, _ = drive
    cfg.drive.auto_upload = False
    from chatwatcher.discover import add_channel
    yt = FakeYouTube(tmp_path, [entry("streamone01")])
    add_channel(cfg, conn, "@usadapekora", backfill=1, ytdlp=yt)
    assert Worker(cfg, deps=Deps(ytdlp=yt, claude=FakeRunner())).run(once=True) == 5
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


def test_drive_login_notice(drive):
    from datetime import datetime, timedelta, timezone
    from chatwatcher.upload.login import drive_login_notice
    cfg, conn, remote = drive
    t0 = datetime(2026, 11, 8, 4, 0, tzinfo=timezone.utc)
    rc = Rclone(cfg.drive)
    assert drive_login_notice(cfg, conn, rclone=rc, now=t0) is None             # no token yet
    (remote / "_token").write_text("refresh-A")
    assert drive_login_notice(cfg, conn, rclone=rc, now=t0) is None             # expiry off (0 days)
    cfg.drive.login_expires_days = 7
    assert drive_login_notice(cfg, conn, rclone=rc, now=t0 + timedelta(days=4)) is None
    n = drive_login_notice(cfg, conn, rclone=rc, now=t0 + timedelta(days=5, hours=12))
    assert n.level == "warn" and "36 hours" in n.text and "config reconnect gdrive:" in n.command
    n = drive_login_notice(cfg, conn, rclone=rc, now=t0 + timedelta(days=7, minutes=1))
    assert n.level == "err" and "expired on" in n.text

    # An upload that failed on sign-in shows the banner; a new sign-in requeues it and resets the clock.
    pk = store.upsert_video(conn, VID)
    jid = jobs.enqueue(conn, pk, "upload")
    jobs.finish(conn, jid, ok=False, message="Google Drive login expired. Run ...")
    t1 = t0 + timedelta(days=8)
    assert drive_login_notice(cfg, conn, rclone=rc, now=t1).level == "err"
    (remote / "_token").write_text("refresh-B")
    assert drive_login_notice(cfg, conn, rclone=rc, now=t1) is None
    j = conn.execute("SELECT status, attempts FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert j["status"] == "pending" and j["attempts"] == 0
    assert drive_login_notice(cfg, conn, rclone=rc, now=t1 + timedelta(days=6)).level == "warn"


def test_switch_off_then_upload_one_clip_or_kept_clips(drive, tmp_path):
    from chatwatcher import prefs
    from chatwatcher.discover import add_channel
    from chatwatcher.pipeline import stage_enabled
    from chatwatcher.upload.drive import clip_folders
    cfg, conn, remote = drive
    prefs.put(conn, prefs.AUTO_UPLOAD, False)            # dashboard switch beats config.toml
    assert cfg.drive.auto_upload and not stage_enabled(cfg, "upload", conn)
    yt = FakeYouTube(tmp_path, [entry("streamone01")])
    add_channel(cfg, conn, "@usadapekora", backfill=1, ytdlp=yt)
    cfg.pipeline.analyze_top = 1
    Worker(cfg, deps=Deps(ytdlp=yt, claude=FakeRunner())).run(once=True)
    v = store.get_video(conn, "streamone01")
    assert v["status"] == "done" and v["drive_url"] is None and not any(remote.iterdir())
    folders = clip_folders(conn, "streamone01")
    assert len(folders) >= 2

    # One clip: only its folder (and the stream-level files) go to Drive.
    m2 = conn.execute("SELECT id FROM moments WHERE rank = 2").fetchone()["id"]
    jobs.enqueue(conn, v["id"], "upload", moment_pk=m2)
    Worker(cfg, deps=Deps(ytdlp=yt, claude=FakeRunner())).run(once=True)
    stream_dir = next(remote.glob("chat-watcher/*/*"))
    assert sorted(p.name for p in stream_dir.iterdir() if p.is_dir()) == [folders[1]]
    assert (stream_dir / folders[1] / "moment.json").is_file()
    uploaded = conn.execute("SELECT m.rank FROM clips c JOIN moments m ON m.id = c.moment_id "
                            "WHERE c.uploaded_at IS NOT NULL").fetchall()
    assert [r["rank"] for r in uploaded] == [2]
    assert store.get_video(conn, "streamone01")["drive_url"].endswith("FOLDER123")

    # Kept clips only.
    assert run_stage(cfg, conn, "streamone01", "upload", params={"kept_only": True}).message.startswith("nothing")
    conn.execute("UPDATE moments SET status = 'approved' WHERE rank = 1")
    out = run_stage(cfg, conn, "streamone01", "upload", params={"kept_only": True})
    assert out.status == "done" and out.message.startswith("1 clip(s)")
    assert sorted(p.name for p in stream_dir.iterdir() if p.is_dir()) == sorted(folders[:2])

    # Re-analysing an uploaded clip refreshes just that clip, when auto-upload is on.
    prefs.put(conn, prefs.AUTO_UPLOAD, True)
    jobs.enqueue(conn, v["id"], "analyze", moment_pk=m2)
    Worker(cfg, deps=Deps(ytdlp=yt, claude=FakeRunner())).run(once=True)
    assert conn.execute("SELECT status FROM jobs WHERE stage = 'upload' AND moment_id = ?",
                        (m2,)).fetchone()["status"] == "done"
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE stage = 'upload' AND moment_id IS NULL").fetchone()[0] == 0


def test_glob_characters_in_folder_names(drive):
    from chatwatcher.upload.drive import _glob_escape
    cfg, conn, remote = drive
    out = make_output(cfg, conn)
    (out / "02_hype_[x]*").mkdir()
    (out / "02_hype_[x]*" / "clip.mp4").write_bytes(b"y")
    assert _glob_escape("02_hype_[x]*") == "02_hype_\\[x\\]\\*"
    upload_video(cfg, conn, VID, folders=["02_hype_[x]*"])
    up = remote / "chat-watcher" / "Pekora_Ch" / f"2025-03-22_{VID}_title"
    assert sorted(p.name for p in up.iterdir()) == ["02_hype_[x]*", "stream_digest.md"]


def test_drive_storage_usage(drive):
    from chatwatcher import storage
    cfg, conn, remote = drive
    assert storage.drive_usage(cfg).files == 0           # folder not created yet: not an error
    make_output(cfg, conn)
    upload_video(cfg, conn, VID)
    u = storage.drive_usage(cfg)
    assert u.error is None and (u.total, u.free) == (15_000_000_000, 8_000_000_000)
    assert u.ours == 100 + 1 + len("# digest") and u.files == 3
    assert u.by_path["Pekora_Ch"] == u.ours
    assert u.by_path[f"Pekora_Ch/2025-03-22_{VID}_title"] == u.ours
    assert u.your_other == 5_000_000_000 - u.ours
    local = storage.local_usage(cfg, conn)
    storage.merge(local, u)
    assert local.channels[0].drive == u.ours and local.streams[0].drive == u.ours
    assert local.output_bytes == u.ours + len(b"partial")   # local also counts the .part file


def test_fmt_bytes():
    from chatwatcher.storage import fmt_bytes
    assert [fmt_bytes(n) for n in (None, 999, 1284, 12_900_000, 4_200_000_000, 66e12)] == \
        ["–", "999 B", "1.3 KB", "12.9 MB", "4.2 GB", "66.0 TB"]
