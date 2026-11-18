"""The music library (tempo, beats, drop), the music bed in an edit, hushes, and ✨ AI
edit's picks of both."""

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from chatwatcher import db
from chatwatcher.config import EditConfig
from chatwatcher.edit import music as mus
from chatwatcher.edit import autoedit as ae
from chatwatcher.edit.motion import Visual
from chatwatcher.edit.render import render
from chatwatcher.edit.service import EditError
from chatwatcher.edit.validate import clean_effects, visuals
from chatwatcher.worker import Worker

from test_clip import make_video
from test_web import mid, site  # noqa: F401  (fixture)

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def kicks(bpm: float, seconds: float, drop: float, sr: int = mus.AN_SR, hz: float = 0.0) -> np.ndarray:
    """A click track: quiet high kicks, then loud bass kicks from `drop` (on a beat).
    hz: a steady tone under it all (to find the music in a render)."""
    rng = np.random.default_rng(1)
    x = (rng.standard_normal(int(seconds * sr)) * 0.002).astype(np.float32)
    if hz:
        x += (0.05 * np.sin(2 * np.pi * hz * np.arange(len(x)) / sr)).astype(np.float32)
    per = 60 / bpm
    for k in range(int(seconds / per)):
        t = k * per
        i, n = int(t * sr), int(0.12 * sr)
        seg = x[i:i + n]
        loud = t >= drop - 1e-6
        e = (0.9 if loud else 0.2) * np.exp(-np.arange(len(seg)) / (0.03 * sr))
        seg += (e * np.sin(2 * np.pi * (60 if loud else 200) * np.arange(len(seg)) / sr)).astype(np.float32)
    return x


def write_wav(path: Path, x: np.ndarray, sr: int) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "-", str(path)],
                   input=x.tobytes(), check=True)
    return path


@pytest.mark.parametrize("bpm", [95, 128, 174])
def test_listen_finds_tempo_beats_and_drop(bpm):
    per = 60 / bpm
    drop = round(20 / per) * per
    h = mus.listen(kicks(bpm, 40, drop))
    assert abs(h.bpm - bpm) < 1.5
    assert max(abs(b - round(b / per) * per) for b in h.beats) < 0.05       # on the beats…
    assert len(h.beats) > 0.9 * 40 / per                                     # …all of them
    assert abs(h.drop_s - drop) < 0.06


def test_bed_lands_the_drop_where_it_was_placed():
    track = {"duration_s": 60.0, "drop_s": 20.0, "beats": [i * 0.5 for i in range(120)]}
    segs = [[10.0, 14.0, 0], [2.0, 6.0, 0]]          # plays clip 10-14, then 2-6: 8 s
    p = Path("t.flac")
    # From the start of the edit: the drop (clip 4.0 → edit 6.0) is 20 s into the track.
    b = mus.bed({"t": 4.0, "lead": -1}, track, p, segs, 30)
    assert (b.at, b.offset, b.drop_at) == (0.0, 14.0, 6.0) and b.length == pytest.approx(8.0)
    assert b.fade_out == pytest.approx(mus.FADE_OUT) and b.beats[0] == 0.0 and all(0 <= t < 8 for t in b.beats)
    # On the drop: silence until then, then the drop itself.
    b = mus.bed({"t": 4.0, "lead": 0}, track, p, segs, 30)
    assert (b.at, b.offset) == (6.0, 20.0)
    # A short build-up; never more than the track has before its drop.
    assert (mus.bed({"t": 4.0, "lead": 2}, track, p, segs, 30).at, mus.bed({"t": 4.0, "lead": 2}, track, p, segs, 30).offset) == (4.0, 18.0)
    early = {"duration_s": 60.0, "drop_s": 1.0}
    assert mus.bed({"t": 4.0, "lead": -1}, early, p, segs, 30).at == 5.0
    # A drop outside the cut has nothing to land on; a short track ends before the edit (no fade).
    assert mus.bed({"t": 8.0}, track, p, segs, 30) is None
    short = {"duration_s": 21.0, "drop_s": 20.0}
    b = mus.bed({"t": 11.0, "lead": 0}, short, p, segs, 30)
    assert b.length == pytest.approx(1.0) and b.fade_out == 0


def test_speech_hushes_and_the_volume_envelope():
    segs = [[10.0, 14.0, 0], [2.0, 6.0, 0]]
    sp = mus.speech_on_edit([[2.5, 3.0], [3.1, 3.4], [11.0, 12.0], [20.0, 21.0]], segs, 30)
    assert sp == [(1.0, 2.0), (4.5, 5.4)]                                  # edit time, short gaps joined
    assert mus.hush_windows([{"type": "hush", "t": 3.0, "d": 0.5}, {"type": "sfx", "t": 3}], segs, 30) == [(5.0, 5.5)]
    assert mus.volume_expr([], -10, 0.1) == "1" and mus.volume_expr([(1, 2)], 0, 0.1) == "1"
    e = mus.volume_expr([(1.0, 2.0), (2.1, 3.0), (5.0, 6.0)], -20, 0.1)
    assert e.count("clip((t-") == 2                                        # 1-2 and 2.1-3 ramp into each other: one dip


def test_clean_effects_music_and_hush():
    fx = clean_effects([{"type": "music", "t": 4, "track": "deja_vu", "gain": 40, "duck": 5, "lead": 999},
                        {"type": "hush", "t": 6, "d": 99, "style": "loud"},
                        {"type": "hush", "t": 7, "style": "black"}], 10, set(), music={"deja_vu"})
    assert fx[0] | {"id": ""} == {"id": "", "type": "music", "t": 4.0, "track": "deja_vu", "gain": 6.0, "duck": 0.0,
                                  "lead": mus.LEAD_MAX, "by": "you", "why": ""}
    assert (fx[1]["d"], fx[1]["style"]) == (3.0, "silence") and (fx[2]["d"], fx[2]["style"]) == (0.6, "black")
    assert clean_effects([{"type": "music", "t": 4, "track": "x", "lead": -5}], 10, set())[0]["lead"] == -1
    for bad in ([{"type": "music", "t": 4, "track": "nope"}],
                [{"type": "music", "t": 4, "track": "deja_vu"}, {"type": "music", "t": 5, "track": "deja_vu"}],
                [{"type": "music", "t": 4, "track": "deja_vu", "gain": "loud"}]):
        with pytest.raises(EditError):
            clean_effects(bad, 10, set(), music={"deja_vu"})
    pics, _ = visuals(fx, {})
    assert [(v.type, v.t, v.d) for v in pics] == [("hold", 7.0, 0.6)]      # only the black one darkens the picture


def test_ai_music_and_hushes():
    segs = [[2.0, 12.0, 0]]
    tracks = [{"id": "deja_vu", "name": "Deja Vu"}]
    got = ae.music_effects(["nope | 5 | all", "deja_vu | 30 | all", "deja_vu | 7.9 | 0 | -3 dB | -12 | the crash", "deja_vu | 8"],
                           segs, [(8.1, 20.0)], tracks)
    assert len(got) == 1 and got[0] | {"id": ""} == {"id": "", "type": "music", "t": 8.1, "track": "deja_vu", "lead": 0.0,
                                                    "gain": -3.0, "duck": -12.0, "by": "claude", "why": "the crash"}
    assert ae.music_effects(["deja_vu | 5 | all | loud"], segs, [], tracks)[0]["lead"] == -1
    assert ae.music_effects(["deja_vu | 5"], segs, [], []) == []
    hs = ae.hush_effects(["7.0 | 0.9 | silence | before the drop", "9 | 0.2 | black", "1 | 1 | black", "x"], segs, drop=8.1)
    assert [(h["t"], h["d"], h["style"]) for h in hs] == [(7.2, 0.9, "silence"), (9.0, 0.2, "black")]   # ends on the drop
    s = ae.schema(3, 2, max_music=0)
    assert s["properties"]["music"]["maxItems"] == 0 and "hushes" in s["required"]
    line = ae.placed_line({"type": "music", "t": 4.0, "track": "deja_vu", "lead": -1, "gain": -6, "duck": -10})
    assert "deja_vu" in line and "this one stays" in line


@needs_ffmpeg
def test_import_levels_listens_and_names(tmp_path):
    from chatwatcher.config import Config, PathsConfig
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data", output_dir=tmp_path / "out"))
    conn = db.open_db(cfg.paths.db_path)
    per = 60 / 128
    src = write_wav(tmp_path / "Deja Vu!.wav", kicks(128, 30, round(16 / per) * per) * 0.2, mus.AN_SR)
    t = mus.import_track(cfg, conn, src, original="Deja Vu!.wav", mood="EUROBEAT", description="racing")
    assert t["id"] == "deja_vu" and t["mood"] == "eurobeat" and abs(t["bpm"] - 128) < 1.5
    assert abs(t["drop_s"] - round(16 / per) * per) < 0.08 and t["found_drop_s"] == t["drop_s"] and len(t["beats"]) > 50
    x = mus._decode(mus.track_path(cfg, t), mus.SR, 2)
    assert abs(mus.loud_level(x, mus.SR) - mus.LEVEL_DBFS) < 1.0 or 20 * np.log10(np.abs(x).max()) > -1.5
    mus.update_track(conn, "deja_vu", name="Deja Vu", mood="nope", description="d", source="", gain_db=-99, drop_s=999)
    t = mus.get_track(conn, "deja_vu")
    assert (t["mood"], t["gain_db"], t["drop_s"]) == ("", -30.0, t["duration_s"])
    with pytest.raises(mus.MusicError):
        mus.import_track(cfg, conn, write_wav(tmp_path / "blip.wav", kicks(128, 2, 1), mus.AN_SR), original="blip.wav")
    (tmp_path / "bad.mp3").write_text("not audio")
    with pytest.raises(mus.MusicError):
        mus.import_track(cfg, conn, tmp_path / "bad.mp3", original="bad.mp3")
    mus.delete_track(cfg, conn, "deja_vu")
    assert mus.list_music(conn) == [] and not list((cfg.paths.data_dir / "music").glob("*.flac"))


@needs_ffmpeg
def test_render_mixes_the_bed_ducks_it_and_hushes(tmp_path):
    src = make_video(tmp_path / "src.mp4", 10)                     # a steady 440 Hz tone, her "voice"
    m = write_wav(tmp_path / "m.wav", 0.3 * np.sin(2 * np.pi * 1200 * np.arange(30 * 44100) / 44100).astype(np.float32), 44100)
    segs = [[0.0, 8.0, 0]]
    bed = mus.bed({"t": 2.0, "lead": 0, "gain": 0, "duck": -14}, {"duration_s": 30.0, "drop_s": 5.0}, m, segs, 30)
    out = tmp_path / "e.mp4"
    render(EditConfig(), src, segs, out, fps=30, music=bed, speech=[(4.0, 5.0)], mutes=[(6.0, 6.5)],
           visuals=[Visual("hold", 6.0, 0.5, color="black")])
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-ac", "1", "-ar", "8000", "-f", "f32le", "-"],
                         capture_output=True).stdout
    x = np.frombuffer(pcm, "<f4")

    def band(f, a, b):
        seg = x[int(a * 8000):int(b * 8000)]
        spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        return 20 * np.log10(spec[np.abs(np.fft.rfftfreq(len(seg), 1 / 8000) - f) < 20].max() + 1e-9)
    assert band(1200, 2.3, 3.8) - band(1200, 0.3, 1.8) > 30            # no music before the drop, then it hits
    assert band(1200, 4.3, 4.8) < band(1200, 2.3, 2.8) - 10            # it dips under her words
    assert band(1200, 5.3, 5.8) > band(1200, 4.3, 4.8) + 10            # …and comes back
    assert band(440, 6.1, 6.4) < band(440, 0.3, 0.6) - 40              # the hush: silence…
    frame = subprocess.run(["ffmpeg", "-v", "error", "-ss", "6.25", "-i", str(out), "-frames:v", "1", "-vf", "scale=16:16",
                            "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True).stdout
    assert max(frame) < 30                                             # …and black


@needs_ffmpeg
def test_music_page_editor_and_render(site, tmp_path):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    per = 60 / 128
    wav = write_wav(tmp_path / "a.wav", kicks(128, 30, round(16 / per) * per, hz=1200) * 0.3, mus.AN_SR)
    r = client.post("/music/upload", files=[("files", ("Gas Gas.wav", wav.read_bytes(), "audio/wav")),
                                            ("files", ("blip.wav", tone_bytes(tmp_path), "audio/wav"))],
                    data={"mood": "eurobeat"}, follow_redirects=False)
    assert "gas_gas" in r.headers["location"] and "blip.wav" in r.headers["location"]       # too short: an error
    page = client.get("/sounds").text
    assert 'id="m-gas_gas"' in page and "BPM" in page and "eurobeat" in page
    assert client.get("/media/music/gas_gas").headers["content-type"] == "audio/flac"
    assert client.get("/media/music/..%2Fchatwatcher.db").status_code == 404
    client.post("/music/gas_gas/save", data={"name": "Gas Gas", "mood": "eurobeat", "description": "racing",
                                             "source": "", "gain_db": "0", "drop_s": "15"})
    assert mus.get_track(conn, "gas_gas")["drop_s"] == 15.0

    e = client.get(f"/api/moment/{m}/edit").json()
    assert e["music"][0]["id"] == "gas_gas" and e["music"][0]["beats"] and e["music_info"]["moods"]["eurobeat"]
    assert "speech" in e and e["visual"]["types"]["hush"]
    seg = {"segments": [[2, 8, 0], [12, 16, 0]], "source": "long"}
    fx = [{"id": "u1", "type": "music", "t": 13.0, "track": "gas_gas", "lead": -1, "gain": -3, "duck": -10},
          {"id": "u2", "type": "hush", "t": 12.2, "d": 0.4, "style": "black"}]
    assert client.post(f"/moment/{m}/edit", json=seg | {"effects": fx}).json()["ok"]
    assert client.get(f"/api/moment/{m}/edit").json()["effects"][1]["track"] == "gas_gas"
    client.post(f"/moment/{m}/render", json=seg | {"effects": fx})
    Worker(cfg, deps=deps).run(once=True)
    info = client.get(f"/api/moment/{m}/render").json()["render"]
    assert info["state"] == "done"
    import json
    rendered = json.loads(conn.execute("SELECT rendered_json FROM edits WHERE moment_id = ?", (m,)).fetchone()[0])
    assert rendered["music"] == "gas_gas" and not rendered["missing"]
    # A deleted track: the edit renders without music, and says so.
    client.post("/music/gas_gas/delete")
    client.post(f"/moment/{m}/render", json=seg)
    Worker(cfg, deps=deps).run(once=True)
    rendered = json.loads(conn.execute("SELECT rendered_json FROM edits WHERE moment_id = ?", (m,)).fetchone()[0])
    assert rendered["music"] is None and rendered["missing"] == ["gas_gas"]


def tone_bytes(tmp_path: Path) -> bytes:
    return write_wav(tmp_path / "blip.wav", kicks(128, 2, 1), mus.AN_SR).read_bytes()
