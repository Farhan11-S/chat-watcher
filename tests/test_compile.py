"""Whole-stream compilation ideas (compile/): chat thinning, the prompt, the answer, the job."""

import json

import pytest

from chatwatcher import db, jobs, store
from chatwatcher.analyze.claude_runner import ClaudeError, ClaudeResult
from chatwatcher.compile import prompt as P
from chatwatcher.compile.chat import clean, key, thin_chat
from chatwatcher.compile.parse import parse_result, parse_segment
from chatwatcher.compile.service import CompileError, compile_video, load
from chatwatcher.config import Config, PathsConfig
from chatwatcher.ingest.captions import Segment
from chatwatcher.ingest.chat_parser import ChatMessage
from chatwatcher.pipeline import Deps, run_stage
from chatwatcher.worker import Worker

from scoring_helpers import background, burst, chat
from test_ingest_service import FakeYtDlp, make_info
from test_score_service import VID, ingest_fake
from test_web import site  # noqa: F401  (fixture)


def msg(t, text, kind="chat"):
    return ChatMessage(t=t, id=f"m{t}", author="a", author_id="UCa", text=text, kind=kind)


# ── chat thinning ────────────────────────────────────────────────────────────

def test_clean_and_key():
    assert clean(":_kusa::_kusa:") == "" and clean("😂😂") == "" and clean("  ") == ""
    assert clean("bless you :_tissue:") == "bless you"
    assert len(clean("x" * 500)) == 60
    assert key("LOOOOOL!!") == key("lool") and key("Bless you!") == key("bless  you")
    assert key("草") == "草"


def test_thin_chat_keeps_the_most_repeated_per_window():
    msgs = ([msg(100 + i * 0.5, "bless you") for i in range(6)] + [msg(101, "BLESS YOU!!"), msg(102, "she ok?"),
            msg(103, "random long message that only one person typed"), msg(104, ":_emote:"),
            msg(105, "member joined", kind="member_new"), msg(-3, "before the stream")])
    out = thin_chat(msgs, window_s=10, per_window=2)
    assert [c.t for c in out] == [100]
    assert out[0].texts[0] == "bless you ×7"                    # counted across spellings
    assert len(out[0].texts) == 2 and "emote" not in " ".join(out[0].texts)
    assert thin_chat(msgs, per_window=1)[0].texts == ["bless you ×7"]


def test_thin_chat_skips_what_the_window_before_showed():
    msgs = [msg(t, "lol") for t in (1, 2, 11, 12, 21, 22)] + [msg(13, "wait what")]
    out = thin_chat(msgs, window_s=10, per_window=1)
    # 0-10 lol; 10-20 "lol" was just shown, so the next best; 20-30 lol again
    assert [(c.t, c.texts) for c in out] == [(0, ["lol"]), (10, ["wait what"]), (20, ["lol"])]
    # after a silent window the same message counts as new
    out = thin_chat([msg(1, "lol"), msg(35, "lol")], window_s=10)
    assert [c.t for c in out] == [0, 30]
    assert len(thin_chat(msgs, window_s=30, per_window=2)) == 1  # a wider window = fewer lines


def test_thin_chat_rate_on_a_busy_stream():
    msgs = chat(background(3600, per_5s=20), burst(900, "草", n=60))
    out = thin_chat(msgs)
    assert len(out) <= 361 and all(1 <= len(c.texts) <= 2 for c in out)
    assert any("草 ×" in t for c in out if 890 <= c.t <= 920 for t in c.texts)


# ── prompt ───────────────────────────────────────────────────────────────────

def test_stamp_and_transcript_lines():
    assert P.stamp(0) == "0:00:00" and P.stamp(3725.9) == "1:02:05"
    segs = [Segment(0, 4, "hello"), Segment(4, 8, "hello"), Segment(8, 12, "everyone  today"),
            Segment(21, 25, "we play"), Segment(26, 30, "a game"), Segment(60, 62, "achoo"), Segment(63, 64, " ")]
    assert P.transcript_lines(segs, line_s=20) == [
        (0, "hello everyone today"), (21, "we play a game"), (60, "achoo")]   # fragments merged, repeat dropped
    assert P.transcript_lines(segs[:5], line_s=60) == [(0, "hello everyone today"), (21, "we play a game")]  # silence splits


def test_timeline_is_chronological_with_speech_first():
    text = P.timeline([(10, "I never get scared"), (3700, "AAAAH")],
                      thin_chat([msg(10, "sure"), msg(3705, "called it"), msg(5, "hi")]))
    assert text.splitlines() == ["0:00:00 > hi", "0:00:10 I never get scared", "0:00:10 > sure",
                                 "1:01:40 AAAAH", "1:01:40 > called it"]


def _stream(hours=1.0, per_5s=10):
    n = int(hours * 3600)
    segs = [Segment(t, t + 5, f"this is what she says at second {t} of the stream") for t in range(0, n, 5)]
    return segs, background(n, per_5s=per_5s)


def test_build_puts_everything_inline_and_reports_size():
    segs, msgs = _stream()
    b = P.build({"title": "【雑談】flu stream", "channel": "Ch", "duration": 3600}, segs, msgs, output_language="English")
    assert b.level == 0 and b.chars == len(b.prompt) and b.tokens == P.estimate_tokens(b.prompt)
    assert b.speech_lines == 180 and 300 <= b.chat_lines <= 360
    for part in ("<timeline>", "0:00:00 this is what", "\n0:59:40 ", " > ", "【雑談】flu stream", "Length: 1:00:00",
                 "recurring", "callback", "in English", "H:MM:SS"):
        assert part in b.prompt
    assert b.size == {"chars": b.chars, "est_tokens": b.tokens, "speech_lines": 180,
                      "chat_lines": b.chat_lines, "level": 0}
    assert set(P.SCHEMA["properties"]["compilations"]["items"]["required"]) == {
        "title", "kind", "reason", "rating", "segments"}


def test_build_gets_coarser_to_fit_the_budget():
    segs, msgs = _stream()
    info = {"title": "t", "duration": 3600}
    full = P.build(info, segs, msgs, output_language="English")
    less = P.build(info, segs, msgs, output_language="English", budget=full.tokens - 1)
    assert 0 < less.level and less.tokens < full.tokens and less.chars < full.chars
    no_chat = P.build(info, segs, [], output_language="English")
    assert no_chat.chat_lines == 0 and " > " not in no_chat.prompt and "chat typed" not in no_chat.prompt
    last = P.build(info, segs, msgs, output_language="English", budget=no_chat.tokens)
    assert last.level == len(P.LEVELS) - 1 and last.chat_lines == 0      # chat dropped before giving up
    assert P.build(info, segs, msgs, output_language="English", budget=1000) is None


def test_estimate_tokens_counts_japanese_heavier():
    assert P.estimate_tokens("a" * 300) == 100
    assert P.estimate_tokens("草" * 100) == 110


def test_five_hour_stream_fits_one_call():
    segs, msgs = _stream(hours=5, per_5s=20)
    b = P.build({"title": "long", "duration": 5 * 3600}, segs, msgs, output_language="English")
    assert b is not None and b.tokens <= P.BUDGET_TOKENS


# ── the answer ───────────────────────────────────────────────────────────────

def test_parse_segment():
    assert parse_segment("0:14:05-0:14:20 | setup | says she never gets scared") == {
        "start": 845.0, "end": 860.0, "role": "setup", "note": "says she never gets scared"}
    assert parse_segment("14:05 – 14:20 | a note | with a pipe")["note"] == "a note | with a pipe"   # no role
    assert parse_segment("1:02:03-1:02:30|PAYOFF|x")["role"] == "payoff"
    assert parse_segment("1:02:03-1:02:30 | just a note")["role"] == "beat"
    assert parse_segment({"start": "0:00:10", "end": "0:00:20", "role": "setup", "note": "n"})["start"] == 10
    assert parse_segment("0:10:00-0:09:00 | beat | backwards") is None
    assert parse_segment("soon | beat | no time") is None
    assert parse_segment("0:59:50-1:10:00 | beat | x", duration=3600)["end"] == 3600        # clamped to the stream
    assert parse_segment("0:00:10-0:30:00 | beat | x")["end"] == 310                        # never minutes long
    assert parse_segment("1:10:00-1:11:00 | beat | x", duration=3600) is None                # past the end


def answer():
    return {"compilations": [
        {"title": "Weak one", "kind": "other", "reason": "meh", "rating": 5,
         "segments": ["0:01:00-0:01:10 | beat | a", "0:20:00-0:20:10 | setup | b"]},
        {"title": "She sneezes 10 times", "kind": "recurring", "reason": "Flu stream. Chat counts every sneeze.",
         "rating": 9, "segments": ["0:05:00-0:05:08 | beat | first sneeze", "0:25:00-0:25:10 | beat | mid-sentence",
                                   "0:25:00-0:25:10 | beat | same piece again", "0:50:10-0:50:20 | beat | the tenth"]},
        {"title": "\"I never get scared\"", "kind": "CALLBACK", "reason": "Setup and payoff 40 minutes apart.",
         "rating": 12, "segments": ["0:10:00-0:10:15 | setup | the claim", "0:50:00-0:50:20 | payoff | the scream"]},
        {"title": "One piece only", "kind": "recurring", "reason": "", "rating": 8,
         "segments": ["0:05:00-0:05:08 | beat | x", "nonsense"]},
        {"title": "", "kind": "other", "reason": "", "rating": 7, "segments": ["0:01:00-0:01:10", "0:02:00-0:02:10"]},
        "not an object",
    ]}


def test_parse_result_validates():
    out = parse_result(answer(), duration=3600)
    assert [c["title"] for c in out] == ['"I never get scared"', "She sneezes 10 times", "Weak one"]   # best first
    cb, rec, weak = out
    assert cb["kind"] == "callback" and cb["rating"] == 10
    assert [s["role"] for s in cb["segments"]] == ["setup", "payoff"]
    assert len(rec["segments"]) == 3 and rec["segments"][0] == {
        "start": 300.0, "end": 308.0, "role": "beat", "note": "first sneeze"}
    assert [s["role"] for s in weak["segments"]] == ["beat", "beat"]         # roles are for callbacks
    assert parse_result({}) == [] and parse_result({"compilations": "x"}) == [] and parse_result(None) == []
    assert parse_result({"compilations": []}) == []


# ── the service and the job ──────────────────────────────────────────────────

class FakeClaude:
    def __init__(self, data=None, error=None, logged_in=True):
        self.data, self.error, self.logged_in = data if data is not None else answer(), error, logged_in
        self.calls: list[dict] = []

    def auth_status(self):
        return {"loggedIn": self.logged_in}

    def run(self, prompt, schema, *, system, cwd, **kw):
        self.calls.append({"prompt": prompt, "schema": schema, "system": system, "cwd": cwd, **kw})
        if self.error:
            raise self.error
        return ClaudeResult(self.data, "claude-opus-5-5", 1, {"usage": {
            "input_tokens": 1200, "cache_creation_input_tokens": 300, "output_tokens": 400}})


@pytest.fixture
def env(tmp_path):
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data", output_dir=tmp_path / "out"))
    conn = db.open_db(cfg.paths.db_path)
    yield cfg, conn
    conn.close()


def test_compile_makes_one_call_and_stores_candidates(env):
    cfg, conn = env
    ingest_fake(cfg, conn)
    fc, said = FakeClaude(), []
    run = compile_video(cfg, conn, VID, runner=fc, on_step=said.append)
    assert len(fc.calls) == 1
    call = fc.calls[0]
    assert "0:19:10 それでさ、\n" in call["prompt"] and "草 ×" in call["prompt"]   # all inline
    assert call["schema"] is P.SCHEMA and "allow_read" not in call and "images" not in call
    assert run.size["chars"] == len(call["prompt"]) and run.input_tokens == 1500
    assert any(f"{len(call['prompt']):,} characters" in m for m in said)       # the input size is logged
    v = store.get_video(conn, VID)
    got = load(conn, v["id"])
    assert [c["title"] for c in got["candidates"]] == ['"I never get scared"', "She sneezes 10 times", "Weak one"]
    assert got["candidates"][0]["length_s"] == 35 and got["model"] == "claude-opus-5-5"
    assert got["input"]["chars"] == len(call["prompt"]) and got["usage"]["output"] == 400
    # a second run replaces the first
    compile_video(cfg, conn, VID, runner=FakeClaude({"compilations": []}))
    assert load(conn, v["id"])["candidates"] == []
    assert conn.execute("SELECT COUNT(*) FROM compile_runs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM compilations").fetchone()[0] == 0


def test_compile_fetches_captions_that_appeared_after_ingest(env):
    cfg, conn = env
    f = ingest_fake(cfg, conn, transcript=False)
    fc, yt = FakeClaude(), FakeYtDlp()
    compile_video(cfg, conn, VID, runner=fc, ytdlp=yt)
    assert yt.caption_calls == 1 and f.transcript.is_file() and len(fc.calls) == 1
    assert store.get_video(conn, VID)["caption_lang"] == "ja-orig" and not f.downloads.exists()
    compile_video(cfg, conn, VID, runner=fc, ytdlp=yt)
    assert yt.caption_calls == 1                                             # saved: not fetched again


def test_compile_without_captions_says_so_and_never_calls_claude(env):
    cfg, conn = env
    ingest_fake(cfg, conn, transcript=False)
    fc = FakeClaude()
    yt = FakeYtDlp(info=make_info(automatic_captions={}))
    with pytest.raises(CompileError) as e:
        compile_video(cfg, conn, VID, runner=fc, ytdlp=yt)
    assert e.value.kind == "no_captions" and "no captions" in str(e.value) and fc.calls == []
    with pytest.raises(CompileError) as e:
        compile_video(cfg, conn, "zzzzzzzzzzz", runner=fc)
    assert e.value.kind == "not_ingested"
    cfg.claude.enabled = False
    with pytest.raises(CompileError) as e:
        compile_video(cfg, conn, VID, runner=fc)
    assert e.value.kind == "disabled"


def test_compile_stage(env):
    cfg, conn = env
    ingest_fake(cfg, conn)
    fc, said = FakeClaude(), []
    out = run_stage(cfg, conn, VID, "compile", deps=Deps(claude=fc), log=said.append)
    assert out.status == "done" and out.message.startswith("3 compilation ideas (Claude read 1,500 tokens")
    assert len(fc.calls) == 1 and any("input:" in m for m in said)

    # a bad answer is an error, not another call
    bad = FakeClaude(error=ClaudeError("claude returned no structured output", kind="bad_output"))
    out = run_stage(cfg, conn, VID, "compile", deps=Deps(claude=bad))
    assert out.status == "error" and len(bad.calls) == 1
    limit = FakeClaude(error=ClaudeError("usage limit reached", kind="usage_limit"))
    out = run_stage(cfg, conn, VID, "compile", deps=Deps(claude=limit))
    assert out.status == "retry" and out.retry_in.total_seconds() == 3600
    out = run_stage(cfg, conn, VID, "compile", deps=Deps(claude=FakeClaude(logged_in=False)))
    assert out.status == "retry" and "logged in" in out.message
    # the earlier good run is still there
    assert len(load(conn, store.get_video(conn, VID)["id"])["candidates"]) == 3
    cfg.claude.enabled = False
    assert run_stage(cfg, conn, VID, "compile", deps=Deps(claude=fc)).status == "skip"


def test_compile_job_runs_alone_and_leaves_the_stream_as_it_was(env):
    cfg, conn = env
    ingest_fake(cfg, conn)
    pk = store.upsert_video(conn, VID, status="done")
    assert "compile" not in jobs.STAGES and jobs.next_stage("compile") is None
    with pytest.raises(ValueError):
        jobs.enqueue(conn, pk, "compile", moment_pk=1)
    jobs.enqueue(conn, pk, "compile")
    fc = FakeClaude()
    assert Worker(cfg, deps=Deps(claude=fc)).run(once=True) == 1              # one job, nothing chained
    assert len(fc.calls) == 1
    rows = conn.execute("SELECT stage, status, message FROM jobs").fetchall()
    assert [(r["stage"], r["status"]) for r in rows] == [("compile", "done")]
    assert "3 compilation ideas" in rows[0]["message"]
    assert store.get_video(conn, VID)["status"] == "done"

    # failing doesn't mark the stream as broken
    jobs.enqueue(conn, pk, "compile")
    Worker(cfg, deps=Deps(claude=FakeClaude(error=ClaudeError("boom", kind="error")))).run(once=True)
    job = conn.execute("SELECT status, error FROM jobs").fetchone()
    assert job["status"] == "error" and "boom" in job["error"]
    v = store.get_video(conn, VID)
    assert v["status"] == "done" and not v["error"]


# ── the stream page ──────────────────────────────────────────────────────────

def test_stream_page_button_queues_one_job_and_lists_candidates(site):
    cfg, conn, client, deps = site
    r = client.get(f"/video/{VID}")
    assert "Compilation ideas" in r.text and "Find compilation ideas" in r.text
    assert f'action="/video/{VID}/compile"' in r.text and "data-stream-seek" not in r.text

    r = client.post(f"/video/{VID}/compile", follow_redirects=False)
    assert r.status_code == 303 and "Queued" in r.headers["location"] and r.headers["location"].endswith("#compilations")
    rows = conn.execute("SELECT status, moment_id FROM jobs WHERE stage = 'compile'").fetchall()
    assert [(j["status"], j["moment_id"]) for j in rows] == [("pending", None)]
    client.post(f"/video/{VID}/compile")                                     # pressing twice: still one job
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE stage = 'compile'").fetchone()[0] == 1
    page = client.get(f"/video/{VID}").text
    assert "waiting in the queue" in page and "disabled" in page.split("/compile")[1].split("</form>")[0]

    fc = FakeClaude()
    assert Worker(cfg, deps=Deps(claude=fc)).run(once=True) == 1 and len(fc.calls) == 1
    page = client.get(f"/video/{VID}").text
    for part in ("She sneezes 10 times", "keeps happening", "setup and payoff", "Flu stream. Chat counts every sneeze.",
                 f'href="https://youtu.be/{VID}?t=600"', 'data-stream-seek="600.0"', "▶ 10:00–10:15",
                 '<span class="tag">setup</span>', '<span class="tag">payoff</span>', "the scream",
                 "3 pieces", "Look again", "Claude read 1,500 tokens in one call"):
        assert part in page, part
    assert "Find compilation ideas" not in page and "waiting in the queue" not in page

    # a failure shows on the card, with the old ideas kept
    jobs.enqueue(conn, store.get_video(conn, VID)["id"], "compile")
    Worker(cfg, deps=Deps(claude=FakeClaude(error=ClaudeError("claude timed out after 1200s", kind="timeout")))).run(once=True)
    page = client.get(f"/video/{VID}").text
    assert "claude timed out" in page and "She sneezes 10 times" in page

    cfg.claude.enabled = False
    r = client.post(f"/video/{VID}/compile", follow_redirects=False)
    assert "error=" in r.headers["location"]
    assert "Compilation ideas" not in client.get(f"/video/{VID}").text
    assert client.post("/video/zzzzzzzzzzz/compile").status_code == 404


def test_migration_keeps_jobs_and_allows_compile(tmp_path):
    conn = db.connect(tmp_path / "old.db")
    conn.executescript("BEGIN;" + "".join(db.MIGRATIONS[:21]) + "PRAGMA user_version = 21; COMMIT;")
    v = conn.execute("INSERT INTO videos (video_id) VALUES ('abcdefghijk')").lastrowid
    conn.execute("INSERT INTO jobs (video_id, stage, message, params_json) VALUES (?, 'clip', 'm', '{\"a\": 1}')", (v,))
    assert db.migrate(conn) == [22, 23, 24]
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert (row["stage"], row["message"], json.loads(row["params_json"])) == ("clip", "m", {"a": 1})
    conn.execute("INSERT INTO jobs (video_id, stage) VALUES (?, 'compile')", (v,))
    conn.execute("INSERT INTO jobs (video_id, stage) VALUES (?, 'thumb')", (v,))          # 24: Claude's thumbnails
    conn.close()
