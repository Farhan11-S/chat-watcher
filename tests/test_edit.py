import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from chatwatcher import db, jobs
from chatwatcher.config import EditConfig
from chatwatcher.edit.audio import RATE, analyse, from_samples, snap
from chatwatcher.edit.render import render
from chatwatcher.edit.service import MAX_SEGMENTS, EditError, clean_segments
from chatwatcher.worker import Worker

from test_clip import make_video
from test_web import mid, site  # noqa: F401  (fixture)

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def speech_like(gaps: list[tuple[float, float]], duration: float = 10.0, sr: int = 8000) -> np.ndarray:
    """Loud noise with quiet gaps (plus faint background music) at the given times."""
    rng = np.random.default_rng(1)
    t = np.arange(int(duration * sr)) / sr
    x = 0.5 * rng.standard_normal(len(t)) * (0.85 + 0.15 * np.sin(2 * np.pi * 3 * t))
    for a, b in gaps:
        x[int(a * sr):int(b * sr)] = 0.0
    return (x + 0.01 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


# ── audio ────────────────────────────────────────────────────────────────────

def test_pauses_found_in_gaps_and_snap_moves_cuts_into_them():
    wf = from_samples(speech_like([(2.0, 2.4), (5.0, 6.0)]))
    assert len(wf.peaks) == 10 * RATE and max(wf.peaks) == 100
    times = [t for t, _ in wf.pauses]
    near = lambda lo, hi: [t for t in times if lo <= t <= hi]
    assert len(near(1.9, 2.5)) == 1 and len(near(4.9, 6.1)) == 1      # one point per gap
    assert all(d >= 6 for _, d in wf.pauses)
    assert 5.3 < near(4.9, 6.1)[0] < 5.7                               # the middle of a long gap
    assert abs(snap(2.7, wf.pauses, 1.0) - near(1.9, 2.5)[0]) < 1e-9   # a cut mid-"word" moves to the gap
    assert snap(8.5, wf.pauses, 1.0) == 8.5                            # nothing near: stays put


@needs_ffmpeg
def test_analyse_reads_a_file_and_caches(tmp_path):
    src = make_video(tmp_path / "a.mp4", 4)
    cache = tmp_path / "waveform.json"
    wf = analyse(src, cache)
    assert 3.9 < wf.duration_s < 4.2 and len(wf.peaks) >= 195
    stamp = cache.stat().st_mtime_ns
    assert analyse(src, cache).peaks == wf.peaks and cache.stat().st_mtime_ns == stamp


def test_clean_segments():
    assert clean_segments([[1, 5], [8.12345, 9, -6]], 10) == [[1.0, 5.0, 0.0], [8.123, 9.0, -6.0]]
    assert clean_segments([[-3, 4, -99]], 10) == [[0.0, 4.0, -30.0]]        # clamped
    for bad in (None, [], [[1]], [["a", 2]], [[5, 5.1]], [[0.2, 0.9]], [[1, float("nan")]], [[0, 1]] * (MAX_SEGMENTS + 1)):
        with pytest.raises(EditError) as e:
            clean_segments(bad, 10)
        assert e.value.kind == "invalid"


# ── render ───────────────────────────────────────────────────────────────────

@needs_ffmpeg
def test_render_is_frame_exact_and_reordered(tmp_path):
    src = make_video(tmp_path / "src.mp4", 12)
    out = tmp_path / "edit.mp4"
    seen = []
    r = render(EditConfig(), src, [[6.0, 9.0, 0], [1.0, 2.5, -10]], out, fps=30, on_progress=seen.append)
    assert r.path == out and out.is_file() and not list(tmp_path.glob(".render_*"))
    assert abs(r.duration_s - 4.5) < 0.05
    streams = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,nb_frames,duration", "-of", "json", str(out)],
        capture_output=True, text=True).stdout)["streams"]
    v = next(s for s in streams if s["codec_type"] == "video")
    a = next(s for s in streams if s["codec_type"] == "audio")
    assert int(v["nb_frames"]) == 135                       # 4.5 s × 30 fps, no frame lost or doubled
    assert abs(float(a["duration"]) - float(v["duration"])) < 0.03
    assert seen == sorted(seen) and seen[-1] == 1.0


@needs_ffmpeg
def test_render_without_loudness_or_fades(tmp_path):
    src = make_video(tmp_path / "src.mp4", 5)
    r = render(EditConfig(loudness_lufs=0, fade_ms=0), src, [[0.5, 3.0, 0]], tmp_path / "e.mp4", fps=30)
    assert abs(r.duration_s - 2.5) < 0.05


# ── editor pages and the render job ──────────────────────────────────────────

@needs_ffmpeg
def test_editor_save_render_and_media(site):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    d = client.get(f"/api/moment/{m}/edit").json()
    assert d["ok"] and not d["saved"] and d["source"] == "whole"             # ✨ AI edit makes the real cut
    assert len(d["segments"]) == 1 and d["segments"][0][0] == 0 and d["segments"][0][1] == pytest.approx(d["duration"], abs=0.05)
    assert "presets" not in d
    assert d["peaks"] and d["rate"] == 50 and d["render"]["state"] == "none"
    assert d["beats"] and "lines" in d

    zero = conn.execute("SELECT zero_s FROM clips WHERE moment_id = ?", (m,)).fetchone()["zero_s"]
    r = client.post(f"/moment/{m}/edit", json={"segments": [[4, 8, -6], [1, 3]], "source": "long"}).json()
    assert r["ok"] and r["segments"] == [[4.0, 8.0, -6.0], [1.0, 3.0, 0.0]]
    row = conn.execute("SELECT * FROM edits WHERE moment_id = ?", (m,)).fetchone()
    assert json.loads(row["segments_json"])[0][:2] == [round(4 + zero, 3), round(8 + zero, 3)]   # stream time
    d = client.get(f"/api/moment/{m}/edit").json()
    assert d["saved"] and d["segments"] == [[4.0, 8.0, -6.0], [1.0, 3.0, 0.0]]

    bad = client.post(f"/moment/{m}/edit", json={"segments": [[3, 3.05]]})
    assert bad.status_code == 400 and "shorter" in bad.json()["error"]

    r = client.post(f"/moment/{m}/render", json={"segments": [[4, 8, -6], [1, 3, 0]]}).json()
    assert r["ok"] and r["render"]["state"] == "queued"
    # Renders jump the queue: you're waiting for them.
    v = conn.execute("SELECT video_id FROM moments WHERE id = ?", (m,)).fetchone()["video_id"]
    jobs.enqueue(conn, v, "analyze")
    assert jobs.claim(conn)["stage"] == "render"
    conn.execute("UPDATE jobs SET status = 'pending' WHERE status = 'running'")
    conn.execute("DELETE FROM jobs WHERE stage = 'analyze' AND moment_id IS NULL")

    Worker(cfg, deps=deps).run(once=True)
    st = client.get(f"/api/moment/{m}/render").json()["render"]
    assert st["state"] == "done" and abs(st["length"] - 6.0) < 0.1 and Path(st["path"]).name == "edit.mp4"
    media = client.get(st["url"])
    assert media.status_code == 200 and media.headers["content-type"] == "video/mp4"
    assert "attachment" in client.get(f"/media/edit/{m}?dl=1").headers["content-disposition"]

    client.post(f"/moment/{m}/edit", json={"segments": [[4, 9]]})
    assert client.get(f"/api/moment/{m}/render").json()["render"]["state"] == "stale"
    page = client.get(f"/moment/{m}").text
    assert 'data-editor="' in page and '<script type="module" src="/static/editor/main.js?v=' in page
    # The modules import each other by plain paths (no ?v=), so the browser must ask each time.
    assert client.get("/static/editor/state.js").headers["cache-control"] == "no-cache"
    assert "cache-control" not in client.get("/static/app.js").headers

    # "Delete videos only" removes the rendered edit too.
    out = Path(conn.execute("SELECT output_dir FROM videos").fetchone()["output_dir"])
    assert list(out.glob("*/edit.mp4"))
    client.post("/storage/delete", data={"key": f"{out.parent.name}/{out.name}", "mode": "videos"})
    assert not list(out.glob("*/edit.mp4"))
    assert client.get(f"/api/moment/{m}/edit").status_code == 404
    assert client.get(f"/media/edit/{m}").status_code == 404


def test_render_needs_a_moment_and_never_joins_the_pipeline(tmp_path):
    conn = db.open_db(tmp_path / "t.db")
    v = conn.execute("INSERT INTO videos (video_id) VALUES ('abcdefghijk')").lastrowid
    with pytest.raises(ValueError):
        jobs.enqueue(conn, v, "render")
    assert "render" not in jobs.STAGES and jobs.next_stage("upload") is None


def test_migration_keeps_jobs(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    for version in range(1, 8):
        conn.executescript(f"BEGIN; {db.MIGRATIONS[version - 1]}; PRAGMA user_version = {version}; COMMIT;")
    v = conn.execute("INSERT INTO videos (video_id) VALUES ('abcdefghijk')").lastrowid
    conn.execute("INSERT INTO jobs (video_id, stage, status, message, params_json) VALUES (?, 'clip', 'error', 'x', '{}')", (v,))
    assert db.migrate(conn) == list(range(8, db.SCHEMA_VERSION + 1))
    j = conn.execute("SELECT * FROM jobs").fetchone()
    assert (j["stage"], j["status"], j["message"], j["params_json"]) == ("clip", "error", "x", "{}")
    m = conn.execute("INSERT INTO moments (video_id, rank, score, start_s, end_s, peak_s) VALUES (?,1,1,0,30,10)",
                     (v,)).lastrowid
    assert jobs.enqueue(conn, v, "render", moment_pk=m)
    conn.close()


# ── sound effects ────────────────────────────────────────────────────────────

from chatwatcher.edit import sounds as sfx  # noqa: E402
from chatwatcher.edit.render import SoundCue, place  # noqa: E402
from chatwatcher.edit.service import clean_effects  # noqa: E402


def test_hits_mark_sudden_loud_moments_not_every_word():
    sr, rng = 8000, np.random.default_rng(2)
    t = np.arange(10 * sr) / sr
    talk = 0.1 * rng.standard_normal(len(t)) * (np.sin(2 * np.pi * 2 * t) > -0.3)   # words with short gaps
    talk[int(6.0 * sr):int(7.0 * sr)] = 0.9 * rng.standard_normal(sr)                # a scream at 6.0 s
    wf = from_samples(talk.astype(np.float32))
    strong = [h for h in wf.hits if h[1] >= 12]
    assert len(strong) == 1 and abs(strong[0][0] - 6.0) < 0.1
    assert -40 < wf.loudness < 0


def tone(path: Path, seconds: float, lead: float = 0.0, db: float = -20) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={lead or 0.001}",
                    "-f", "lavfi", "-i", f"sine=f=1200:d={seconds}", "-filter_complex",
                    f"[0][1]concat=n=2:v=0:a=1,volume={db}dB", str(path)], check=True)
    return path


@needs_ffmpeg
def test_sound_import_trims_levels_and_names(tmp_path):
    from chatwatcher.config import Config, PathsConfig
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data", output_dir=tmp_path / "out"))
    conn = db.open_db(cfg.paths.db_path)
    src = tone(tmp_path / "in.mp3", 0.8, lead=0.5, db=-35)
    s = sfx.import_sound(cfg, conn, src, original="FAAAH!!.mp3", description="fail scream", tags="Fail, scream, fail")
    assert s["id"] == "faaah" and s["tags"] == "fail, scream" and 0.75 < s["duration_s"] < 0.9   # lead-in trimmed
    level = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(sfx.sound_path(cfg, s)), "-af", "volumedetect",
                            "-f", "null", "-"], capture_output=True, text=True).stderr
    assert "mean_volume: -14" in level or "mean_volume: -13" in level
    assert sfx.import_sound(cfg, conn, src, original="faaah.wav")["id"] == "faaah_2"
    sfx.update_sound(conn, "faaah", name="Faaah", description="d", tags="", source="x · CC0", gain_db=-99)
    assert sfx.get_sound(conn, "faaah")["gain_db"] == -30
    (tmp_path / "bad.mp3").write_text("not audio")
    with pytest.raises(sfx.SoundError):
        sfx.import_sound(cfg, conn, tmp_path / "bad.mp3", original="bad.mp3")
    sfx.delete_sound(cfg, conn, "faaah_2")
    assert [x["id"] for x in sfx.list_sounds(conn)] == ["faaah"] and not (cfg.paths.data_dir / "sounds" / "faaah_2.flac").exists()


def test_clean_effects_and_place():
    ok = clean_effects([{"t": 3, "sound": "boom", "gain": 99, "duck": 5, "id": "a b!", "by": "claude", "why": "x" * 400}],
                       10, {"boom"})
    assert ok == [{"id": "ab", "type": "sfx", "t": 3.0, "sound": "boom", "gain": 10.0, "duck": 0.0, "by": "claude",
                   "why": "x" * 300}]
    for bad in ([{"t": 3, "sound": "nope"}], [{"t": 11, "sound": "boom"}], [{"sound": "boom"}], "x",
                [{"t": 1, "sound": "boom", "type": "zoom"}]):
        with pytest.raises(EditError):
            clean_effects(bad, 10, {"boom"})
    segs = [[10.0, 12.0, 0], [2.0, 4.0, 0], [11.0, 13.0, 0]]
    assert place(11.5, segs, 30) == [1.5, 4.5] and place(3.0, segs, 30) == [3.0] and place(5.0, segs, 30) == []


@needs_ffmpeg
def test_render_mixes_sounds_where_placed(tmp_path):
    src = make_video(tmp_path / "src.mp4", 8)          # a steady 440 Hz tone
    beep = tone(tmp_path / "beep.wav", 1.0, db=-6)    # 1200 Hz
    out = tmp_path / "e.mp4"
    render(EditConfig(), src, [[1.0, 6.0, 0]], out, fps=30,
           sounds=[SoundCue(2.0, beep, 0, -12, 1.0), SoundCue(99, beep), SoundCue(1.0, tmp_path / "gone.wav")])
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-ac", "1", "-ar", "8000", "-f", "f32le", "-"],
                         capture_output=True).stdout
    x = np.frombuffer(pcm, "<f4")

    def band(f, a, b):
        seg = x[int(a * 8000):int(b * 8000)]
        spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        freqs = np.fft.rfftfreq(len(seg), 1 / 8000)
        return 20 * np.log10(spec[np.abs(freqs - f) < 20].max() + 1e-9)
    assert band(1200, 2.2, 2.8) - band(1200, 0.2, 0.8) > 30              # the beep is there…
    assert band(1200, 3.3, 3.9) - band(1200, 2.2, 2.8) < -30             # …and only there
    assert band(440, 2.2, 2.8) < band(440, 0.2, 0.8) - 8                 # her sound ducked under it


@needs_ffmpeg
def test_sounds_page_and_effects(site, tmp_path):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    seg = {"segments": [[2, 8, 0], [12, 16, 0]], "source": "long"}
    assert client.post(f"/moment/{m}/effects/suggest", json=seg).status_code in (404, 405)   # ✨ AI edit does effects

    # Upload two sounds on the Sounds page.
    files = [("files", ("Vine Boom.wav", tone(tmp_path / "a.wav", 0.6).read_bytes(), "audio/wav")),
             ("files", ("bonk.mp3", tone(tmp_path / "b.mp3", 0.3).read_bytes(), "audio/mpeg")),
             ("files", ("notes.txt", b"hello", "text/plain"))]
    r = client.post("/sounds/upload", files=files, data={"description": "impact"}, follow_redirects=False)
    assert "error=" in r.headers["location"] and "notes.txt" in r.headers["location"]
    page = client.get("/sounds").text
    assert "vine_boom" in page and "bonk" in page and 'href="/sounds"' in page
    assert client.get("/media/sound/vine_boom").headers["content-type"] == "audio/flac"
    assert client.get("/media/sound/..%2Fchatwatcher.db").status_code == 404
    client.post("/sounds/bonk/save", data={"name": "Bonk", "description": "someone says something dumb",
                                           "tags": "dumb", "source": "", "gain_db": "-3"})

    # Sounds you place are saved with the edit.
    mine = {"id": "u1", "t": 3.0, "sound": "vine_boom", "gain": 0, "duck": 0, "by": "you"}
    later = {"id": "u2", "t": 14.0, "sound": "vine_boom", "gain": 0, "duck": -10, "by": "you"}
    assert client.post(f"/moment/{m}/edit", json=seg | {"effects": [mine, later]}).json()["ok"]

    # The editor gets them back with the library; rendering mixes them in.
    e = client.get(f"/api/moment/{m}/edit").json()
    assert [f["t"] for f in e["effects"]] == [3.0, 14.0] and {s["id"] for s in e["sounds"]} == {"bonk", "vine_boom"}
    assert e["hits"] is not None and "loudness" in e
    client.post(f"/moment/{m}/render", json=seg | {"effects": e["effects"]})
    Worker(cfg, deps=deps).run(once=True)
    job = conn.execute("SELECT message FROM jobs WHERE moment_id = ? AND stage = 'render'", (m,)).fetchone()
    assert "2 sound effect(s)" in job["message"]
    assert client.get(f"/api/moment/{m}/render").json()["render"]["state"] == "done"

    # Moving a sound makes the render stale; a deleted sound is skipped, not an error.
    client.post(f"/moment/{m}/edit", json=seg | {"effects": [e["effects"][0] | {"t": 4.0}, e["effects"][1]]})
    assert client.get(f"/api/moment/{m}/render").json()["render"]["state"] == "stale"
    client.post("/sounds/vine_boom/delete")
    client.post(f"/moment/{m}/render", json=seg)
    Worker(cfg, deps=deps).run(once=True)
    job = conn.execute("SELECT status, message FROM jobs WHERE moment_id = ? AND stage = 'render'", (m,)).fetchone()
    assert job["status"] == "done" and "skipped sounds no longer in the library: vine_boom" in job["message"]
    bad = client.post(f"/moment/{m}/edit", json=seg | {"effects": [e["effects"][0]]})
    assert bad.status_code == 400 and "library" in bad.json()["error"]


@needs_ffmpeg
def test_starter_pack_files_get_their_details(site, tmp_path):  # noqa: F811
    cfg, conn, client, deps = site
    page = client.get("/sounds").text
    assert "0 of 40 in your library" in page and "soundeffect-lab.info/sound/anime/" in page
    wav = tone(tmp_path / "a.wav", 0.4).read_bytes()
    files = [("files", ("tin1.mp3", wav, "audio/mpeg")), ("files", ("fall-down1 (1).mp3", wav, "audio/mpeg")),
             ("files", ("mine.wav", wav, "audio/wav"))]
    r = client.post("/sounds/upload", files=files, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    chiin = sfx.get_sound(conn, "chiin")
    assert chiin["name"] == "チーン chiin" and "falls flat" in chiin["description"] and "効果音ラボ" in chiin["source"]
    assert sfx.get_sound(conn, "zukko")["tags"] == "absurd, comedy, reaction"
    assert sfx.get_sound(conn, "mine")["description"] == ""
    assert "2 of 40 in your library" in client.get("/sounds").text
    r = client.post("/sounds/upload", files=[("files", ("tin1.mp3", wav, "audio/mpeg"))], follow_redirects=False)
    assert "Already%20in%20your%20library" in r.headers["location"]
    assert conn.execute("SELECT COUNT(*) FROM sounds").fetchone()[0] == 3


def _wav_of_size(path, size):
    """A short tone saved as a WAV file of exactly `size` bytes (44-byte header + 16-bit mono)."""
    import wave
    n = (size - 44) // 2
    t = np.arange(n) / 8000
    pcm = (np.sin(2 * np.pi * 440 * t) * 12000).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(pcm)
    assert path.stat().st_size == size
    return path.read_bytes()


def test_starter_pack_knows_the_sites_own_file_names(site, tmp_path):  # noqa: F811
    cfg, conn, client, deps = site
    assert sfx.starter_for("チーン1.mp3")["file"] == "tin1"
    assert sfx.starter_for("ちゃんちゃん♪1 (2).mp3")["file"] == "chan-chan1"
    assert sfx.starter_for("ドーン.mp3") is None                      # two sounds save as ドーン
    assert sfx.starter_for("ドーン.mp3", 92786)["file"] == "don-1"
    assert sfx.starter_for("ドーン (1).mp3", 60480)["file"] == "doon1"
    heavy = _wav_of_size(tmp_path / "heavy.wav", 92786)
    light = _wav_of_size(tmp_path / "light.wav", 60480)
    wav = tone(tmp_path / "a.wav", 0.4).read_bytes()
    files = [("files", ("ドーン.mp3", heavy, "audio/mpeg")), ("files", ("ドーン (1).mp3", light, "audio/mpeg")),
             ("files", ("チーン1.mp3", wav, "audio/mpeg")), ("files", ("tin1.mp3", wav, "audio/mpeg"))]
    r = client.post("/sounds/upload", files=files, follow_redirects=False)
    assert "Already%20in%20your%20library" in r.headers["location"]       # tin1.mp3 is the same sound as チーン1
    names = {r["name"] for r in conn.execute("SELECT name FROM sounds")}
    assert names == {"ドーン heavy impact", "ドーン reveal", "チーン chiin"}
    assert "3 of 40 in your library" in client.get("/sounds").text


def test_render_in_a_folder_with_a_hash_in_its_name(tmp_path):
    """Stream titles like "iya iya #bozet" end up in folder names; ffmpeg's concat list
    once read the "#" as the end of a URL and couldn't find the parts."""
    from chatwatcher.config import EditConfig
    from chatwatcher.edit.render import render
    from test_clip import make_video
    folder = tmp_path / "2026-11-09_【Karaoke】iya_iya_#bozet_?x" / "01_laugh"
    folder.mkdir(parents=True)
    src = make_video(folder / "clip.mp4", 4)
    r = render(EditConfig(loudness_lufs=0), src, [[0.0, 1.0, 0.0], [2.0, 3.0, 0.0]], folder / "short.mp4", fps=30)
    assert r.path.is_file() and 1.9 < r.duration_s < 2.2
