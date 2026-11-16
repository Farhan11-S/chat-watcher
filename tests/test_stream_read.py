"""✨ Read the whole stream, beyond compilation ideas: listening first (compile.speech), the
stream's context, the moments chat missed, and the context in clip analyses."""

import json

import pytest

from chatwatcher import db, jobs, store
from chatwatcher.analyze.service import analyze_video
from chatwatcher.compile import speech
from chatwatcher.compile.parse import parse_context, parse_missed
from chatwatcher.compile.service import compile_video, context, context_text, load
from chatwatcher.config import Config, PathsConfig
from chatwatcher.ingest.captions import read_transcript
from chatwatcher.paths import vod_files
from chatwatcher.pipeline import Deps, run_stage
from chatwatcher.scoring.service import score_video
from chatwatcher.subs.transcribe import Line
from chatwatcher.worker import Worker

from test_analyze import FakeRunner
from test_compile import FakeClaude, answer
from test_score_service import VID, ingest_fake
from test_web import site  # noqa: F401  (fixture)


def read_answer(**extra):
    return answer() | {
        "summary": "A calm chat stream about her week; she tells the vending machine story.",
        "chapters": ["0:00:00-0:15:00 | greetings and her week", "0:15:00-0:35:00 | the vending machine story",
                     "nonsense line", "0:35:00-1:00:00 | reading superchats"],
        "running_jokes": ["the cursed vending machine (first at 0:16:10)"],
        "names": ["Kronii: her friend (heard as 'crony')"],
        "missed": [
            {"span": "0:30:00-0:31:00", "peak": "0:30:40", "title": "The vending machine ate her card",
             "category": "funny", "why": "A slow build to a great punchline.", "rating": 8},
            {"span": "0:19:50-0:20:15", "peak": "0:20:00", "title": "Chat already had this", "category": "funny",
             "why": "x", "rating": 7},
            {"span": "0:40:00-0:40:30", "peak": "9:99:99", "title": "Too weak", "category": "other", "why": "", "rating": 3},
        ],
    } | extra


@pytest.fixture
def env(tmp_path):
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data", output_dir=tmp_path / "out"))
    cfg.compile.part_s = 900
    conn = db.open_db(cfg.paths.db_path)
    yield cfg, conn
    conn.close()


# ── parsing ──────────────────────────────────────────────────────────────────

def test_parse_context_and_missed():
    cx = parse_context(read_answer(), 3600)
    assert cx["summary"].startswith("A calm chat stream")
    assert [c["text"] for c in cx["chapters"]] == ["greetings and her week", "the vending machine story", "reading superchats"]
    assert cx["chapters"][1] == {"start": 900.0, "end": 2100.0, "text": "the vending machine story"}
    assert cx["jokes"] and cx["names"] == ["Kronii: her friend (heard as 'crony')"]
    assert parse_context(None) == {"summary": "", "chapters": [], "jokes": [], "names": []}

    missed = parse_missed(read_answer(), 3600)
    assert [m["title"] for m in missed] == ["The vending machine ate her card", "Chat already had this"]   # rating 3 dropped
    assert missed[0] | {} == {"start": 1800.0, "end": 1860.0, "peak": 1840.0, "title": "The vending machine ate her card",
                              "rating": 8, "category": "funny", "why": "A slow build to a great punchline."}
    short = parse_missed({"missed": [{"span": "0:10:00-0:10:04", "peak": "", "title": "t", "category": "nope", "rating": 9}]})
    assert short[0]["end"] - short[0]["start"] == 15 and short[0]["category"] == "other"                 # widened
    assert short[0]["start"] <= short[0]["peak"] <= short[0]["end"]
    long = parse_missed({"missed": [{"span": "0:10:00-0:30:00", "title": "t", "rating": 9}]})
    assert long[0]["end"] - long[0]["start"] == 240                                                     # capped
    assert parse_missed({"missed": "x"}) == [] and parse_missed(read_answer(), 3600, limit=1)[0]["rating"] == 8


# ── listening ────────────────────────────────────────────────────────────────

class FakeAudio:
    """Stands in for decoded samples: only its length matters."""

    def __init__(self, seconds):
        self.n = int(seconds * speech.RATE)

    def __len__(self):
        return self.n


class FakeWhisper:
    name = "fake-turbo"

    def __init__(self):
        self.calls = []

    def run(self, audio, language):
        secs = len(audio) / speech.RATE
        self.calls.append(secs)
        k = len(self.calls)
        words = [[0.0, 0.5, " hi", 0.9]]
        return [Line(2.0, 6.0, f"part {k} starts here", words),
                Line(secs - 3, secs - 1, f"part {k} tail", words),          # in the next part's time unless last
                Line(10.0, 12.0, "", [])], "en", 0.99                       # invented: dropped


class FakeAudioYt:
    def __init__(self):
        self.downloads = 0

    def download_audio(self, video_id, dest, fmt):
        self.downloads += 1
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "audio.webm").write_bytes(b"x" * 1024)
        return dest / "audio.webm"


@pytest.fixture
def fake_decode(monkeypatch):
    calls = []

    def decode(audio, start, length):
        calls.append((start, length))
        return FakeAudio(min(length, 3600 - start))
    monkeypatch.setattr(speech, "decode", decode)
    return calls


def test_transcribe_stream_in_parts_resumes_and_cleans_up(env, fake_decode):
    cfg, conn = env
    f = ingest_fake(cfg, conn)
    yt, w = FakeAudioYt(), FakeWhisper()
    pauses = iter([False, True])
    done = speech.transcribe_stream(cfg, f, VID, 3600, ytdlp=yt, engine=w, should_pause=lambda: next(pauses))
    assert done is False and len(list(f.listen.glob("part_*.json"))) == 2 and not f.speech.exists()
    assert speech.progress(f, 3600, 900) == {"done": 2, "total": 4, "finished": False, "meta": {}}
    assert fake_decode[:2] == [(0, 905.0), (900, 905.0)]                      # each part hears 5 s of the next

    assert speech.transcribe_stream(cfg, f, VID, 3600, ytdlp=yt, engine=w) is True
    assert yt.downloads == 1 and fake_decode[-1] == (2700, 900)               # the last part needs no extra
    segs = read_transcript(f.speech)
    assert [(s.start, s.text) for s in segs] == [
        (2.0, "part 1 starts here"), (902.0, "part 2 starts here"), (1802.0, "part 3 starts here"),
        (2702.0, "part 4 starts here"), (3597.0, "part 4 tail")]
    assert sorted(p.name for p in f.listen.iterdir()) == [speech.META]        # parts and audio gone
    p = speech.progress(f, 3600, 900)
    assert p["finished"] and p["meta"]["model"] == "fake-turbo" and p["meta"]["language"] == "en"
    assert speech.stream_transcript(f) == (segs, "whisper")
    assert speech.transcribe_stream(cfg, f, VID, 3600, ytdlp=yt, engine=w) is True and yt.downloads == 1
    speech.forget(f)
    assert speech.stream_transcript(f)[1] == "captions"


def test_read_after_listening_finds_missed_moments_and_context(env, fake_decode):
    cfg, conn = env
    f = ingest_fake(cfg, conn)
    store.upsert_video(conn, VID, duration_s=3600, status="done")
    score_video(cfg, conn, VID)
    scored = conn.execute("SELECT COUNT(*) FROM moments").fetchone()[0]
    fc, said = FakeClaude(read_answer()), []
    run = compile_video(cfg, conn, VID, runner=fc, ytdlp=FakeAudioYt(), whisper=FakeWhisper(),
                        listen=True, on_step=said.append)
    prompt = fc.calls[0]["prompt"]
    assert run.source == "whisper" and "Whisper transcript" in prompt and "0:15:02 part 2 starts here" in prompt
    assert "それでさ" not in prompt                                         # the captions aren't read
    assert "<known_moments>\n0:19:" in prompt or "<known_moments>\n0:" in prompt
    assert any("input (Whisper transcript)" in m for m in said)

    rows = conn.execute("SELECT * FROM moments WHERE kind = 'missed'").fetchall()
    assert len(rows) == 1 and rows[0]["rank"] == 501 and rows[0]["status"] == "new"
    assert (rows[0]["start_s"], rows[0]["end_s"], rows[0]["peak_s"]) == (1800, 1860, 1840)
    feats = json.loads(rows[0]["features_json"])
    assert feats["missed"]["title"] == "The vending machine ate her card" and feats["reasons"][0]["signal"] == "heard"
    got = load(conn, store.get_video(conn, VID)["id"])
    assert got["source"] == "whisper" and got["context"]["names"] and len(got["candidates"]) == 3
    assert got["missed"][0]["moment_id"] == rows[0]["id"]
    assert isinstance(got["missed"][1]["same_as"], int)                     # the one chat had already

    # a second read doesn't add it twice; a re-score keeps it
    compile_video(cfg, conn, VID, runner=FakeClaude(read_answer()))
    assert conn.execute("SELECT COUNT(*) FROM moments WHERE kind = 'missed'").fetchone()[0] == 1
    assert load(conn, store.get_video(conn, VID)["id"])["missed"][0]["same_as"] == 501
    score_video(cfg, conn, VID, force=True)
    assert conn.execute("SELECT COUNT(*) FROM moments").fetchone()[0] == scored + 1


def test_context_text():
    cx = parse_context(read_answer(), 3600)
    t = context_text(cx, 1800, 1860)
    assert t.startswith("The whole stream: A calm chat stream")
    assert "0:15:00-0:35:00 the vending machine story ← this clip" in t and "greetings" not in t
    assert "Running jokes: the cursed vending machine" in t and "Names (right spellings): Kronii" in t
    assert "Around this clip" not in context_text(cx)                       # a compilation clip: no stream times
    assert context_text({}) == "" and context_text({"summary": "", "chapters": []}) == ""


# ── the job ──────────────────────────────────────────────────────────────────

def test_stage_gives_way_between_parts_without_using_up_attempts(env, fake_decode):
    cfg, conn = env
    ingest_fake(cfg, conn)
    pk = store.upsert_video(conn, VID, duration_s=3600, status="done")
    jobs.enqueue(conn, pk, "compile", params={"listen": True})
    jobs.enqueue(conn, pk, "ingest", run_after=jobs.now().replace(year=2000))   # other work waiting
    conn.execute("UPDATE jobs SET run_after = '2000-01-01 00:00:00' WHERE stage = 'compile'")
    fc = FakeClaude(read_answer())
    w = Worker(cfg, deps=Deps(claude=fc, whisper=FakeWhisper(), ytdlp=FakeAudioYt()))
    job = jobs.claim(conn)
    assert job["stage"] == "compile"
    w.process(conn, job)
    row = conn.execute("SELECT * FROM jobs WHERE stage = 'compile'").fetchone()
    assert row["status"] == "pending" and row["attempts"] == 0 and "1 of 4 parts done" in row["message"]
    assert fc.calls == []
    v = store.get_video(conn, VID)
    assert v["status"] == "done" and not v["error"]                         # the stream itself is untouched

    conn.execute("DELETE FROM jobs WHERE stage = 'ingest'")
    out = run_stage(cfg, conn, VID, "compile", deps=Deps(claude=fc, whisper=FakeWhisper(), ytdlp=FakeAudioYt()),
                    params={"listen": True, "reanalyze": True, "analyze_new": True, "bogus": 1})
    assert out.status == "done" and len(fc.calls) == 1
    assert out.message.startswith("3 compilation ideas, 2 moments chat missed (Claude read 1,500 tokens from the Whisper transcript")
    queued = {r["stage"]: jobs.job_params(r) for r in conn.execute("SELECT * FROM jobs WHERE status = 'pending' AND stage != 'compile'")}
    assert queued == {"analyze": {"redo": True}, "frames": {}}


def test_analysis_gets_the_stream_context(site):
    cfg, conn, client, deps = site
    v = store.get_video(conn, VID)
    compile_video(cfg, conn, VID, runner=FakeClaude(read_answer()))
    assert context(conn, v["id"])["summary"]
    fr = FakeRunner()
    analyze_video(cfg, conn, VID, runner=fr, ranks=[1], force=True)
    p = fr.calls[0]["prompt"]
    assert "<stream_context>\nThe whole stream: A calm chat stream" in p and "Kronii" in p
    a = json.loads(conn.execute("SELECT analysis_json FROM analyses ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert a["_meta"]["stream_context"] is True


def test_stream_page_card(site):
    cfg, conn, client, deps = site
    page = client.get(f"/video/{VID}").text
    assert "Read the whole stream" in page and 'name="analyze_new"' in page and 'name="reanalyze"' in page
    r = client.post(f"/video/{VID}/compile", data={"listen": "1", "analyze_new": "1"}, follow_redirects=False)
    assert r.status_code == 303 and "Queued" in r.headers["location"]
    job = conn.execute("SELECT * FROM jobs WHERE stage = 'compile'").fetchone()
    assert jobs.job_params(job) == {"listen": True, "analyze_new": True}
    conn.execute("DELETE FROM jobs WHERE stage = 'compile'")

    compile_video(cfg, conn, VID, runner=FakeClaude(read_answer()))
    page = client.get(f"/video/{VID}").text
    for part in ("What the stream was about", "the vending machine story", "Running jokes:", "Kronii",
                 "Moments chat missed", "The vending machine ate her card", "#501", "🎧",
                 "already moment #", "chat quiet · heard by Claude 8/10", "Compilation ideas",
                 "with this context", "from YouTube&#39;s captions"):
        assert part in page, part
    r = client.post(f"/video/{VID}/reanalyze", follow_redirects=False)
    assert r.status_code == 303
    job = conn.execute("SELECT * FROM jobs WHERE stage = 'analyze' AND status = 'pending'").fetchone()
    assert jobs.job_params(job) == {"redo": True}
