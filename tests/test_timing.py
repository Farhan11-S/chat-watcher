"""Time effects: slow motion, fast forward, a speed ramp, a freeze frame."""

import json
import shutil
import subprocess

import pytest

from chatwatcher.config import EditConfig
from chatwatcher.edit import autoedit as ae
from chatwatcher.edit import music as mus
from chatwatcher.edit import timing as tm
from chatwatcher.edit.render import SoundCue, lengths, place, render, retime_audio  # noqa: F401
from chatwatcher.edit.service import EditError
from chatwatcher.edit.validate import clean_effects
from chatwatcher.subs import lines
from chatwatcher.worker import Worker

from test_clip import make_video
from test_web import mid, site  # noqa: F401  (fixture)

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def test_clean_time():
    slow, frz, fast = clean_effects([
        {"type": "time", "t": 1, "style": "slow", "rate": 0.01, "d": 2, "sound": "deep"},
        {"type": "time", "t": 2, "style": "freeze", "d": 99, "zoom": 9, "region": "Face ", "mono": 1},
        {"type": "time", "t": 3, "style": "fast", "rate": 3, "sound": "nope"}], 10, set())
    assert (slow["rate"], slow["sound"], slow["d"]) == (0.2, "deep", 2.0)
    assert (frz["d"], frz["zoom"], frz["region"], frz["mono"]) == (8.0, 2.5, "face", True) and "rate" not in frz
    assert (fast["rate"], fast["sound"], fast["d"]) == (3.0, "tempo", tm.D["fast"][0])
    with pytest.raises(EditError):
        clean_effects([{"type": "time", "t": 1, "style": "slow", "rate": "fast"}], 10, set())


def test_retime_parts_lengths_and_place():
    fps = 30
    segs = [[0.0, 4.0, 0], [10.0, 12.0, -3]]
    fx = [{"type": "time", "t": 1.0, "d": 1.0, "style": "slow", "rate": 0.5, "sound": "deep"},
          {"type": "time", "t": 1.5, "d": 1.0, "style": "fast", "rate": 2},          # overlaps the slow: skipped
          {"type": "time", "t": 3.0, "d": 2.0, "style": "freeze", "zoom": 1.5, "region": "face"},
          {"type": "time", "t": 11.0, "d": 0.6, "style": "ramp", "rate": 0.3}]
    parts = tm.retime(segs, fx, fps, lambda name: (0.3, 0.2) if name == "face" else None)
    assert parts[0] == [0.0, 1.0, 0] and parts[1] == [1.0, 2.0, 0, 0.5, {"sound": "deep"}]
    assert parts[2] == [2.0, 3.0, 0]
    frz = parts[3]
    assert frz[:2] == [3.0, round(3 + 1 / fps, 4)] and frz[4]["freeze"] == {"zoom": 1.5, "x": 0.3, "y": 0.2, "mono": False}
    assert parts[4][0] == round(3 + 1 / fps, 4) and parts[4][1] == 4.0
    ramp = [p for p in parts if p[0] >= 11.0 and len(p) > 3]
    assert len(ramp) == 5 and ramp[0][3] == tm.RAMP_FROM and ramp[-1][3] == 0.3 and ramp[-1][1] == 11.6
    assert all(p[2] == -3 for p in parts if p[0] >= 10)
    ls = lengths(parts, fps)
    assert ls[1] == pytest.approx(2.0) and ls[3] == pytest.approx(2.0)          # 1 s at half speed; the held frame
    assert sum(ls) == pytest.approx(4 + 1 + 2 + 2 + sum((p[1] - p[0]) / p[3] - (p[1] - p[0]) for p in ramp), abs=0.2)
    assert place(1.5, parts, fps) == [pytest.approx(2.0)]                       # 0.5 s into the slow part = 1 s
    assert place(3.0, parts, fps) == [pytest.approx(4.0)]                       # the frozen frame
    assert place(3.5, parts, fps)[0] == pytest.approx(4.0 + 2.0 + 0.5 - 1 / fps, abs=0.01)
    assert tm.retime(segs, [], fps) is segs                                     # nothing to do: unchanged
    # speech and subtitles follow the speed
    assert mus.speech_on_edit([[1.2, 1.4]], parts, fps)[0] == pytest.approx((1.4, 1.8))
    shown = lines.on_edit([{"t0": 1.0, "t1": 2.0, "text": "slow"}], parts, ls)
    assert (shown[0][0], shown[0][1]) == (pytest.approx(1.0), pytest.approx(3.0))


def test_audio_chains():
    assert retime_audio(0.25, {"sound": "tempo"})[:2] == ["atempo=0.5", "atempo=0.500000"]
    assert retime_audio(3.0, {"sound": "tempo"})[0] == "atempo=2.0"
    assert any(f.startswith("asetrate=24000") for f in retime_audio(0.5, {"sound": "deep"}))
    assert retime_audio(0.01, {"freeze": {"zoom": 1}}, 2)[0] == "volume=0" and retime_audio(1, {}) == []


def _gray(path, at, size="32:24"):
    px = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{at:.3f}", "-i", str(path), "-frames:v", "1", "-vf",
                         f"scale={size}", "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True).stdout
    return px


def _diff(a, b):
    return sum(abs(x - y) for x, y in zip(a, b)) / max(1, len(a))


@needs_ffmpeg
def test_render_slow_fast_and_freeze(tmp_path):
    src = make_video(tmp_path / "src.mp4", 8)
    out = tmp_path / "e.mp4"
    fx = [{"type": "time", "t": 1.0, "d": 1.0, "style": "slow", "rate": 0.5},
          {"type": "time", "t": 3.0, "d": 1.0, "style": "freeze", "zoom": 1.0, "mono": True},
          {"type": "time", "t": 5.0, "d": 2.0, "style": "fast", "rate": 2}]
    parts = tm.retime([[0.0, 7.0, 0]], fx, 30)
    r = render(EditConfig(loudness_lufs=0), src, parts, out, fps=30)
    assert r.duration_s == pytest.approx(sum(lengths(parts, 30)), abs=0.1)   # 1 + 2 + 1 + 1 (held) + 1 + 1
    # 0.5 s into the slow part (edit 1.5) is clip 1.25
    assert _diff(_gray(out, 1.5), _gray(src, 1.25)) < _diff(_gray(out, 1.5), _gray(src, 1.5))
    # the freeze: edit 4.0 to 5.0 is one frame, in black and white
    assert _diff(_gray(out, 4.1), _gray(out, 4.8)) < 1.0
    rgb = subprocess.run(["ffmpeg", "-v", "error", "-ss", "4.5", "-i", str(out), "-frames:v", "1", "-vf", "scale=8:6",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    assert max(abs(rgb[i] - rgb[i + 1]) for i in range(0, len(rgb) - 2, 3)) < 12
    # fast: clip 6.0 lands half a second (a clip second) into the fast part
    at = place(6.0, parts, 30)[0]
    assert at == pytest.approx(sum(lengths(parts, 30)) - 0.5, abs=0.05)
    assert _diff(_gray(out, at), _gray(src, 6.0)) < _diff(_gray(out, at), _gray(src, 5.5))


def test_ai_time():
    got = ae.time_effects(["10 | 1.5 | slow | 0.5 | | the crash", "4 | 2 | freeze | 1.4 | face | stare",
                           "99 | 1 | slow", "11 | 1 | ramp | 0.3 | overlaps the slow", "2 | 1.5 | fast | 3 | boring bit"],
                          [[0, 20, 0]])
    assert [(f["style"], f["t"]) for f in got] == [("fast", 2.0), ("freeze", 4.0), ("slow", 10.0)]
    frz = got[1]
    assert (frz["zoom"], frz["region"], frz["d"]) == (1.4, "face", 2.0) and got[2]["why"] == "the crash"
    assert "time" in ae.schema(3, 2)["required"]
    assert "freeze" in ae.placed_line({"type": "time", "t": 4.0, "d": 2.0, "style": "freeze", "zoom": 1.4})


@needs_ffmpeg
def test_time_effects_in_the_editor_and_render(site):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    e = client.get(f"/api/moment/{m}/edit").json()
    assert e["time_info"]["styles"] and e["visual"]["types"]["time"]
    seg = {"segments": [[2, 8, 0]], "source": "long"}
    fx = [{"id": "u1", "type": "time", "t": 3.0, "d": 1.0, "style": "slow", "rate": 0.5},
          {"id": "u2", "type": "time", "t": 7.0, "d": 1.0, "style": "freeze"}]
    assert client.post(f"/moment/{m}/edit", json=seg | {"effects": fx}).json()["ok"]
    client.post(f"/moment/{m}/render", json=seg | {"effects": fx})
    Worker(cfg, deps=deps).run(once=True)
    assert client.get(f"/api/moment/{m}/render").json()["render"]["state"] == "done"
    info = json.loads(conn.execute("SELECT rendered_json FROM edits WHERE moment_id = ?", (m,)).fetchone()[0])
    assert info["duration_s"] == pytest.approx(6 + 1 + 1, abs=0.15)


def test_preview_parts_match_the_render():
    from pathlib import Path
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    js = (Path(__file__).resolve().parents[1] / "src/chatwatcher/web/static/editor/util.js").read_text(encoding="utf-8")
    code = js[js.index("// ── time effects (edit/timing.py)"):js.index("export const partAt")].replace("\nexport ", "\n")
    code = code.replace("export function timeParts", "function timeParts")
    segs = [[0.0, 4.0, 0], [10.0, 12.0, 0]]
    fx = [{"type": "time", "t": 1.0, "d": 1.0, "style": "slow", "rate": 0.5},
          {"type": "time", "t": 3.0, "d": 2.0, "style": "freeze", "zoom": 1.5},
          {"type": "time", "t": 10.5, "d": 1.4, "style": "ramp", "rate": 0.3},
          {"type": "time", "t": 11.95, "d": 1.0, "style": "fast", "rate": 2}]
    script = (f"const S = {{ D: {{ fps: 30, time_info: {json.dumps(tm.info())} }}, segs: {json.dumps(segs)}, "
              f"fx: {json.dumps(fx)} }};\n{code}\n"
              "console.log(JSON.stringify(timeParts().map(p => [p.s, p.e, p.k])));")
    got = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout)
    want = [[p[0], p[1], tm.speed(p)] for p in tm.retime(segs, fx, 30)]
    assert len(got) == len(want)
    for g, w in zip(got, want):
        assert g == pytest.approx(w, abs=1e-3)
