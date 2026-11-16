import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import numpy as np
import pytest

from chatwatcher import db, jobs
from chatwatcher.analyze.claude_runner import ClaudeError, ClaudeResult
from chatwatcher.stage import models, mouth, run, script, store, track
from chatwatcher.subs import speakers

from test_web import mid, site  # noqa: F401  (fixture)

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
CUES_JS = Path(__file__).resolve().parents[1] / "src/chatwatcher/web/static/stage/cues.js"


TINY_DICT = {"oh": ["OW1"], "my": ["M", "AY1"], "god": ["G", "AA1", "D"], "i": ["AY1"], "was": ["W", "AA1", "Z"],
             "wrong": ["R", "AO1", "NG"], "the": ["DH", "AH0"], "cloud": ["K", "L", "AW1", "D"]}


@pytest.fixture(autouse=True)
def no_download(monkeypatch):
    """The pronunciation dictionary is downloaded once in real use; tests use a small one."""
    monkeypatch.setattr(mouth, "cmudict", lambda models_dir: TINY_DICT)


# ── the voice track ──────────────────────────────────────────────────────────

def test_emphasis_from_the_subtitles():
    st = {}
    first = mouth.tokens('like, "Oh my god, the lights are so pretty."', st)
    assert first[0] == ("like", 0.0) and dict(first)["oh"] == 0.25 and dict(first)["pretty"] == 0.15
    shout = mouth.tokens('"Oh my god, did the entire *continent* just lose power?!"', st)
    assert dict(shout)["continent"] == pytest.approx(0.57) and dict(shout)["just"] == pytest.approx(0.27)
    st = {}
    mouth.tokens('"Oh my god, I\'m gonna take my phone out and', st)      # a quote open over two lines
    assert dict(mouth.tokens("try and capture the moment", st))["try"] == 0.15
    assert dict(mouth.tokens('we\'re going through a power outage!"', st))["outage"] == pytest.approx(0.27)
    assert dict(mouth.tokens("after that", st))["after"] == 0.0
    assert dict(mouth.tokens("WHAT is that", {}))["what"] == pytest.approx(0.4)


def test_shapes_from_how_words_sound():
    my = mouth.visemes(["M", "AY1"], [("m", 1.0), ("y", 1.1)], 1.0, 1.3)
    assert my[0] == [1.0, "a", 0.0]                               # lips shut on m
    assert my[1][1:] == ["a", 1.0] and my[2][1] == "i"             # then a → i
    cloud = mouth.visemes(["K", "L", "AW1", "D"], [(c, 4 + i / 20) for i, c in enumerate("cloud")], 4.0, 4.4)
    assert [c[1] for c in cloud if c[2] >= 0.7] == ["a", "u"] and cloud[-1] == [4.4, "u", 0.0]
    assert mouth.visemes(["DH", "AH0"], [("t", 0), ("h", 0.05), ("e", 0.1)], 0, 0.2)[1][2] == pytest.approx(0.45)
    loud = mouth.visemes(["G", "AA1", "D"], [("g", 2.0), ("o", 2.05), ("d", 2.3)], 2.0, 2.4, gain=1.5)
    assert max(c[2] for c in loud) == 1.5 and loud[1][0] == 2.05   # the vowel sound on the vowel letter


def test_subtitles_win_where_whisper_misheard():
    lines = [{"t0": 44.79, "t1": 47.95, "text": "I thought i was seeing *power outage* from above"},
             {"t0": 47.99, "t1": 49.34, "text": "I was wrong..."}]
    whisper = [(46.82, 47.04, "from"), (47.04, 47.9, "above"), (48.02, 48.1, "i"), (48.06, 48.28, "was"),
               (48.26, 48.58, "wrong"), (48.54, 48.82, "capture")]
    out, rewritten = track.said(whisper, lines, True)
    assert rewritten == 2 and "capture" not in [w["text"] for w in out]
    assert [w["text"] for w in out][-3:] == ["i", "was", "wrong"] and out[-1]["a"] == 47.99
    assert {w["text"]: w["bonus"] for w in out}["outage"] == 0.3
    same, n = track.said(whisper[-4:-1], [lines[1]], True)                # Whisper heard it right: its times
    assert n == 0 and same[0]["a"] == 48.02
    jp, n = track.said(whisper, [{"t0": 44.0, "t1": 49.0, "text": "わたしがまちがってた"}], True)
    assert n == 0 and len(jp) == len(whisper)                             # not words the aligner can read
    other, n = track.said(whisper[2:], [{"t0": 47.9, "t1": 49.0, "text": "my mistake there friends"}], True)
    assert n == 0                                                         # a translation, not what she said


def test_words_it_cannot_hear_fill_the_gap():
    words = [{"a": 0, "b": 3, "text": "one"}, {"a": 0, "b": 3, "text": "two"}, {"a": 0, "b": 3, "text": "three"}]
    timed = [[("o", 0.5), ("n", 0.6), ("e", 0.7)], None, [("t", 2.0), ("h", 2.1)]]
    out = track.fill_gaps(words, timed)
    assert [ok for _, ok in out] == [True, False, True]
    mid = out[1][0]
    assert 0.7 < mid[0][1] < mid[-1][1] < 2.0


def test_dictionary_reads_variants_and_survives_no_network(tmp_path, monkeypatch):
    monkeypatch.undo()
    (tmp_path / mouth.CMU_FILE).write_text("read R IY1 D\nread(2) R EH1 D\nlive L IH1 V # verb\n", encoding="utf-8")
    d = mouth.cmudict(tmp_path)
    assert d["read"] == ["R", "IY1", "D"] and d["live"] == ["L", "IH1", "V"]

    def offline(*a, **k):
        raise OSError("no network")
    monkeypatch.setattr(mouth.urllib.request, "urlopen", offline)
    assert mouth.cmudict(tmp_path / "empty") == {}


def test_mouth_shapes_from_letters():
    chars = [(c, round(i * 0.1, 2)) for i, c in enumerate("moment")]
    got = track.shapes(chars, 0.7)
    assert got[0] == [0.0, "a", 0.0]                       # m: lips shut
    assert [0.1, "o", 1.0] in got and [0.3, "e", 1.0] in got
    assert got[-1] == [0.7, "e", 0.0]                      # closes at the end of the word
    late = track.shapes([(c, i * 0.1) for i, c in enumerate("late")], 0.5)
    assert all(v != "e" for _, v, k in late if k == 1.0)   # silent e
    assert [0.0, "u", 0.7] in track.shapes([("w", 0.0), ("e", 0.1)], 0.2)
    assert track.shapes([("s", 0.0), ("e", 0.1), ("e", 0.2)], 0.3)[1] == [0.1, "i", 1.0]   # "ee" is one i


def test_spans_follow_the_cut():
    cut = [[10.0, 20.0, 0.0], [30.0, 35.0, 10.0]]
    assert track.to_cut(12, 14, cut) == [(2.0, 4.0)]
    assert track.to_cut(18, 32, cut) == [(8.0, 10.0), (10.0, 12.0)]
    assert track.to_cut(21, 29, cut) == []                  # cut out


def test_word_offset_finds_the_late_sound():
    env = np.zeros(track.RATE * 20)
    words = [[1.0 + k * 2, 1.6 + k * 2, "w"] for k in range(8)]
    for t0, t1, _ in words:
        env[int((t0 + 3.0) * track.RATE): int((t1 + 3.0) * track.RATE)] = 1.0
    assert track.word_offset(env, words, [3.0]) == pytest.approx(3.0, abs=0.03)
    assert track.word_offset(env, words, []) != pytest.approx(3.0, abs=0.5)    # not guessed, not found
    aligned = np.zeros_like(env)
    for t0, t1, _ in words:
        aligned[int(t0 * track.RATE): int(t1 * track.RATE)] = 1.0
    assert track.word_offset(aligned, words, [3.0]) == pytest.approx(0.0, abs=0.03)


@needs_ffmpeg
def test_track_is_made_once_per_cut(site):
    cfg, conn, client, deps = site
    pk = mid(conn)
    st = store.setup(cfg, conn, pk, shape="long")
    tr = track.ensure(cfg, conn, pk, st)
    clip = store.cr.clip_ref(conn, pk)
    assert tr["duration"] == pytest.approx(store.duration(st["segments"]), abs=0.01)
    assert len(tr["env"]) == pytest.approx(tr["duration"] * track.RATE, abs=3)
    assert (clip.folder / track.AUDIO_FILE).stat().st_size > 1000
    assert track.ensure(cfg, conn, pk, st)["key"] == tr["key"]       # same cut: the same track
    conn.execute("UPDATE stages SET segments_json = ? WHERE moment_id = ?",
                 (json.dumps([[st["segments"][0][0], st["segments"][0][0] + 3]]), pk))
    short = track.ensure(cfg, conn, pk, store.get(conn, pk))
    assert short["key"] != tr["key"] and short["duration"] == pytest.approx(3, abs=0.01)


@needs_ffmpeg
def test_sound_is_read_on_the_pictures_clock(tmp_path):
    from test_clip import make_video
    src = tmp_path / "late.mp4"                             # the picture starts 0 s, the sound at 1.5 s
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=4:size=160x120:rate=30",
                    "-itsoffset", "1.5", "-f", "lavfi", "-i", "sine=frequency=440:duration=2.5",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True)
    assert track.audio_start(src) == pytest.approx(1.5, abs=0.05)
    pcm = track._pcm(src, track.SR, 1)
    assert len(pcm) / track.SR == pytest.approx(4.0, abs=0.1)
    assert np.abs(pcm[: int(1.3 * track.SR)]).max() < 300           # silence where there was no sound
    assert np.abs(pcm[int(1.7 * track.SR): int(2.5 * track.SR)]).max() > 3000
    assert make_video                                                # (the helper the fixture uses)


# ── the scene ────────────────────────────────────────────────────────────────

def test_validate_keeps_what_the_set_can_do():
    raw = {"shots": [{"t": 2, "shot": "pov", "yaw": 200, "push": 3}, {"t": 5, "shot": "flying"}],
           "poses": [{"t": 99, "pose": "shock", "ease": 0}], "faces": "nope",
           "lanes": {"cloud": [{"t": 1, "v": 7}], "rain": [{"t": 1, "v": 1}]}}
    s, warnings = script.validate(raw, 10.0, "cabin")
    assert [c["shot"] for c in s["shots"]] == ["medium", "pov"]     # a first shot at 0, then hers
    assert s["shots"][1]["yaw"] == 75 and s["shots"][1]["push"] == 0.4
    assert s["poses"][-1] == {"t": 10.0, "pose": "shock", "ease": 0.05, "by": "you"}
    assert s["faces"] == [{"t": 0.0, "face": "neutral", "by": "you", "ease": 0.3}]
    assert s["lanes"]["cloud"][-1]["v"] == 1.0 and "rain" not in s["lanes"]
    assert any("flying" in w for w in warnings) and any("rain" in w for w in warnings)
    room, w2 = script.validate(raw, 10.0, "room")
    assert [c["shot"] for c in room["shots"]] == ["medium"] and room["lanes"] == {}    # no pov, no cloud


def test_actions_are_checked():
    s, warnings = script.validate({"actions": [{"t": 3, "action": "take", "size": 9}, {"t": 1, "action": "fly"},
                                               {"t": 0.5, "action": "nod"}]}, 10.0, "room", by="claude")
    assert s["actions"] == [{"t": 0.5, "action": "nod", "size": 1.0, "by": "claude"},
                            {"t": 3.0, "action": "take", "size": 1.5, "by": "claude"}]
    assert any("fly" in w for w in warnings)
    assert "take" in script.vocab()["actions"]
    sch = run.schema("room")
    assert "actions" in sch["required"] and "take" in json.dumps(sch["properties"]["actions"])
    assert run.yours({"actions": [{"t": 0.0, "action": "take", "by": "you"}]})[0]["lane"] == "actions"


def test_a_gesture_is_never_off_screen():
    shots = [{"t": 0, "shot": "medium"}, {"t": 38.4, "shot": "pov"}, {"t": 39.4, "shot": "face"}]
    s, _ = script.validate({"shots": shots, "actions": [{"t": 39.1, "action": "take"}]}, 49.3, "cabin")
    assert [(c["t"], c["shot"]) for c in s["shots"]] == [(0, "medium"), (38.4, "pov"), (39.1, "medium"), (39.4, "face")]
    s, _ = script.validate({"shots": [{"t": 0, "shot": "medium"}, {"t": 10, "shot": "pov"}],
                            "actions": [{"t": 9.9, "action": "recoil"}]}, 49.3, "cabin")
    assert [(c["t"], c["shot"]) for c in s["shots"]] == [(0, "medium"), (10.0, "close")]
    s, _ = script.validate({"shots": [{"t": 0, "shot": "screen"}], "actions": [{"t": 2, "action": "nod"}]}, 9, "room")
    assert [c["shot"] for c in s["shots"]] == ["screen", "close"]


def test_your_cues_survive_a_fresh_write():
    old = {"shots": [{"t": 0, "shot": "medium", "by": "claude"}, {"t": 4.0, "shot": "face", "by": "you", "yaw": 0, "push": 0.1}],
           "poses": [], "faces": [{"t": 6.0, "face": "shock", "by": "you", "ease": 0.2}], "lanes": {}}
    keep = run.yours(old)
    assert [c["lane"] for c in keep] == ["shots", "faces"]
    fresh = {"shots": [{"t": 0, "shot": "wide"}, {"t": 4.2, "shot": "close"}, {"t": 8, "shot": "medium"}],
             "faces": [{"t": 0, "face": "smile"}], "poses": [], "lanes": {}}
    got = run.keep_yours(fresh, keep)
    assert [(c["t"], c["shot"]) for c in got["shots"]] == [(0, "wide"), (4.0, "face"), (8, "medium")]
    assert got["faces"][-1]["face"] == "shock"


# ── the model library ────────────────────────────────────────────────────────

class _SJIS(zipfile.ZipInfo):
    """A name stored as Windows zips Japanese names: Shift-JIS bytes, no UTF-8 flag."""

    def _encodeFilenameFlags(self):
        return self.filename.encode("cp437"), self.flag_bits & ~0x800


def _zip(path: Path, names: dict[str, bytes], sjis: bool = False) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in names.items():
            z.writestr(_SJIS(name.encode("cp932").decode("cp437")) if sjis else zipfile.ZipInfo(name), data)
    return path


def test_import_names_and_picks_the_model(site, tmp_path):
    cfg, conn, client, deps = site
    for name, jp in (("Natsuiro Matsuri", "夏色まつり"), ("La+ Darknesss", "ラプラス・ダークネス"), ("AZKi", "AZKi")):
        speakers.save_vtuber(conn, name, "FF8800", japanese_name=jp)
    jp = _zip(tmp_path / "夏色まつり公式mmd_ver1.0.zip",
              {"夏色まつり公式mmd_ver1.0/夏色まつり.pmx": b"x" * 10, "夏色まつり公式mmd_ver1.0/tex/顔.png": b"p",
               "../evil.txt": b"no"}, sjis=True)
    p = models.import_zip(cfg, jp, conn=conn)
    assert (p["slug"], p["name"], p["vtuber"]) == ("natsuiro-matsuri", "Natsuiro Matsuri", "Natsuiro Matsuri")
    assert p["pmx"] == "夏色まつり公式mmd_ver1.0/夏色まつり.pmx"
    assert not (models.root(cfg).parent / "evil.txt").exists() and not (models.root(cfg) / "evil.txt").exists()
    la = models.import_zip(cfg, _zip(tmp_path / "LaplusDarknesss.zip", {
        "PMX/La+Darknesss_short.pmx": b"x" * 50, "PMX/La+Darknesss.pmx": b"x" * 10}), conn=conn)
    assert la["vtuber"] == "La+ Darknesss" and la["pmx"] == "PMX/La+Darknesss.pmx"
    a1 = models.import_zip(cfg, _zip(tmp_path / "AZKi_4th.zip", {"AZKi_4th.pmx": b"x"}), conn=conn)
    a2 = models.import_zip(cfg, _zip(tmp_path / "AZKi_5th.zip", {"AZKi_5th.pmx": b"x"}), conn=conn)
    assert (a1["slug"], a2["slug"]) == ("azki", "azki-2") and a2["name"] == "AZKi 5th"
    again = models.import_zip(cfg, tmp_path / "AZKi_4th.zip", conn=conn)              # the same zip: same folder
    assert again["slug"] == "azki" and len(models.list_models(cfg)) == 4
    with pytest.raises(models.ModelError):
        models.import_zip(cfg, _zip(tmp_path / "empty.zip", {"readme.txt": b"hi"}), conn=conn)


def test_import_a_folder_once(site, tmp_path):
    cfg, conn, client, deps = site
    src = tmp_path / "downloads"
    src.mkdir()
    _zip(src / "One.zip", {"One.pmx": b"x"})
    _zip(src / "Two.zip", {"Two.pmx": b"x"})
    _zip(src / "Broken.zip", {"readme.txt": b"no model"})
    done, problems = models.import_folder(cfg, src)
    assert sorted(done) == ["One", "Two"] and len(problems) == 1 and "Broken.zip" in problems[0]
    assert models.import_folder(cfg, src)[0] == []                    # nothing new the second time
    r = client.post("/models/import-folder", data={"folder": str(src)}, follow_redirects=False)
    assert r.status_code == 303 and "Broken.zip" in r.headers["location"]


def test_model_files_stay_inside_the_folder(site, tmp_path):
    cfg, conn, client, deps = site
    models.import_zip(cfg, _zip(tmp_path / "Someone.zip", {"Someone/PMX/Someone.pmx": b"x", "Someone/Tex/Face.PNG": b"p"}))
    assert models.file(cfg, "someone", "Someone/tex/face.png").name == "Face.PNG"     # any case, as Windows models need
    assert models.file(cfg, "someone", "Someone/PMX/../Tex/Face.PNG") is None
    assert models.file(cfg, "someone", "../../chatwatcher.db") is None
    assert models.file(cfg, "../data", "chatwatcher.db") is None
    assert client.get("/media/mmd/someone/Someone/tex/face.png").status_code == 200
    assert client.get("/media/mmd/someone/..%2F..%2Fchatwatcher.db").status_code == 404
    p = models.save(cfg, "someone", {"seated": {"pin": "lower", "keep": ["_Nui$"], "fold": {"Tail_01": [55, 0, 0]}},
                                     "checked": 1, "source": "hacked"})
    assert p["checked"] is True and p["seated"]["fold"] == {"Tail_01": [55.0, 0.0, 0.0]} and p["source"] == "Someone.zip"
    with pytest.raises(models.ModelError):
        models.save(cfg, "someone", {"seated": {"keep": ["("]}})


# ── Claude writes, you direct ────────────────────────────────────────────────

class FakeStage:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def run(self, prompt, schema, *, system, cwd, effort=None, images=None, session=None, resume=False, model=None, tools=None):
        self.calls.append({"prompt": prompt, "schema": schema, "session": session, "resume": resume, "images": images})
        step = self.answers.pop(0)
        if isinstance(step, ClaudeError):
            raise step
        return ClaudeResult(step, "claude-test", 1, {"usage": {"output_tokens": 5}})


SCENE = {"apply": True, "note": "The idea.", "shots": [{"t": 0, "shot": "medium", "yaw": 0, "push": 0.05},
                                                      {"t": 1.5, "shot": "pov", "push": 0.1}],
         "poses": [{"t": 0, "pose": "rest"}, {"t": 1.0, "pose": "look", "ease": 0.4}],
         "faces": [{"t": 0, "face": "smile"}], "lanes": {"cloud": [{"t": 0, "v": -1}, {"t": 2, "v": 0, "ease": 0.5}]}}


@needs_ffmpeg
def test_write_then_direct(site):
    cfg, conn, client, deps = site
    pk = mid(conn)
    store.setup(cfg, conn, pk, set_name="cabin", shape="long")
    follow = dict(SCENE, note="Held it.", faces=[{"t": 0, "face": "smile"}, {"t": 2.0, "face": "shock", "by": "you"}])
    fake = FakeStage([SCENE, follow, {"apply": False, "note": "It's a cloud.", "shots": [], "poses": [], "faces": []}])
    t = run.direct(cfg, conn, pk, new=True, runner=fake)
    assert t.fresh and t.scene["shots"][1]["shot"] == "pov" and t.scene["shots"][1]["by"] == "claude"
    assert t.scene["lanes"]["cloud"][1]["v"] == 0 and t.reply.startswith("The idea.")
    first = fake.calls[0]
    assert not first["resume"] and "pov" in json.dumps(first["schema"]) and "cloud" in first["prompt"]
    t2 = run.direct(cfg, conn, pk, "make her shocked at 2s", runner=fake)
    assert not t2.fresh and fake.calls[1]["resume"] and fake.calls[1]["session"] == first["session"]
    assert store.get(conn, pk)["scene"]["faces"][-1] == {"t": 2.0, "face": "shock", "by": "you", "ease": 0.3}
    t3 = run.direct(cfg, conn, pk, "why the cloud?", runner=fake)
    assert t3.scene is None and t3.reply == "It's a cloud." and store.get(conn, pk)["scene"]["note"] == "The idea."
    assert [m["role"] for m in t3.messages] == ["user", "assistant"] * 3
    again = FakeStage([SCENE])                                     # writing again keeps your shocked face
    t4 = run.direct(cfg, conn, pk, new=True, runner=again)
    assert {"t": 2.0, "face": "shock", "by": "you", "ease": 0.3} in t4.scene["faces"]
    over = run.direct(cfg, conn, pk, new=True, start_over=True, runner=FakeStage([SCENE]))
    assert all(c["by"] == "claude" for c in over.scene["faces"])
    with pytest.raises(run.StageRunError):
        run.direct(cfg, conn, pk, "", runner=FakeStage([]))       # a follow-up needs words


@needs_ffmpeg
def test_lost_session_starts_again(site):
    cfg, conn, client, deps = site
    pk = mid(conn)
    store.setup(cfg, conn, pk, set_name="room")
    run.direct(cfg, conn, pk, new=True, runner=FakeStage([dict(SCENE, lanes={})]))
    fake = FakeStage([ClaudeError("gone", kind="no_session"), dict(SCENE, lanes={})])
    t = run.direct(cfg, conn, pk, "wider", runner=fake)
    assert t.fresh and "Earlier in this conversation" in fake.calls[1]["prompt"] and "couldn't be resumed" in t.reply


# ── the page ─────────────────────────────────────────────────────────────────

@needs_ffmpeg
def test_stage_pages_and_api(site, tmp_path):
    cfg, conn, client, deps = site
    pk = mid(conn)
    assert client.get(f"/moment/{pk}/3d").status_code == 200
    d = client.get(f"/api/moment/{pk}/stage").json()
    assert d["ok"] and d["stage"] is None and d["models"] == [] and "cabin" in d["vocab"]["sets"]
    models.import_zip(cfg, _zip(tmp_path / "Someone.zip", {"Someone.pmx": b"x"}))
    r = client.post(f"/moment/{pk}/stage/setup", json={"model": "someone", "set": "room", "shape": "short"}).json()
    assert r["ok"] and r["stage"]["model"] == "someone" and r["track"]["ready"]
    assert r["model_url"] == "/media/mmd/someone/Someone.pmx"
    assert client.get(r["track"]["url"]).json()["duration"] > 0 and client.get(r["track"]["audio"]).status_code == 200
    assert client.post(f"/moment/{pk}/stage/setup", json={"model": "nobody"}).status_code == 404
    bad = client.post(f"/moment/{pk}/stage/scene", json={"scene": {"shots": [{"t": 1, "shot": "pov"}]}}).json()
    assert bad["ok"] and bad["warnings"] and [c["shot"] for c in bad["scene"]["shots"]] == ["medium"]
    deps.claude = FakeStage([dict(SCENE, lanes={})])
    w = client.post(f"/moment/{pk}/stage/direct", json={"new": True}).json()
    assert w["ok"] and w["applied"] and w["messages"][-1]["role"] == "assistant"
    q = client.post(f"/moment/{pk}/stage/render").json()
    assert q["ok"] and q["render"]["job"]["status"] == "pending"
    job = conn.execute("SELECT stage, moment_id FROM jobs WHERE stage = '3d'").fetchone()
    assert job["moment_id"] == pk
    assert client.get(f"/media/stage/{pk}/screen/1").status_code == 404
    assert client.get(f"/media/stage/{pk}/nothing").status_code == 404
    assert client.get("/models").status_code == 200 and client.get("/models/someone").status_code == 200
    assert client.get("/models/nobody").status_code == 404
    assert client.post("/models/someone/profile", json={"changes": {"notes": "ok"}}).json()["profile"]["notes"] == "ok"


def test_schema_has_the_stage(tmp_path):
    conn = db.open_db(tmp_path / "x.db")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(stages)")}
    assert {"moment_id", "model", "set_name", "shape", "segments_json", "scene_json"} <= cols
    assert "3d" in jobs.MOMENT_STAGES


# ── cues.js, in node ─────────────────────────────────────────────────────────

def test_cues_js():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script_ = f"""
      import * as c from {json.dumps(CUES_JS.as_uri())};
      const shots = [{{t: 0, shot: 'medium'}}, {{t: 2, shot: 'face', ease: 1}}, {{t: 5, shot: 'wide'}}];
      const lane = [{{t: 0, v: -1}}, {{t: 4, v: 1, ease: 2}}];
      const track = {{rate: 10, env: [0, 0.5, 1, 1], mouth: [[1, 'a', 1], [1.5, 'o', 0]], laughs: [[2, 3]]}};
      const segs = [[10, 20, 0], [30, 35, 10]];
      const keys = [[0, {{}}], [0.2, {{a: [10, 0, 0], arm: {{up: 40}}}}], [0.3, {{snap: 1, a: [-20, 0, 0], center: [0, 1, 0]}}], [1, {{}}]];
      console.log(JSON.stringify({{
        cue: c.cueAt(shots, 3).map((x) => x && (x.shot ?? x)), before: c.cueAt(shots, 1)[1].shot,
        laneMid: c.laneAt(lane, 5, -1), laneStart: c.laneAt(lane, 1, -1), laneEnd: c.laneAt(lane, 9, -1),
        shot: c.shotAt(shots, 3.5, 10), env: c.envAt(track, 0.2, 2),
        mouth: c.mouthAt(track, 1.6), none: c.mouthAt(track, 0.5)[0],
        span: [c.spanAt(track.laughs, 2.5), c.spanAt(track.laughs, 4)],
        blinks: c.blinkTimes(20).join() === c.blinkTimes(20).join(), clip: [c.clipTime(segs, 3), c.clipTime(segs, 12)],
        act: [c.actionPose(keys, 0.1), c.actionPose(keys, 0.2), c.actionPose(keys, 0.25), c.actionPose(keys, 1)],
        env2: [c.envelopeAt([[0, 0], [1, 1], [2, 0]], 0.5), c.envelopeAt([[0, 0], [1, 1]], 5)],
        ease: [c.backOut(0), c.backOut(1), Math.max(...[0.5, 0.6, 0.7, 0.8].map((x) => c.backOut(x))) > 1, c.snap(0.5) > 0.9],
      }}));"""
    got = json.loads(subprocess.run([node, "--input-type=module", "-e", script_], capture_output=True, text=True,
                                    check=True).stdout)
    assert got["cue"] == ["medium", "face", 1] and got["before"] == "medium"
    assert got["laneStart"] == -1 and got["laneEnd"] == 1 and -1 < got["laneMid"] < 1
    assert got["shot"][0]["shot"] == "face" and got["shot"][1] == pytest.approx(0.5)
    assert got["env"] == pytest.approx(0.75)
    assert got["mouth"][0] == [1.5, "o", 0] and got["mouth"][1] == [1, "a", 1] and got["none"] == [0, "a", 0]
    assert got["span"] == [1, 0] and got["blinks"] is True and got["clip"] == [13, 32]
    early, at, hit, done = got["act"]
    assert early["a"] == pytest.approx([5, 0, 0]) and early["arm"]["up"] == pytest.approx(20)   # half way to the first key
    assert at["a"] == [10, 0, 0] and at["arm"]["up"] == 40
    assert hit["a"][0] < -10 and hit["center"][1] > 0.9 and hit["arm"]["up"] < 10             # snaps into the hit
    assert done["a"] == [0, 0, 0] and "snap" not in hit
    assert got["env2"] == [0.5, 1] and got["ease"] == [0, 1, True, True]
