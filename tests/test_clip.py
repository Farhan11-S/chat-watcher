import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from chatwatcher import db, store
from chatwatcher.clip.download import ClipFile, _attempts, download_clip
from chatwatcher.clip.media import probe, remux
from chatwatcher.clip.package import (
    extract_frames, moment_dirname, slug, video_dir, write_chat_excerpt, write_transcript_excerpt,
)
from chatwatcher.clip.service import ClipError, clip_video
from chatwatcher.config import ClipConfig, Config, PathsConfig
from chatwatcher.ingest.captions import Segment
from chatwatcher.ingest.chat_parser import ChatMessage
from chatwatcher.ingest.ytdlp import YtDlpError
from chatwatcher.scoring.service import score_video

from test_score_service import VID, ingest_fake

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def make_video(path: Path, duration: float = 20, video_offset: float = 0.0) -> Path:
    """Tiny real H.264/AAC file. video_offset > 0 → audio-only lead-in, like real section cuts."""
    cmd = ["ffmpeg", "-v", "error", "-y"]
    if video_offset:
        cmd += ["-itsoffset", str(video_offset)]
    cmd += ["-f", "lavfi", "-i", f"testsrc=duration={duration}:size=320x240:rate=30",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration + video_offset}",
            "-c:v", "libx264", "-g", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest" if not video_offset else "-y",
            str(path)]
    subprocess.run([c for c in cmd if c], check=True)
    return path


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    return {
        "clean": make_video(d / "clean.mp4", 20),
        "leadin": make_video(d / "leadin.mkv", 20, video_offset=3.0),
    }


# ── media ────────────────────────────────────────────────────────────────────

def test_probe(samples):
    p = probe(samples["clean"])
    assert (p.width, p.height, p.vcodec, p.acodec) == (320, 240, "h264", "aac")
    assert p.fps == 30 and 19.5 < p.duration_s < 20.5 and p.first_video_pts < 0.1
    lead = probe(samples["leadin"])
    assert 2.9 < lead.first_video_pts < 3.1 and lead.duration_s > 22.5


def test_remux_trims_lead_in_losslessly(samples, tmp_path):
    out = remux(samples["leadin"], tmp_path / "clip", trim_start=probe(samples["leadin"]).first_video_pts)
    assert out.suffix == ".mp4"
    p = probe(out)
    assert p.first_video_pts < 0.1 and 19.5 < p.duration_s < 20.5 and p.vcodec == "h264"


# ── download (fake yt-dlp writes a real file) ────────────────────────────────

class FakeYt:
    """Mimics YtDlp.run for section downloads: drops a prepared file into -P."""

    def __init__(self, source: Path, fail_first: int = 0, fail_kind: str = "error"):
        self.source = source
        self.fail_first = fail_first
        self.fail_kind = fail_kind
        self.calls: list[list[str]] = []

    def run(self, args, *, timeout, on_line=None):
        self.calls.append(args)
        if len(self.calls) <= self.fail_first:
            raise YtDlpError("ERROR: ffmpeg exited with code 234", kind=self.fail_kind)
        dest = Path(args[args.index("-P") + 1])
        if on_line:
            on_line("CWFMT 312+140")
            on_line("[download] 50%")
        shutil.copy(self.source, dest / f"clip{self.source.suffix}")
        return ""

    def info(self, video_id):
        return {"id": video_id, "formats": []}


def test_download_measures_start_and_trims_lead_in(samples, tmp_path):
    yt = FakeYt(samples["leadin"])
    c = download_clip(yt, tmp_path / "info.json", 100.0, 123.0, tmp_path / "out" / "clip", ClipConfig())
    assert c.path.suffix == ".mp4" and c.path.is_file()
    assert c.format_id == "312+140"
    # raw file is ~23s ending at 123 → starts at ~100; the 3s audio-only lead-in is cut off
    assert c.zero_s == pytest.approx(103.0, abs=0.15)
    assert c.to_clip_time(110.0) == pytest.approx(7.0, abs=0.15)
    assert not list((tmp_path / "out").glob(".dl_*"))
    args = yt.calls[0]
    assert args[args.index("--download-sections") + 1] == "*100.00-123.00"
    assert args[args.index("-f") + 1] == ClipConfig().format


def test_download_falls_back_to_other_formats(samples, tmp_path):
    yt = FakeYt(samples["clean"], fail_first=1)
    c = download_clip(yt, tmp_path / "i.json", 0, 20, tmp_path / "clip", ClipConfig())
    assert len(yt.calls) == 2 and c.path.is_file()
    assert yt.calls[1][yt.calls[1].index("-S") + 1] == "res,fps,proto,br"


def test_download_does_not_retry_bot_check(samples, tmp_path):
    yt = FakeYt(samples["clean"], fail_first=5, fail_kind="bot_check")
    with pytest.raises(YtDlpError):
        download_clip(yt, tmp_path / "i.json", 0, 20, tmp_path / "clip", ClipConfig())
    assert len(yt.calls) == 1


def test_download_gives_up_after_all_attempts(samples, tmp_path):
    yt = FakeYt(samples["clean"], fail_first=99)
    with pytest.raises(YtDlpError, match="All download attempts failed"):
        download_clip(yt, tmp_path / "i.json", 0, 20, tmp_path / "clip", ClipConfig())
    assert len(yt.calls) == len(_attempts(ClipConfig()))


# ── packaging ────────────────────────────────────────────────────────────────

def test_slug_and_dirs(tmp_path):
    assert slug('【歌枠】アニソン縛り！ / "live"?') == "【歌枠】アニソン縛り!_live"
    assert slug("   ") == "untitled"
    d = video_dir(tmp_path, {"channel": "AZKi Channel", "title": "【歌枠】テスト",
                             "release_timestamp": 1700000000}, VID)
    assert d == tmp_path / "AZKi_Channel" / f"2023-11-14_{VID}_【歌枠】テスト"
    assert moment_dirname(1, "song", [], 2711) == "01_song_45m11s"
    assert moment_dirname(12, "reaction", [{"signal": "laugh"}], 3650) == "12_laugh_1h00m50s"


def _clip(path, zero=100.0, dur=20.0):
    return ClipFile(path=path, zero_s=zero, duration_s=dur, width=320, height=240, fps=30,
                    vcodec="h264", acodec="aac", size_bytes=1)


def test_excerpts_use_clip_time(tmp_path):
    msgs = [ChatMessage(t=99, id="a", author="early", author_id="1", text="no"),
            ChatMessage(t=105.5, id="b", author="x", author_id="2", text="草"),
            ChatMessage(t=110, id="c", author="s", author_id="3", text="", kind="superchat", amount_text="¥500"),
            ChatMessage(t=111, id="d", author="me", author_id="4", text="hi", is_owner=True)]
    n = write_chat_excerpt(tmp_path / "chat.txt", msgs, _clip(tmp_path / "c.mp4"))
    lines = (tmp_path / "chat.txt").read_text().splitlines()
    assert n == 3 and lines[0] == "[00:05.5] x: 草"
    assert "[SC ¥500]" in lines[1] and "(streamer)" in lines[2]

    segs = [Segment(90, 95, "前。"), Segment(98, 102, "それで、"), Segment(102, 104, "こうなった。")]
    write_transcript_excerpt(tmp_path / "t.txt", segs, _clip(tmp_path / "c.mp4"))
    assert (tmp_path / "t.txt").read_text().splitlines() == ["[00:00.0] それで、こうなった。"]


def test_extract_frames(samples, tmp_path):
    c = _clip(samples["clean"], zero=1000.0)
    frames = extract_frames(c, tmp_path / "frames", 4, peak_s=1010.0)
    assert len(frames) == 5 and sum(f["is_peak"] for f in frames) == 1
    assert all((tmp_path / f["file"]).is_file() for f in frames)
    assert [f["clip_s"] for f in frames] == sorted(f["clip_s"] for f in frames)
    assert frames[0]["stream_s"] == pytest.approx(1000 + frames[0]["clip_s"])
    assert re.match(r"\d\d_\d\dm\d\ds\.jpg", Path(frames[0]["file"]).name)


# ── service ──────────────────────────────────────────────────────────────────

@pytest.fixture
def scored(tmp_path):
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data", output_dir=tmp_path / "out"))
    cfg.clip.frames_per_clip = 2
    conn = db.open_db(cfg.paths.db_path)
    ingest_fake(cfg, conn)
    score_video(cfg, conn, VID)
    yield cfg, conn
    conn.close()


def test_clip_video_end_to_end(scored, samples):
    cfg, conn = scored
    n_moments = conn.execute("SELECT COUNT(*) FROM moments").fetchone()[0]
    events = []
    run = clip_video(cfg, conn, VID, top=2, ytdlp=FakeYt(samples["clean"]),
                     on_event=lambda r, m: events.append((r, m)))
    assert n_moments >= 2 and run.count("done") == 2
    rows = conn.execute("SELECT * FROM clips").fetchall()
    assert len(rows) == 2
    for o in run.outcomes:
        assert (o.folder / "clip.mp4").is_file() and (o.folder / "moment.json").is_file()
        assert (o.folder / "chat_excerpt.txt").is_file() and (o.folder / "transcript.txt").is_file()
        assert len(list((o.folder / "frames").glob("*.jpg"))) >= 2
        meta = json.loads((o.folder / "moment.json").read_text())
        assert meta["clip"]["vcodec"] == "h264" and meta["youtube_url"].startswith("https://youtu.be/")
    assert rows[0]["zero_s"] is not None and json.loads(rows[0]["frames_json"])
    assert store.get_video(conn, VID)["output_dir"] == str(run.output_dir)
    assert any(m == "done" for _, m in events)
    assert not (cfg.paths.data_dir / "vods" / VID / "_dl").exists()  # signed URLs removed

    again = clip_video(cfg, conn, VID, top=2, ytdlp=FakeYt(samples["clean"]))
    assert again.count("skipped") == 2
    forced = clip_video(cfg, conn, VID, ranks=[1], force=True, ytdlp=FakeYt(samples["clean"]))
    assert [o.rank for o in forced.outcomes] == [1] and forced.count("done") == 1
    assert conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 2


def test_one_failure_does_not_stop_others(scored, samples):
    cfg, conn = scored
    cfg.clip.max_concurrent = 1
    yt = FakeYt(samples["clean"], fail_first=len(_attempts(cfg.clip)))  # first clip exhausts retries
    run = clip_video(cfg, conn, VID, top=2, ytdlp=yt)
    assert run.count("error") == 1 and run.count("done") == 1


def test_rejected_moments_are_skipped(scored, samples):
    cfg, conn = scored
    conn.execute("UPDATE moments SET status = 'rejected' WHERE rank = 1")
    run = clip_video(cfg, conn, VID, top=1, ytdlp=FakeYt(samples["clean"]))
    assert [o.rank for o in run.outcomes] == [2]


def test_clip_errors(tmp_path):
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data", output_dir=tmp_path / "out"))
    conn = db.open_db(cfg.paths.db_path)
    with pytest.raises(ClipError, match="ingest"):
        clip_video(cfg, conn, VID)
    ingest_fake(cfg, conn)
    with pytest.raises(ClipError, match="score"):
        clip_video(cfg, conn, VID)
    conn.close()
