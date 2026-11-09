import json
import shutil
from pathlib import Path

import pytest

from chatwatcher import jobs
from chatwatcher.config import Config
from chatwatcher.pipeline import Deps, run_stage
from chatwatcher.subs import transcribe as tr
from chatwatcher.subs.transcribe import Line, transcribe_clip
from chatwatcher.web.queries import stage_map
from chatwatcher.worker import Worker

from test_clip import make_video
from test_score_service import VID
from test_web import mid, site  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


class FakeWhisper:
    """Says the same few lines for every clip, without a model."""
    name = "fake-turbo"

    def __init__(self, lines=None):
        self.calls = 0
        self.lines = lines or [
            Line(0.5, 2.0, "Good thing I put a limiter in there.",
                 [[0.5, 0.8, " Good", 0.9], [0.8, 1.0, " thing", 0.95], [1.0, 2.0, " there.", 0.9]], "en", 0.01, -0.2),
            Line(3.0, 4.0, "Are you deaf?", [[3.0, 3.4, " Are", 0.9], [3.4, 4.0, " deaf?", 0.8]], "en", 0.02, -0.3),
        ]

    def run(self, audio, language):
        self.calls += 1
        assert len(audio) > 16000 and audio.dtype.name == "float32"      # real samples from ffmpeg
        return self.lines, "en", 0.99


def test_transcribe_clip_writes_words_and_keeps_youtube(tmp_path):
    folder = tmp_path / "01_laugh"
    folder.mkdir()
    clip = make_video(folder / "clip.mp4", 5)
    (folder / "transcript.txt").write_text("[00:00.5] GOOD THING I PUT A limiter\n", encoding="utf-8")
    made_up = [Line(4.2, 4.9, "Thanks for watching!", [[4.2, 4.9, " Thanks", 0.4]], "en", 0.1, -0.8),
               Line(4.9, 5.0, "", [], "en"), Line(1, 2, "ah", [[1, 2, " ah", 0.9]], "en", 0.9, -1.5),
               Line(2, 3, "mumble", [[2, 3, " mumble", 0.05]], "en", 0.1, -0.5)]
    engine = FakeWhisper(FakeWhisper().lines + made_up)
    d = transcribe_clip(Config(), folder, clip, engine=engine)
    assert d["model"] == "fake-turbo" and d["language"] == "en" and d["dropped"] == 4 and len(d["lines"]) == 2
    words = json.loads((folder / "words.json").read_text(encoding="utf-8"))
    assert words["lines"][0]["words"][1] == [0.8, 1.0, " thing", 0.95]
    assert (folder / "transcript.txt").read_text(encoding="utf-8") == \
        "[00:00.5] Good thing I put a limiter in there.\n[00:03.0] Are you deaf?\n"
    assert "GOOD THING" in (folder / "transcript.youtube.txt").read_text(encoding="utf-8")
    # Again: the YouTube copy stays the YouTube one.
    transcribe_clip(Config(), folder, clip, engine=engine)
    assert "GOOD THING" in (folder / "transcript.youtube.txt").read_text(encoding="utf-8")
    meta = tr.info(folder)
    assert abs(meta.pop("duration_s") - 5.0) < 0.1
    assert meta | {"created_at": "", "took_s": 0} == {"model": "fake-turbo", "language": "en", "language_p": 0.99,
                                                      "took_s": 0, "created_at": "", "dropped": 4, "lines": 2}
    long = {"start": 10.0, "text": "be able to. Oh, I pressed this, didn't I? Yeah.",
            "words": [[10.0, 10.2, " be", 1], [10.2, 10.4, " able", 1], [10.4, 10.6, " to.", 1], [11.0, 11.2, " Oh,", 1],
                      [11.2, 11.6, " I", 1], [11.6, 11.8, " pressed", 1], [11.8, 12.0, " this,", 1],
                      [12.0, 12.2, " didn't", 1], [12.2, 12.4, " I?", 1], [13.0, 13.4, " Yeah.", 1]]}
    assert tr.sentences(long) == [(10.0, "be able to."), (11.0, "Oh, I pressed this, didn't I?"), (13.0, "Yeah.")]
    tr.forget(folder)                                          # downloaded again
    assert not (folder / "words.json").exists() and tr.info(folder) is None


def _folders(conn):
    return [Path(r["folder"]) for r in conn.execute(
        "SELECT c.folder FROM clips c JOIN moments m ON m.id = c.moment_id ORDER BY m.rank")]


def test_transcribe_stage_does_each_clip_once(site):  # noqa: F811
    cfg, conn, client, deps = site
    fake = FakeWhisper()
    n = len(_folders(conn))
    out = run_stage(cfg, conn, VID, "transcribe", deps=Deps(whisper=fake))
    assert out.status == "done" and out.message == f"{n} transcribed (en)" and fake.calls == n
    assert all((f / "words.json").is_file() for f in _folders(conn))
    assert run_stage(cfg, conn, VID, "transcribe", deps=Deps(whisper=fake)).message == "every clip already transcribed"
    m = mid(conn)                                             # one clip again, on request
    jobs.enqueue(conn, conn.execute("SELECT video_id FROM moments WHERE id = ?", (m,)).fetchone()[0], "transcribe",
                 moment_pk=m)
    Worker(cfg, deps=Deps(whisper=fake)).run(once=True)
    assert fake.calls == n + 1


def test_a_queued_render_goes_first(site):  # noqa: F811
    cfg, conn, client, deps = site
    fake = FakeWhisper()
    m = mid(conn)
    client.post(f"/moment/{m}/edit", json={"segments": [[1, 5, 0]]})
    v = conn.execute("SELECT video_id FROM moments WHERE id = ?", (m,)).fetchone()[0]
    jobs.enqueue(conn, v, "render", moment_pk=m)
    out = run_stage(cfg, conn, VID, "transcribe", deps=Deps(whisper=fake))
    assert out.status == "retry" and "paused for a render" in out.message and fake.calls == 1


def test_worker_transcribes_after_clipping_only(site, monkeypatch):  # noqa: F811
    cfg, conn, client, deps = site
    monkeypatch.setattr(tr, "available", lambda: True)
    w = Worker(cfg, deps=Deps(whisper=FakeWhisper()))
    v = conn.execute("SELECT id FROM videos WHERE video_id = ?", (VID,)).fetchone()[0]
    for f in _folders(conn):
        (f / "words.json").unlink(missing_ok=True)
    jobs.enqueue(conn, v, "clip")
    w.run(once=True)
    staged = {r["stage"]: r["status"] for r in conn.execute("SELECT stage, status FROM jobs WHERE video_id = ?", (v,))}
    assert staged.get("transcribe") == "done" and all((f / "words.json").is_file() for f in _folders(conn))
    assert stage_map(conn, v)["transcribe"]["status"] == "done"
    # Without a clip download in front of it, it doesn't run: score → (clip off) → previews.
    cfg.pipeline.auto_clip = False
    conn.execute("DELETE FROM jobs WHERE video_id = ?", (v,))
    jobs.enqueue(conn, v, "score")
    w.run(once=True)
    assert conn.execute("SELECT 1 FROM jobs WHERE video_id = ? AND stage = 'transcribe'", (v,)).fetchone() is None
    assert "transcribe" not in stage_map(conn, v)


def test_editor_button_and_state(site):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    t = client.get(f"/api/moment/{m}/edit").json()["transcript"]
    assert t["source"] in ("youtube", "none") and t["state"] == "none"
    deps.whisper = FakeWhisper()
    r = client.post(f"/moment/{m}/transcribe").json()
    assert r["ok"] and r["transcript"]["state"] == "queued"
    Worker(cfg, deps=deps).run(once=True)
    d = client.get(f"/api/moment/{m}/transcript").json()
    assert d["transcript"]["source"] == "whisper" and d["transcript"]["whisper"]["model"] == "fake-turbo"
    assert [l["text"] for l in d["lines"]] == ["Good thing I put a limiter in there.", "Are you deaf?"]
    assert client.get(f"/api/moment/{m}/edit").json()["lines"][1]["t"] == 3.0     # the editor reads Whisper's lines
    cfg.transcribe.enabled = False
    assert client.post(f"/moment/{m}/transcribe").status_code == 400
