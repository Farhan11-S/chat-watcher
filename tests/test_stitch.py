"""A compilation idea as one clip (clip/stitch.py): its pieces downloaded, joined, and
opened by the editor and the AI edit like any clip."""

import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote

import pytest

from chatwatcher import jobs, store
from chatwatcher.clip import stitch
from chatwatcher.clip.media import concat, probe
from chatwatcher.clip.preview import preview_video
from chatwatcher.clip.service import clip_video
from chatwatcher.compile.service import compile_video
from chatwatcher.edit import pieces as pc
from chatwatcher.edit.autoedit.prompt import compilation_text
from chatwatcher.edit.autoedit.run import material
from chatwatcher.edit.service import clip_ref
from chatwatcher.ingest.chat_parser import read_messages
from chatwatcher.paths import vod_files
from chatwatcher.pipeline import Deps, run_stage
from chatwatcher.scoring.service import score_video
from chatwatcher.worker import Worker

from test_analyze import FakeRunner
from test_clip import FakeYt, make_video
from test_compile import FakeClaude
from test_direct import WORDS
from test_score_service import VID
from test_web import site  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def test_ranges_pad_clamp_and_join_neighbours():
    segs = [{"start": 2, "end": 10, "role": "setup", "note": "a"}, {"start": 12, "end": 20, "role": "beat", "note": "b"},
            {"start": 3590, "end": 3600, "role": "payoff", "note": "c"}, {"start": 50, "end": 50.5, "role": "beat", "note": ""}]
    out = stitch.ranges(segs, duration=3600, pad=4)
    assert [(r["start"], r["end"]) for r in out] == [(0.0, 24.0), (3586.0, 3600.0), (46.0, 54.5)]
    assert out[0]["role"] == "setup + beat" and out[0]["note"] == "a; b" and out[1]["role"] == "payoff"
    assert stitch.ranges([{"start": 5, "end": 5.2}], pad=0) == []            # nothing left of it


def test_concat_copies_like_files_and_re_encodes_others(tmp_path):
    a, b = make_video(tmp_path / "a.mp4", 4), make_video(tmp_path / "b.mp4", 6)
    out = probe(concat([a, b], tmp_path / "ab.mp4"))
    assert out.duration_s == pytest.approx(10, abs=0.3) and (out.width, out.vcodec, out.acodec) == (320, "h264", "aac")
    assert not list(tmp_path.glob("*.txt"))

    small = tmp_path / "small.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=3:size=160x120:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=300:duration=3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(small)], check=True)
    out = probe(concat([a, small, a], tmp_path / "mixed.mp4"))
    assert out.duration_s == pytest.approx(11, abs=0.4) and (out.width, out.height) == (320, 240)   # the first one's size
    out = probe(concat([a, b], tmp_path / "forced.mp4", copy=False))
    assert out.duration_s == pytest.approx(10, abs=0.3)


def test_no_piece_runs_across_a_join():
    words = {"lines": [{"words": [[1.0, 2.0, " a"], [2.1, 3.0, " b."], [3.2, 4.0, " c"], [6.0, 7.0, " d."]]}]}
    edges = lambda j: [(p.kind, round(p.t0, 2), round(p.t1, 2)) for p in pc.build(words, 9.0, joins=[j])]  # noqa: E731
    assert edges(3.1) == [("quiet", 0, 1), ("talk", 1, 3.1), ("talk", 3.1, 4), ("quiet", 4, 6), ("talk", 6, 7), ("quiet", 7, 9)]
    assert ("quiet", 4, 5) in edges(5.0) and ("quiet", 5, 6) in edges(5.0)             # a quiet stretch is split
    assert ("quiet", 0, 0.5) in edges(0.5) and ("quiet", 7.5, 9) in edges(7.5)
    # a word Whisper ran across the jump ends at the jump
    cut = pc.build(words, 9.0, joins=[3.6])
    talk = next(p for p in cut if p.kind == "talk" and p.t0 == 1.0)
    assert (talk.t1, talk.w1) == (3.6, 3.6) and cut[cut.index(talk) + 1].t0 == 3.6
    assert [p.id for p in cut] == [f"p{i}" for i in range(1, len(cut) + 1)]
    near = pc.build(words, 9.0, joins=[3.97])                                  # 30 ms before an edge: the edge moves
    talk = next(p for p in near if p.kind == "talk" and p.t0 == 1.0)
    assert (talk.t1, talk.w1) == (3.97, 3.97) and near[near.index(talk) + 1].t0 == 3.97 and len(near) == 5
    assert [(p.t0, p.t1) for p in pc.build(words, 9.0)] == [(p.t0, p.t1) for p in pc.build(words, 9.0, joins=[])]


def ideas(cfg, conn):
    compile_video(cfg, conn, VID, runner=FakeClaude())
    return {c["title"]: c["id"] for c in conn.execute("SELECT id, title FROM compilations")}


def test_an_idea_becomes_one_joined_clip(site, tmp_path):  # noqa: F811
    cfg, conn, client, deps = site
    video_pk = store.get_video(conn, VID)["id"]
    scored = conn.execute("SELECT COUNT(*) FROM moments").fetchone()[0]
    by_title = ideas(cfg, conn)
    assert stitch.create(conn, video_pk, 999_999) is None
    pk = stitch.create(conn, video_pk, by_title["She sneezes 10 times"])
    assert stitch.create(conn, video_pk, by_title["She sneezes 10 times"]) == pk          # once per idea
    m = conn.execute("SELECT * FROM moments WHERE id = ?", (pk,)).fetchone()
    assert (m["kind"], m["rank"], m["status"], m["start_s"]) == ("compilation", 1001, "approved", 0)

    # not something the automatic download, the previews or a re-score touch
    yt = FakeYt(tmp_path / "src.mp4")
    assert all(o.rank < 1000 for o in clip_video(cfg, conn, VID, ytdlp=yt).outcomes) and not yt.calls
    assert preview_video(cfg, conn, VID, ytdlp=yt).skipped == scored and not yt.calls

    said = []
    out = run_stage(cfg, conn, VID, "clip", moment_id=pk, deps=Deps(ytdlp=yt), log=said.append)
    assert out.status == "done", out.message
    assert [a[a.index("--download-sections") + 1] for a in yt.calls] == [
        "*296.00-312.00", "*1496.00-1514.00", "*3006.00-3024.00"]                      # 4 s spare each side
    assert any("part 2 of 3" in s for s in said) and any("joining" in s for s in said)

    clip = clip_ref(conn, pk)
    assert clip.zero_s == 0 and clip.duration_s == pytest.approx(60, abs=0.5) and clip.path.name == "clip.mp4"
    assert clip.folder.name.startswith("c01_She_sneezes") and not (clip.folder / "parts").exists()
    m = conn.execute("SELECT * FROM moments WHERE id = ?", (pk,)).fetchone()
    assert m["start_s"] == 0 and m["end_s"] == pytest.approx(clip.duration_s)
    parts = json.loads(m["features_json"])["compilation"]["parts"]
    assert [p["t0"] for p in parts] == pytest.approx([0, 20, 40], abs=0.3)
    assert [p["start"] for p in parts] == pytest.approx([292, 1494, 3004], abs=0.3)      # the fake file is 20 s
    assert [p["note"] for p in parts] == ["first sneeze", "mid-sentence", "the tenth"]

    meta = json.loads((clip.folder / "moment.json").read_text())
    assert meta["kind"] == "compilation" and meta["clip"]["zero_s"] == 0 and len(meta["compilation"]["parts"]) == 3
    assert meta["youtube_url"].endswith("?t=292") and meta["stream_range"] == ["4:52", "50:24"]
    assert len(meta["frames"]) >= 3 and all(f["clip_s"] < 61 for f in meta["frames"])
    assert stitch.joins(meta) == pytest.approx([20, 40], abs=0.3)

    # chat is timed on the joined clip: a message 5 s into the third part is at 45 s
    msg = next(x for x in read_messages(vod_files(cfg, VID).chat) if 3006 <= x.t <= 3020)
    at = parts[2]["t0"] + msg.t - parts[2]["start"]
    lines = (clip.folder / "chat_excerpt.txt").read_text().splitlines()
    assert f"[{int(at // 60):02d}:{at % 60:04.1f}] {msg.author}: {msg.text}" in lines
    times = pc.chat_times("\n".join(lines))
    assert min(times) < 20 < max(times) <= 61 and times == sorted(times)

    # the AI edit sees it as a compilation, and no piece runs across a join
    (clip.folder / "words.json").write_text(json.dumps(WORDS))
    mat = material(cfg, conn, pk, "long")
    edges = [p.t0 for p in mat.ps]
    assert any(e == pytest.approx(parts[1]["t0"]) for e in edges) and any(e == pytest.approx(parts[2]["t0"]) for e in edges)
    prompt = mat.prompt()
    for part in ("COMPILATION: 3 parts", '"She sneezes 10 times" (something that keeps happening)',
                 "from 4:52 of the stream · first sneeze", "Keep the parts in this order"):
        assert part in prompt, part
    assert compilation_text(None) == "" and compilation_text({"parts": parts[:1]}) == ""

    # a re-score replaces the scored moments and leaves this one alone
    score_video(cfg, conn, VID, force=True)
    assert conn.execute("SELECT kind FROM moments WHERE id = ?", (pk,)).fetchone()["kind"] == "compilation"
    assert clip_ref(conn, pk).path.is_file()
    assert conn.execute("SELECT COUNT(*) FROM moments WHERE kind != 'compilation'").fetchone()[0] == scored


def test_make_this_video_on_the_stream_page(site, tmp_path):  # noqa: F811
    cfg, conn, client, deps = site
    by_title = ideas(cfg, conn)
    idea = by_title['"I never get scared"']
    page = client.get(f"/video/{VID}").text
    assert page.count("Make this video") == 3 and f'action="/video/{VID}/stitch"' in page

    r = client.post(f"/video/{VID}/stitch", data={"idea": idea}, follow_redirects=False)
    assert r.status_code == 303 and "Queued" in r.headers["location"] and r.headers["location"].endswith("#compilations")
    pk = conn.execute("SELECT id FROM moments WHERE kind = 'compilation'").fetchone()["id"]
    assert [(j["stage"], j["status"]) for j in conn.execute("SELECT * FROM jobs WHERE moment_id = ?", (pk,))] == [("clip", "pending")]
    client.post(f"/video/{VID}/stitch", data={"idea": idea})                 # pressing twice: one moment, one job
    assert conn.execute("SELECT COUNT(*) FROM moments WHERE kind = 'compilation'").fetchone()[0] == 1
    page = client.get(f"/video/{VID}").text
    assert "downloading and joining the pieces" in page and page.count("Make this video") == 2
    assert f'href="/moment/{pk}">#1001' not in page                           # not in the scored moments' table
    assert "The pieces are not downloaded yet." in client.get(f"/moment/{pk}").text

    Worker(cfg, deps=Deps(ytdlp=FakeYt(tmp_path / "src.mp4"), claude=FakeRunner())).run(once=True)
    page = client.get(f"/video/{VID}").text
    assert f'href="/moment/{pk}">Open in the editor' in page and "downloading and joining" not in page
    r = client.post(f"/video/{VID}/stitch", data={"idea": idea}, follow_redirects=False)
    assert r.headers["location"] == f"/moment/{pk}"                           # already made: straight to it

    page = client.get(f"/moment/{pk}").text
    for part in ("<video", "compilation · 2 pieces", "2 parts joined", "Setup and payoff 40 minutes apart.",
                 f'href="https://youtu.be/{VID}?t=600"', '<span class="tag">payoff</span>', 'value="the scream"',
                 "at 0:01 in the clip", "at 0:20 in the clip", "Save and download again", "Re-download"):
        assert part in page, part
    assert "Re-cut clip" not in page
    r = client.post(f"/moment/{pk}/cut", data={"start": "0:10", "end": "0:30"}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    assert conn.execute("SELECT end_s FROM moments WHERE id = ?", (pk,)).fetchone()[0] == pytest.approx(40, abs=0.5)
    assert f'href="/moment/{pk}"' in client.get("/clips").text

    # Clip kept counts only the stream's own moments
    conn.execute("UPDATE moments SET status = 'new' WHERE kind != 'compilation'")
    r = client.post(f"/video/{VID}/clip", follow_redirects=False)
    assert "error=" in r.headers["location"]

    # Claude looks again and finds the same idea: it is still the one this clip was made from
    compile_video(cfg, conn, VID, runner=FakeClaude())
    page = client.get(f"/video/{VID}").text
    assert f'href="/moment/{pk}">Open in the editor' in page and page.count("Make this video") == 2
    # …and other ideas (whose rows get the old ids): the clip stays, listed on its own
    other = {"compilations": [{"title": "Chat says bless you", "kind": "recurring", "reason": "r", "rating": 7,
                               "segments": ["0:01:00-0:01:10 | beat | a", "0:02:00-0:02:10 | beat | b"]}] * 1 + [
        {"title": f"Idea {i}", "kind": "other", "reason": "r", "rating": 5,
         "segments": ["0:03:00-0:03:10 | beat | a", f"0:0{i}:30-0:0{i}:40 | beat | b"]} for i in (4, 5)]}
    compile_video(cfg, conn, VID, runner=FakeClaude(other))
    assert idea in [c["id"] for c in conn.execute("SELECT id FROM compilations")]
    page = client.get(f"/video/{VID}").text
    assert "Open in the editor" not in page
    assert "Compilation clips made from earlier ideas" in page and f'href="/moment/{pk}">&#34;I never get scared&#34;' in page
    assert page.count("Make this video") == 3

    r = client.post(f"/video/{VID}/stitch", data={"idea": "junk"}, follow_redirects=False)
    assert "error=" in r.headers["location"] and "gone" in r.headers["location"]


def test_a_failed_download_can_be_tried_again(site, tmp_path):  # noqa: F811
    cfg, conn, client, deps = site
    idea = ideas(cfg, conn)["Weak one"]
    client.post(f"/video/{VID}/stitch", data={"idea": idea})
    pk = conn.execute("SELECT id FROM moments WHERE kind = 'compilation'").fetchone()["id"]
    bad = FakeYt(tmp_path / "src.mp4", fail_first=99)
    out = run_stage(cfg, conn, VID, "clip", moment_id=pk, attempt=4, deps=Deps(ytdlp=bad))
    assert out.status == "error" and "All download attempts failed" in out.message
    assert conn.execute("SELECT COUNT(*) FROM clips WHERE moment_id = ?", (pk,)).fetchone()[0] == 0
    job = conn.execute("SELECT id FROM jobs WHERE moment_id = ?", (pk,)).fetchone()
    jobs.finish(conn, job["id"], ok=False, message=out.message)
    page = client.get(f"/video/{VID}").text
    assert "Try again" in page and "All download attempts failed" in page
    out = run_stage(cfg, conn, VID, "clip", moment_id=pk, deps=Deps(ytdlp=FakeYt(tmp_path / "src.mp4")))
    assert out.status == "done" and clip_ref(conn, pk).duration_s == pytest.approx(40, abs=0.5)


def test_change_the_pieces_and_download_again(site, tmp_path):  # noqa: F811
    cfg, conn, client, deps = site
    idea = ideas(cfg, conn)['"I never get scared"']

    def comp():
        return json.loads(conn.execute("SELECT features_json FROM moments WHERE id = ?", (pk,)).fetchone()[0])["compilation"]

    def post(**data):
        return client.post(f"/moment/{pk}/pieces", data=data, follow_redirects=False).headers["location"]

    def job():
        return [r["status"] for r in conn.execute("SELECT status FROM jobs WHERE moment_id = ? AND stage = 'clip'", (pk,))]

    # before the first download: its page with the pieces to change, nothing queued
    assert client.get(f"/video/{VID}").text.count("Change the pieces first") == 3
    r = client.post(f"/video/{VID}/stitch", data={"idea": idea, "edit": "1"}, follow_redirects=False)
    pk = conn.execute("SELECT id FROM moments WHERE kind = 'compilation'").fetchone()["id"]
    assert r.headers["location"] == f"/moment/{pk}#pieces" and job() == []
    page = client.get(f"/moment/{pk}").text
    for part in (f'action="/moment/{pk}/pieces"', 'name="start" value="10:00"', 'name="end" value="50:20"',
                 'value="the scream"', "Save and download<", "The pieces are not downloaded yet."):
        assert part in page, part
    assert "in the clip" not in page and "confirm(" not in page

    # what is wrong is said, and nothing is saved
    before = comp()["segments"]
    for data, said in (({"start": ["10:00"], "end": ["9:00"]}, "ends before it starts"),
                       ({"start": ["x"], "end": ["1"]}, "Bad time"),
                       ({"start": ["", ""], "end": ["", ""]}, "at least one piece"),
                       ({"start": ["2:00:00"], "end": ["2:00:30"]}, "after the stream ends"),
                       ({"start": ["10:00"], "end": ["10:01"]}, "under 2 seconds"),
                       ({"start": ["0:00"], "end": ["40:00"]}, "add up to 40:00")):
        loc = post(**data)
        assert "error=" in loc and said in unquote(loc) and loc.endswith("#pieces"), (said, loc)
    assert comp()["segments"] == before and "changed" not in comp() and job() == []
    assert client.post("/moment/1/pieces", data={"start": ["1"], "end": ["9"]}).status_code == 400    # a scored moment

    # the setup starts 20 s earlier, a piece is added (no note), an empty row is left out
    loc = post(start=["9:40", "", "30:00", "50:00"], end=["10:15", "", "30:10.4", "50:20"],
               role=["setup", "beat", "nonsense", "payoff"], note=["the claim", "", "", "the scream"])
    assert loc.startswith(f"/moment/{pk}?msg=") and job() == ["pending"]
    assert [(s["start"], s["end"], s["role"], s["note"]) for s in comp()["segments"]] == [
        (580, 615, "setup", "the claim"), (1800, 1811, "beat", ""), (3000, 3020, "payoff", "the scream")]
    assert comp()["changed"] and "downloading and joining the pieces" in client.get(f"/video/{VID}").text

    yt = FakeYt(tmp_path / "src.mp4")
    Worker(cfg, deps=Deps(ytdlp=yt, claude=FakeRunner())).run(once=True)
    assert [a[a.index("--download-sections") + 1] for a in yt.calls] == [
        "*576.00-619.00", "*1796.00-1815.00", "*2996.00-3024.00"]
    assert "changed" not in comp() and len(comp()["parts"]) == 3
    page = client.get(f"/video/{VID}").text                    # still the clip of that idea
    assert f'href="/moment/{pk}">Open in the editor' in page and "You changed its pieces" in page
    page = client.get(f"/moment/{pk}").text
    assert "compilation · 3 pieces" in page and page.count("in the clip</span>") == 3 and "not in the clip yet" not in page

    # an edit is made on this clip; the same pieces again change nothing
    clip = clip_ref(conn, pk)
    conn.execute("INSERT INTO edits (moment_id, segments_json) VALUES (?, '[[0, 5, 0]]')", (pk,))
    conn.execute("INSERT INTO shorts (moment_id, segments_json) VALUES (?, '[[0, 5, 0]]')", (pk,))
    (clip.folder / "edit.mp4").write_bytes(b"x")
    same = dict(start=["9:40", "30:00", "50:00"], end=["10:15", "30:11", "50:20"],
                role=["setup", "beat", "payoff"], note=["the claim", "", "the scream"])
    assert "already" in post(**same) and job() == ["done"]
    assert "confirm(" in client.get(f"/moment/{pk}").text
    run_stage(cfg, conn, VID, "clip", moment_id=pk, deps=Deps(ytdlp=yt))            # Re-download: the edit stays
    assert conn.execute("SELECT COUNT(*) FROM edits WHERE moment_id = ?", (pk,)).fetchone()[0] == 1
    assert (clip.folder / "edit.mp4").is_file()

    # not while the pieces are being downloaded
    conn.execute("UPDATE jobs SET status = 'running' WHERE moment_id = ? AND stage = 'clip'", (pk,))
    assert "being downloaded" in unquote(post(start=["9:40"], end=["10:15"]))
    conn.execute("UPDATE jobs SET status = 'done' WHERE moment_id = ? AND stage = 'clip'", (pk,))

    # the added piece is taken out again: until the download the clip is the old one, then the edit starts over
    from chatwatcher.analyze.service import analysis_matches
    analysed = analysis_matches(conn, pk)
    loc = post(start=["9:40", "50:00"], end=["10:15", "50:20"], role=["setup", "payoff"], note=["the claim", "the scream"])
    assert "starts over" in unquote(loc) and job() == ["pending"]
    assert conn.execute("SELECT COUNT(*) FROM edits WHERE moment_id = ?", (pk,)).fetchone()[0] == 1
    yt.calls.clear()
    out = run_stage(cfg, conn, VID, "clip", moment_id=pk, deps=Deps(ytdlp=yt))
    assert out.status == "done" and len(yt.calls) == 2 and len(comp()["parts"]) == 2 and "changed" not in comp()
    for table in ("edits", "shorts"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE moment_id = ?", (pk,)).fetchone()[0] == 0
    assert not (clip.folder / "edit.mp4").exists() and clip_ref(conn, pk).path.is_file()
    assert analysed and not analysis_matches(conn, pk)                              # Claude looks at the new clip
