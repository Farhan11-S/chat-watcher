import json
import shutil
import subprocess
from pathlib import Path

import pytest

from chatwatcher import jobs
from chatwatcher.config import EditConfig
from chatwatcher.edit import layout as lay
from chatwatcher.edit.motion import Visual
from chatwatcher.edit.render import render
from chatwatcher.edit.service import short_visuals
from chatwatcher.subs import lines
from chatwatcher.worker import Worker

from test_clip import make_video
from test_web import mid, site  # noqa: F401  (fixture)

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
FACE = {"x": 0.7, "y": 0.5, "w": 0.2, "h": 0.4}
GAME = {"x": 0.0, "y": 0.0, "w": 0.7, "h": 1.0}


def probe(path: Path) -> dict:
    return json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,nb_frames",
         "-of", "json", str(path)], capture_output=True, text=True).stdout)["streams"][0]


# ── layout maths ─────────────────────────────────────────────────────────────

def test_default_layout_follows_the_regions():
    assert lay.default({}, "talk")["type"] == "crop" and lay.default({}, "talk")["x"] == 0.5
    assert lay.default({"face": FACE}, "talk") | {} == lay.default({"face": FACE}, "talk")
    assert lay.default({"face": FACE}, "talk")["x"] == 0.8                  # centred on her face
    st = lay.default({"face": FACE, "game": GAME}, "gaming")
    assert (st["type"], st["top"], st["bottom"]) == ("stack", "game", "face")
    assert lay.default({"game": GAME}, "gaming")["type"] == "crop"          # no face box: nothing to stack


def test_clean_layout_checks_every_field():
    first = lay.default({}, None)
    got = lay.clean({"type": "fit", "x": 7, "scale": 0.2, "split": "x", "top": "Game ", "bottom": "../etc",
                     "title": "Big\nnews  *now*" + "!" * 200}, first)
    assert got["type"] == "fit" and got["x"] == 1.0 and got["scale"] == 1.0 and got["split"] == 0.6
    assert got["top"] == "game" and got["bottom"] == "" and got["title"].startswith("Big news *now*")
    assert len(got["title"]) == lay.MAX_TITLE
    assert lay.clean({"type": "nope"}, first)["type"] == "crop"
    assert lay.clean(None, first) == first


def test_crop_is_a_9_16_window_kept_inside_the_frame():
    (p,) = lay.panels({"type": "crop", "x": 0.5, "y": 0.5, "scale": 1.0}, {}, 1920, 1080)
    x, y, w, h = p.src
    assert (y, h) == (0.0, 1.0) and abs(w * 1920 / 1080 - 9 / 16) < 1e-9 and abs(x + w / 2 - 0.5) < 1e-9
    (p,) = lay.panels({"type": "crop", "x": 1.0, "y": 0.5, "scale": 1.0}, {}, 1920, 1080)
    assert abs(p.src[0] + p.src[2] - 1.0) < 1e-9                             # slid to the edge, not past it
    (p,) = lay.panels({"type": "crop", "x": 0.5, "y": 0.0, "scale": 2.0}, {}, 1920, 1080)
    assert p.src[1] == 0.0 and abs(p.src[3] - 0.5) < 1e-9                    # 2× tighter, at the top


def test_stack_shows_each_whole_region_in_its_panel():
    top, bottom = lay.panels({"type": "stack", "top": "game", "bottom": "face", "split": 0.6},
                             {"game": GAME, "face": FACE}, 1920, 1080)
    assert top.dst == (0.0, 0.0, 1.0, 0.6) and bottom.dst[1] == 0.6
    for p in (top, bottom):
        x, y, w, h = p.src
        assert abs((w * 1920) / (h * 1080) - (1080 * p.dst[2]) / (1920 * p.dst[3])) < 1e-6     # the panel's shape
        assert 0 <= x and x + w <= 1 + 1e-9 and 0 <= y and y + h <= 1 + 1e-9
    x, y, w, h = bottom.src                                    # her face fits: all of it shows
    assert x <= FACE["x"] and x + w >= FACE["x"] + FACE["w"] and y <= FACE["y"] and y + h >= FACE["y"] + FACE["h"]
    x, y, w, h = top.src                                       # the wide game can't: full height, sides trimmed evenly
    assert h == 1.0 and abs(x + w / 2 - (GAME["x"] + GAME["w"] / 2)) < 1e-9
    # A missing region is the whole frame.
    t, _ = lay.panels({"type": "stack", "top": "", "bottom": "nope", "split": 0.5}, {}, 1920, 1080)
    assert t.src[2] == 1.0 or t.src[3] == 1.0


def test_fit_is_the_whole_frame_over_a_blurred_copy():
    bg, fg = lay.panels({"type": "fit"}, {}, 1920, 1080)
    assert bg.blur and bg.dst == (0.0, 0.0, 1.0, 1.0) and not fg.blur and fg.src == (0.0, 0.0, 1.0, 1.0)
    assert abs(fg.dst[3] - 1080 * 1080 / 1920 / 1920) < 1e-9 and abs(fg.dst[1] + fg.dst[3] / 2 - 0.5) < 1e-9


def test_points_map_onto_the_short():
    ps = lay.panels({"type": "stack", "top": "game", "bottom": "face", "split": 0.6},
                    {"game": GAME, "face": FACE}, 1920, 1080)
    fx, fy = lay.map_point(0.8, 0.7, ps)
    assert fy > 0.6                                                          # her face is in the bottom panel
    bg_ps = lay.panels({"type": "fit"}, {}, 1920, 1080)
    assert abs(lay.map_point(0.5, 0.5, bg_ps)[1] - 0.5) < 1e-9               # never the blurred copy
    crop = lay.panels({"type": "crop", "x": 0.2, "scale": 1.0}, {}, 1920, 1080)
    assert lay.map_point(0.95, 0.5, crop) is None                           # off screen
    # Zooms aim at their region on the Short; a missing region aims at the centre.
    fx_list = [{"type": "punch", "t": 1, "d": 1, "zoom": 1.5, "region": "face"},
               {"type": "push", "t": 3, "d": 2, "zoom": 1.3, "region": "chat"}, {"type": "sfx", "t": 2}]
    out, missing = short_visuals(fx_list, {"face": FACE, "game": GAME}, ps)
    assert isinstance(out[0], Visual) and out[0].cy > 0.6 and (out[1].cx, out[1].cy) == (0.5, 0.5)
    assert missing == {"chat"}


def test_graph_builds_a_1080x1920_picture():
    for layout in ({"type": "crop", "x": 0.5, "y": 0.5, "scale": 1.3},
                   {"type": "stack", "top": "game", "bottom": "face", "split": 0.55}, {"type": "fit"}):
        g = lay.graph(lay.panels(layout, {"game": GAME, "face": FACE}, 1920, 1080), 1920, 1080)
        assert g.endswith("format=yuv420p[base]") and "[0:v]" in g
    g = lay.graph(lay.panels({"type": "fit"}, {}, 1920, 1080), 1920, 1080)
    assert "boxblur" in g and "overlay=0:656" in g


# ── subtitles on a tall frame ────────────────────────────────────────────────

def test_wrap_goes_past_two_lines_only_when_asked():
    text = "this is a fairly long subtitle line that keeps going and going"
    assert len(lines.wrap(text)) == 2                                        # the long edit: as before
    short = lines.wrap(text, lines.SHORT_LINE, lines.SHORT_LINES)
    assert len(short) > 2 and all(lines.width(x) <= lines.SHORT_LINE for x in short)
    assert " ".join(short) == text


def test_ass_for_a_short_sizes_from_the_width_and_adds_the_title():
    a = lines.ass([(0, 1, "hello")], {"size": "normal", "position": "bottom"}, 1080, 1920,
                  title="*Big* news", title_until=4.5)
    clip_style = next(x for x in a.splitlines() if x.startswith("Style: Clip"))
    assert f",{round(0.065 * 1080)}," in clip_style and clip_style.endswith(f",{round(1920 * 0.25)},1")
    title = next(x for x in a.splitlines() if x.startswith("Dialogue: 1"))
    assert "0:00:04.50,Title" in title and "Big" in title
    landscape = lines.ass([(0, 1, "hello")], {"size": "normal"}, 1920, 1080)
    assert "Dialogue: 1" not in landscape and f",{round(0.065 * 1080)}," in landscape
    assert lines.frame_rules(1080, 1920)["vertical"] and not lines.frame_rules(1920, 1080)["vertical"]


def test_build_makes_shorter_cues_for_shorts():
    words = {"lines": [{"words": [[i * 0.25, i * 0.25 + 0.2, f" word{i}", 0.9] for i in range(18)]}]}
    assert len(lines.build(words, lines.SHORT_CUE)) > len(lines.build(words))


# ── render ───────────────────────────────────────────────────────────────────

@needs_ffmpeg
@pytest.mark.parametrize("layout", [{"type": "crop", "x": 0.3, "y": 0.5, "scale": 1.2, "title": ""},
                                    {"type": "stack", "top": "game", "bottom": "face", "split": 0.6, "title": ""},
                                    {"type": "fit", "title": "*Big* news"}])
def test_render_a_short_in_each_layout(tmp_path, layout):
    src = make_video(tmp_path / "src.mp4", 4)
    ps = lay.panels(layout, {"game": GAME, "face": FACE}, 320, 240)
    out = tmp_path / "short.mp4"
    r = render(EditConfig(loudness_lufs=0), src, [[0.5, 2.0, 0], [3.0, 3.5, 0]], out, fps=30,
               visuals=[Visual("punch", 1.0, 0.5, zoom=1.5, cx=0.5, cy=0.8)],
               subs=[{"t0": 0.6, "t1": 1.8, "text": "a subtitle on the short"}],
               sub_style={"on": True, "size": "normal", "position": "bottom"}, size=(lay.OUT_W, lay.OUT_H),
               layout=lay.graph(ps, 320, 240), title=layout["title"])
    v = probe(out)
    assert (v["width"], v["height"]) == (1080, 1920) and int(v["nb_frames"]) == 60
    assert abs(r.duration_s - 2.0) < 0.05


# ── the editor: kind=short ───────────────────────────────────────────────────

@needs_ffmpeg
def test_short_edit_save_render_and_media(site):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    # A long edit with an effect and a fixed subtitle line…
    client.post(f"/moment/{m}/edit", json={
        "segments": [[1, 9]], "effects": [{"type": "shake", "t": 2.0, "d": 0.5}],
        "subs": [{"t0": 1.5, "t1": 3, "text": "fixed line", "by": "you"}]})
    # …and a new Short starts from the whole clip with both.
    d = client.get(f"/api/moment/{m}/edit?kind=short").json()
    assert d["ok"] and d["kind"] == "short" and not d["saved"] and d["source"] == "whole"
    assert len(d["segments"]) == 1 and d["segments"][0][1] == pytest.approx(d["duration"], abs=0.05)
    assert [f["type"] for f in d["effects"]] == ["shake"] and d["subs"][0]["text"] == "fixed line"
    assert d["layout"]["type"] == "crop" and d["layout_info"]["out"] == [1080, 1920]
    assert d["sub_layout"]["vertical"] and d["render"]["state"] == "none" and d["other"]["long"] == "none"
    assert client.get(f"/api/moment/{m}/edit?kind=wide").status_code == 400

    body = {"kind": "short", "segments": [[2, 5]], "layout": {"type": "fit", "title": "Big news"}}
    r = client.post(f"/moment/{m}/edit", json=body).json()
    assert r["ok"] and r["render"]["state"] == "none"
    row = conn.execute("SELECT * FROM shorts WHERE moment_id = ?", (m,)).fetchone()
    assert json.loads(row["layout_json"])["title"] == "Big news" and json.loads(row["effects_json"])
    assert json.loads(conn.execute("SELECT segments_json FROM edits WHERE moment_id = ?", (m,)).fetchone()[0])
    long_segs = json.loads(conn.execute("SELECT segments_json FROM edits WHERE moment_id = ?", (m,)).fetchone()[0])
    assert len(long_segs) == 1 and long_segs[0][1] - long_segs[0][0] == 8                  # the long one untouched

    r = client.post(f"/moment/{m}/render", json=body).json()
    assert r["ok"] and r["render"]["state"] == "queued"
    v = conn.execute("SELECT video_id FROM moments WHERE id = ?", (m,)).fetchone()["video_id"]
    jobs.enqueue(conn, v, "analyze")
    assert jobs.claim(conn)["stage"] == "short"                                             # jumps the queue too
    conn.execute("UPDATE jobs SET status = 'pending' WHERE status = 'running'")
    conn.execute("DELETE FROM jobs WHERE stage = 'analyze' AND moment_id IS NULL")
    Worker(cfg, deps=deps).run(once=True)

    st = client.get(f"/api/moment/{m}/render?kind=short").json()["render"]
    assert st["state"] == "done" and Path(st["path"]).name == "short.mp4" and abs(st["length"] - 3.0) < 0.1
    v = probe(Path(st["path"]))
    assert (v["width"], v["height"]) == (1080, 1920)
    media = client.get(st["url"])
    assert media.status_code == 200 and media.headers["content-type"] == "video/mp4"
    assert "short.mp4" in client.get(f"/media/edit/{m}?kind=short&dl=1").headers["content-disposition"]
    assert client.get(f"/api/moment/{m}/render").json()["render"]["state"] == "none"       # edit.mp4 not made
    assert client.get(f"/media/edit/{m}").status_code == 404

    # Changing the layout (or a region a stack uses) makes it stale.
    client.post(f"/moment/{m}/edit", json=body | {"layout": {"type": "fit", "title": "Other"}})
    assert client.get(f"/api/moment/{m}/render?kind=short").json()["render"]["state"] == "stale"
    client.post(f"/moment/{m}/edit", json=body | {"layout": {"type": "stack", "top": "", "bottom": "face"}})
    client.post(f"/moment/{m}/render", json=body | {"layout": {"type": "stack", "top": "", "bottom": "face"}})
    Worker(cfg, deps=deps).run(once=True)
    assert client.get(f"/api/moment/{m}/render?kind=short").json()["render"]["state"] == "done"
    client.post(f"/moment/{m}/regions", json={"regions": [{"name": "face", "x": 0.6, "y": 0.4, "w": 0.3, "h": 0.5}],
                                             "kind": "short"})
    assert client.get(f"/api/moment/{m}/render?kind=short").json()["render"]["state"] == "stale"
