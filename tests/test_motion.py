import json
import math
import random
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from chatwatcher.config import EditConfig
from chatwatcher.edit import layout as lay
from chatwatcher.edit import motion
from chatwatcher.edit.motion import Visual, camera, flashes, local, video_filter, view_exprs
from chatwatcher.edit.render import render
from chatwatcher.edit.service import EditError, clean_effects
from chatwatcher.worker import Worker

from test_edit import FakeSuggester
from test_web import mid, site  # noqa: F401  (fixture)

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
EDITOR_JS = Path(__file__).resolve().parents[1] / "src/chatwatcher/web/static/editor.js"

_ENV = {"_if": lambda c, a, b: a if c else b, "gte": lambda a, b: float(a >= b), "lt": lambda a, b: float(a < b),
        "gt": lambda a, b: float(a > b), "clip": lambda x, lo, hi: max(min(x, hi), lo), "sin": math.sin}


def ffmpeg_eval(expr: str, t: float) -> float:
    """Evaluate one of our ffmpeg expressions in Python (they only use these functions)."""
    return eval(expr.replace("if(", "_if("), dict(_ENV), {"T": t})  # noqa: S307 (our own expression)


def busy_effects() -> list[Visual]:
    return [Visual("push", 1.0, 3.0, zoom=1.4, cx=0.78, cy=0.45), Visual("punch", 2.5, 1.0, zoom=2.2, cx=0.97, cy=0.03),
            Visual("shake", 2.7, 0.8, strength=3), Visual("shake", 3.0, 0.5, strength=2),
            Visual("shake", 6.0, 0.5, strength=1), Visual("flash", 2.7, 0.3, color="red")]


def test_render_expressions_match_the_camera():
    vis = busy_effects()
    xc, yc, h = view_exprs(vis, "T")
    for i in range(3000):
        t = i / 375
        want, got = camera(t, vis), (ffmpeg_eval(xc, t), ffmpeg_eval(yc, t), ffmpeg_eval(h, t))
        assert max(abs(a - b) for a, b in zip(want, got)) < 1e-5, t


def test_the_view_never_leaves_the_frame():
    rnd = random.Random(7)
    for _ in range(300):
        vis = [Visual(rnd.choice(["punch", "push", "shake"]), rnd.uniform(0, 5), rnd.uniform(0.2, 3),
                      zoom=rnd.uniform(1.05, 4), cx=rnd.random(), cy=rnd.random(), strength=rnd.randint(1, 3))
               for _ in range(rnd.randint(1, 5))]
        for k in range(80):
            x, y, h = camera(k / 10, vis)
            assert x - h >= -1e-9 and x + h <= 1 + 1e-9 and y - h >= -1e-9 and y + h <= 1 + 1e-9


def test_effects_look_right():
    punch = [Visual("punch", 1.0, 1.0, zoom=2.0, cx=0.3, cy=0.4)]
    assert camera(0.99, punch) == (0.5, 0.5, 0.5) and camera(2.0, punch) == (0.5, 0.5, 0.5)
    assert camera(1.5, punch) == pytest.approx((0.3, 0.4, 0.25))
    edge = [Visual("punch", 0, 1, zoom=2.0, cx=0.95, cy=0.5)]          # slides in so no black edge shows
    assert camera(0.5, edge) == pytest.approx((0.75, 0.5, 0.25))
    push = [Visual("push", 0, 4, zoom=1.5, cx=0.7, cy=0.5)]
    zooms = [0.5 / camera(t, push)[2] for t in (0, 1, 2, 3, 3.99)]
    assert zooms[0] == pytest.approx(1) and zooms == sorted(zooms) and zooms[-1] == pytest.approx(1.5, abs=1e-3)
    shake = [Visual("shake", 0, 1, strength=3)]
    moves = {round(camera(t / 30, shake)[0], 4) for t in range(30)}
    assert len(moves) > 20 and camera(1.0, shake) == (0.5, 0.5, 0.5)
    assert flashes(0.0, [Visual("flash", 0, 0.4, color="red")]) == [((255, 30, 30), 0.5)]
    assert flashes(0.2, [Visual("flash", 0, 0.4)])[0][1] == pytest.approx(0.425)
    assert motion.fit_zoom({"x": 0.6, "y": 0.2, "w": 0.25, "h": 0.5}) == 2.0


def test_segments_get_only_their_own_effects():
    vis = [Visual("punch", 5.0, 2.0), Visual("flash", 12.0, 0.3), Visual("shake", 30, 1)]
    seg = local(vis, 6.0, 7.0)                    # 6–13 s: the end of the punch-in and the flash
    assert [(v.type, v.t) for v in seg] == [("punch", -1.0), ("flash", 6.0)]
    assert video_filter([], 30, 5) is None
    vf = video_filter(seg, 30, 7.0)
    assert vf.startswith("perspective=") and "(in/30)" in vf and vf.count("drawbox") == 9    # 0.3 s × 30 fps
    assert video_filter([Visual("flash", 0, 0.2)], 30, 5).count("drawbox") == 6


SHORT_LAYOUTS = [None, {"type": "crop", "x": 0.75, "y": 0.5, "scale": 1.4, "title": ""},
                 {"type": "stack", "top": "game", "bottom": "face", "split": 0.6, "title": ""},
                 {"type": "fit", "title": "x"}]


@pytest.mark.parametrize("layout", SHORT_LAYOUTS)
def test_preview_maths_matches_the_render(layout):
    """editor.js draws the preview with a copy of camera() (and, for a Short, of the layout's
    panels); they must agree."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    js = EDITOR_JS.read_text(encoding="utf-8")
    start, end = js.index("  function centre("), js.index("  // The picture box inside")
    regions = {"face": {"x": 0.7, "y": 0.2, "w": 0.2, "h": 0.4}, "game": {"x": 0, "y": 0, "w": 0.7, "h": 0.8}}
    fx = [{"type": "push", "t": 1.0, "d": 3.0, "zoom": 1.4, "region": "game"},
          {"type": "punch", "t": 2.5, "d": 1.0, "zoom": 2.2, "region": "face"},
          {"type": "punch", "t": 5.0, "d": 1.0, "zoom": 1.5, "region": "nowhere"},
          {"type": "shake", "t": 2.7, "d": 0.8, "strength": 3}, {"type": "shake", "t": 6.0, "d": 0.5, "strength": 1},
          {"type": "flash", "t": 2.7, "d": 0.3, "color": "red"}]
    D = {"regions": regions, "visual": {"zoom_range": motion.ZOOM_RANGE, "shake": motion.SHAKE_AMP, "flash": motion.FLASH,
                                        "margin": motion.MARGIN, "waves": [motion.WAVES_X, motion.WAVES_Y]},
         "layout_info": {"out": [lay.OUT_W, lay.OUT_H], "src": [1920, 1080]}}
    times = [i / 50 for i in range(400)]
    script = (f"const ZOOMS = {json.dumps(motion.ZOOMS)};\nconst D = {json.dumps(D)};\nlet fx = {json.dumps(fx)};\n"
              f"let kind = {json.dumps('short' if layout else 'long')}, layout = {json.dumps(layout)}, regionMode = false;\n"
              "const video = { videoWidth: 1920, videoHeight: 1080 };\n"
              f"{js[start:end]}\nconsole.log(JSON.stringify({{cams: {json.dumps(times)}.map(t => [camera(t), flashesAt(t)]),"
              " panels: layout ? panels() : []}));")
    got = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout)
    from chatwatcher.edit.service import short_visuals, visuals
    fx_full = [f | {"strength": f.get("strength", 2)} for f in fx]
    if layout:
        ps = lay.panels(layout, regions, 1920, 1080)
        assert len(got["panels"]) == len(ps)
        for g, p in zip(got["panels"], ps):
            assert g["src"] == pytest.approx(list(p.src), abs=1e-12) and g["dst"] == pytest.approx(list(p.dst), abs=1e-12)
            assert bool(g.get("blur")) == p.blur
        vis, missing = short_visuals(fx_full, regions, ps)
    else:
        vis, missing = visuals(fx_full, regions)
    assert missing == {"nowhere"}
    for t, (cam, fl) in zip(times, got["cams"]):
        assert cam == pytest.approx(list(camera(t, vis)), abs=1e-9), t
        want = [(list(rgb), a) for rgb, a in flashes(t, vis)]
        assert len(want) == len(fl) and all(w[0] == g[0] and abs(w[1] - g[1]) < 1e-9 for w, g in zip(want, fl)), t


def _rgb_at(path: Path, t: float) -> list[tuple[int, int, int]]:
    """The 2×2 average colours of the frame at t (top left, top right, bottom left, bottom right)."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t}", "-i", str(path), "-frames:v", "1", "-vf", "format=rgb24,scale=2:2:flags=area",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return [tuple(raw[i:i + 3]) for i in range(0, 12, 3)]


@needs_ffmpeg
def test_render_shows_the_punch_in_and_the_flash(tmp_path):
    src = tmp_path / "quad.mp4"            # blue, with a red top-left quarter
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=30:d=4",
                    "-vf", "drawbox=x=0:y=0:w=160:h=90:color=red:t=fill", "-pix_fmt", "yuv420p", str(src)], check=True)
    vis = [Visual("punch", 1.0, 1.0, zoom=2.0, cx=0.25, cy=0.25), Visual("flash", 2.5, 0.5, color="white")]
    out = tmp_path / "e.mp4"
    render(EditConfig(loudness_lufs=0), src, [[0.0, 4.0, 0]], out, fps=30, has_audio=False, visuals=vis)
    red = lambda c: c[0] > 180 and c[2] < 80                       # noqa: E731
    blue = lambda c: c[2] > 180 and c[0] < 80                      # noqa: E731
    before, zoomed, flashed, after = (_rgb_at(out, t) for t in (0.5, 1.5, 2.5, 3.5))
    assert red(before[0]) and all(blue(c) for c in before[1:])
    assert all(red(c) for c in zoomed)                             # the red quarter fills the frame
    assert all(min(c) > 190 for c in flashed)                      # white flash
    assert red(after[0]) and blue(after[3])


def test_clean_effects_checks_picture_effects():
    fx = clean_effects([
        {"type": "punch", "t": 2, "d": 99, "zoom": 9, "region": " Face "},
        {"type": "push", "t": 1, "region": "../etc", "by": "claude", "why": "build-up"},
        {"type": "shake", "t": 3, "d": 0.01, "strength": 7},
        {"type": "flash", "t": 4, "color": "purple"},
    ], 10, set())
    push, punch, shake, flash = fx                       # sorted by time
    assert push == {"id": "fx2", "type": "push", "t": 1.0, "d": 3.0, "region": "", "zoom": 1.25, "by": "claude", "why": "build-up"}
    assert (punch["d"], punch["zoom"], punch["region"]) == (10.0, 4.0, "face")
    assert (shake["d"], shake["strength"]) == (0.1, 3) and flash["color"] == "white" and flash["d"] == 0.3
    with pytest.raises(EditError, match="sound, zoom"):
        clean_effects([{"type": "spin", "t": 1}], 10, set())


@needs_ffmpeg
def test_regions_drive_zooms_and_renders(site):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    d = client.get(f"/api/moment/{m}/edit").json()
    assert d["regions"] == {} and d["region_scope"] == "this stream" and "punch" in d["visual"]["types"]
    bad = client.post(f"/moment/{m}/regions", json={"regions": [{"name": "face", "x": 0, "y": 0, "w": 1, "h": 1}] * 2})
    assert bad.status_code == 400 and "Two regions" in bad.json()["error"]
    r = client.post(f"/moment/{m}/regions", json={"regions": [{"name": "Face", "x": 0.6, "y": 0.1, "w": 0.3, "h": 0.5}]}).json()
    assert r["ok"] and r["regions"] == {"face": {"x": 0.6, "y": 0.1, "w": 0.3, "h": 0.5}}
    other = mid(conn, 2)                                    # every clip of the stream shares them
    assert client.get(f"/api/moment/{other}/edit").json()["regions"] == r["regions"]

    seg = {"segments": [[1, 6, 0]], "effects": [
        {"type": "punch", "t": 2.0, "d": 1.0, "region": "face", "zoom": 2},
        {"type": "shake", "t": 4.0, "d": 0.5, "strength": 3}, {"type": "flash", "t": 4.0, "color": "red"}]}
    client.post(f"/moment/{m}/render", json=seg)
    Worker(cfg, deps=deps).run(once=True)
    job = conn.execute("SELECT status, message FROM jobs WHERE moment_id = ? AND stage = 'render'", (m,)).fetchone()
    assert job["status"] == "done" and "3 zoom/shake/flash" in job["message"]
    assert client.get(f"/api/moment/{m}/render").json()["render"]["state"] == "done"
    # Redrawing the face box changes where the punch-in lands, so the render is out of date.
    client.post(f"/moment/{m}/regions", json={"regions": [{"name": "face", "x": 0.1, "y": 0.1, "w": 0.3, "h": 0.5}]})
    assert client.get(f"/api/moment/{m}/render").json()["render"]["state"] == "stale"


def test_claude_places_camera_effects(site, monkeypatch):  # noqa: F811
    from chatwatcher.edit import suggest
    monkeypatch.setitem(suggest.VISUAL_INTENSITY, "normal", 2.0)       # room for all of them in this short cut
    cfg, conn, client, deps = site
    m = mid(conn)
    client.post(f"/moment/{m}/regions", json={"regions": [{"name": "face", "x": 0.6, "y": 0.1, "w": 0.25, "h": 0.5}]})
    seg = {"segments": [[2, 8, 0], [12, 16, 0]]}
    mine = {"type": "shake", "t": 3.0, "d": 0.5, "strength": 1, "by": "you"}
    deps.claude = FakeSuggester([], visual=[
        {"kind": "punch_in", "at": 5.0, "length_s": 1.2, "region": "face", "zoom": 1.05, "strength": "normal",
         "color": "white", "why": "she realises"},
        {"kind": "flash", "at": 13.0, "length_s": 0.3, "region": "centre", "zoom": 1.05, "strength": "normal",
         "color": "red", "why": "the fail"},
        {"kind": "shake", "at": 10.0, "length_s": 0.5, "region": "centre", "zoom": 1.05, "strength": "heavy",
         "color": "white", "why": "in the gap, so dropped"},
        {"kind": "punch_in", "at": 14.0, "length_s": 1, "region": "chat", "zoom": 2, "strength": "normal",
         "color": "white", "why": "no such region: centre"},
    ])
    d = client.post(f"/moment/{m}/effects/suggest", json=seg | {"effects": [mine]}).json()
    assert d["ok"] and d["added_visual"] == 3 and d["added_sounds"] == 0 and d["dropped"] == 1
    by_t = {f["t"]: f for f in d["effects"]}
    assert by_t[3.0]["by"] == "you"
    assert by_t[5.0] | {"id": ""} == {"id": "", "type": "punch", "t": 5.0, "d": 1.2, "region": "face", "zoom": 2.0,
                                      "by": "claude", "why": "she realises"}       # 1.05 = "fill the frame"
    assert by_t[13.0]["color"] == "red" and by_t[14.0]["region"] == ""
    call = deps.claude.calls[0]
    regions_enum = call["schema"]["properties"]["visual"]["items"]["properties"]["region"]["enum"]
    assert regions_enum == ["face", "centre"] and "- face: x 60-85%" in call["prompt"]
    assert "punch_in" in call["system"] and re.search(r"at most \d+ camera effects", call["prompt"])
    assert "- 3.00–3.50 s shake light" in call["prompt"]               # Claude builds around yours
