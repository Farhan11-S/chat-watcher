"""A Short's stacked pictures zoom on their own, layouts that change for a stretch, and
layers (a region of the clip over the picture, see-through)."""

import shutil

import pytest

from chatwatcher.config import EditConfig
from chatwatcher.edit import autoedit as ae
from chatwatcher.edit import layers as lyr
from chatwatcher.edit import layout as lay
from chatwatcher.edit import motion
from chatwatcher.edit import pieces as pc
from chatwatcher.edit.render import render
from chatwatcher.edit.validate import clean_effects, short_visuals

from test_autoedit import CLIP
from test_clip import make_video
from test_shorts import probe

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
FACE = {"x": 0.04, "y": 0.3, "w": 0.14, "h": 0.35}
GAME = {"x": 0.3, "y": 0.0, "w": 0.4, "h": 1.0}
CHAT = {"x": 0.1, "y": 0.2, "w": 0.18, "h": 0.6}
BOXES = {"face": FACE, "game": GAME, "chat": CHAT}
STACK = {"type": "stack", "top": "game", "bottom": "face", "split": 0.6}


# ── zooms in a stack stay in their panel ─────────────────────────────────────

def test_stack_zoom_stays_in_the_panel_showing_its_region():
    ps = lay.panels(STACK, BOXES, 1920, 1080)
    fx = [{"type": "punch", "t": 1, "d": 1, "region": "face", "zoom": 1.5},
          {"type": "push", "t": 0, "d": 3, "region": "", "zoom": 1.1},
          {"type": "push", "t": 2, "d": 1, "region": "game", "zoom": 1.3},
          {"type": "shake", "t": 1, "d": 0.5, "strength": 2}]
    face, centre, game, shake = short_visuals(fx, BOXES, ps)[0]
    assert face.box == ps[1].dst and 0 < face.cx < 1 and abs(face.cy - 0.5) < 0.01   # inside the bottom panel
    assert centre.box == ps[0].dst and (centre.cx, centre.cy) == (0.5, 0.5)          # the centre: the top panel
    assert game.box == ps[0].dst
    assert shake.box is None                                                         # shakes move the whole picture
    vf = motion.video_filter([face, centre, shake], 30, 4, (1080, 1920))
    assert vf.count("split[") == 2 and "crop=1080:1152:0:0" in vf and "crop=1080:768:0:1152" in vf
    assert vf.count("perspective=") == 3                                             # two panels + the shake


def test_one_picture_still_zooms_the_whole_frame():
    ps = lay.panels({"type": "crop", "x": 0.5, "y": 0.5, "scale": 1.0}, BOXES, 1920, 1080)
    (v,), _ = short_visuals([{"type": "punch", "t": 1, "d": 1, "region": "game", "zoom": 1.5}], BOXES, ps)
    assert v.box is None
    assert "split[" not in motion.video_filter([v], 30, 2)


# ── frame shots that stack two regions ───────────────────────────────────────

def test_stack_shot_is_checked_and_drawn_as_a_stack():
    fx = clean_effects([{"type": "frame", "t": 1, "d": 20, "top": "Chat", "bottom": "face", "split": 0.9},
                        {"type": "frame", "t": 3, "d": 2, "region": "game"}], 30, set())
    st, one = fx
    assert (st["top"], st["bottom"], st["split"], st["region"]) == ("chat", "face", lay.SPLIT_RANGE[1], "")
    assert lay.is_stack_shot(st) and not lay.is_stack_shot(one)
    ps = lay.shot_panels(st, BOXES, 1920, 1080)
    assert len(ps) == 2 and ps[0].dst == (0.0, 0.0, 1.0, 0.7)
    assert lay.shot_panels({"top": "nowhere", "bottom": "face"}, BOXES, 1920, 1080) is None
    assert lay.shot_panels({"top": "", "bottom": "face"}, BOXES, 1920, 1080)[0].src[3] == 1.0   # "" = whole frame
    assert lay.shot_regions(st) == {"chat", "face"}


def test_ai_layouts_become_frame_shots_and_steer_the_camera():
    ps = pc.build(CLIP, 20.0)
    segs = [[1.9, 4.2, 0.0], [8.9, 12.0, 0.0]]
    scenes = ae.layout_effects(["p4 | chat / face | 55", "p2 | full", "p1 | nowhere / face", "p2 | whole / face"],
                               ps, segs, BOXES)
    stacked = [f for f in scenes if f.get("top") == "chat"]
    assert stacked and stacked[0]["split"] == 0.55 and stacked[0]["t"] == 8.9 and stacked[0]["by"] == "claude"
    assert any(f["region"] == "full" for f in scenes)
    assert any(f.get("bottom") == "face" and f.get("top") == "" for f in scenes)   # whole frame over her face
    assert not any(f.get("top") == "nowhere" for f in scenes)
    # In the chat / face stretch, a shot on chat needs no frame shot of its own; on game it does.
    cam = ae.camera_track(["p4 | chat | close | cut"], ps, segs, BOXES, True, {"face", "game"}, scenes)
    assert not [f for f in cam if f["type"] == "frame"]
    cam = ae.camera_track(["p4 | game | close | cut"], ps, segs, BOXES, True, {"face", "game"}, scenes)
    assert [f["region"] for f in cam if f["type"] == "frame"] == ["game"]


# ── layers ───────────────────────────────────────────────────────────────────

def test_layer_effect_is_checked():
    (f,) = clean_effects([{"type": "layer", "t": 2, "d": 999, "region": "Chat", "at": "nowhere!", "size": 9,
                           "opacity": 0.01, "dx": 5}], 30, set())
    assert (f["region"], f["at"], f["size"], f["opacity"], f["dx"]) == ("chat", "middle", lyr.SIZE_RANGE[1],
                                                                        lyr.OPACITY_RANGE[0], 1.0)
    assert f["d"] == lyr.D_LIMITS[2]
    with pytest.raises(Exception):
        clean_effects([{"type": "layer", "t": 2, "d": 3}], 30, set())          # nothing to show


def test_layers_are_placed_and_filtered():
    fx = [{"type": "layer", "t": 1.0, "d": 2.0, "region": "chat", "at": "middle", "dx": 0, "dy": 0.1,
           "size": 0.5, "opacity": 0.6}, {"type": "layer", "t": 1.0, "d": 2.0, "region": "gone"}]
    (p,) = lyr.placed(fx, BOXES, None, (1920, 1080))
    assert (p.x, p.y, p.w, p.opacity) == (0.5, 0.6, 0.5, 0.6)
    assert abs(p.aspect - CHAT["w"] * 1920 / (CHAT["h"] * 1080)) < 1e-9
    items = lyr.local([p], 2.0, 5.0)
    assert items == [(p, 0.0, 1.0)]                       # started before this part: shows from its start
    graph, out = lyr.filter_graph("[m]", "[src]", items, 1080, 1920, start=2.0)
    assert out == "[lyo0]" and "colorchannelmixer=aa=0.600" in graph
    assert "fade=t=in" not in graph and "fade=t=out" in graph   # no fade-in in the middle of it


def test_ai_layers():
    segs = [[1.0, 6.0, 0.0]]
    got = ae.layer_effects(["2 | 3 | chat | middle | 60 | chat spams LMAO", "2 | 3 | nowhere | middle | 50 | x",
                            "9 | 3 | chat | middle | 50 | outside the cut"], segs, BOXES, (1920, 1080), (1080, 1920))
    assert len(got) == 1 and got[0]["opacity"] == 0.6 and got[0]["region"] == "chat" and got[0]["why"] == "chat spams LMAO"
    assert lyr.SIZE_RANGE[0] <= got[0]["size"] <= 0.9
    assert ae.schema(3, 3, short=True)["properties"]["layouts"] and "layouts" not in ae.schema(3, 3)["properties"]


@needs_ffmpeg
def test_render_a_stack_with_panel_zoom_and_a_layer(tmp_path):
    src = make_video(tmp_path / "src.mp4", 4)
    boxes = {"game": GAME, "face": FACE, "chat": CHAT}
    ps = lay.panels(STACK, boxes, 320, 240)
    fx = [{"type": "punch", "t": 1.0, "d": 0.5, "region": "face", "zoom": 1.5},
          {"type": "layer", "t": 0.8, "d": 1.0, "region": "chat", "at": "middle", "size": 0.4, "opacity": 0.5}]
    vis, _ = short_visuals(fx, boxes, ps)
    out = tmp_path / "short.mp4"
    r = render(EditConfig(loudness_lufs=0), src, [[0.5, 2.0, 0], [3.0, 3.5, 0]], out, fps=30, visuals=vis,
               size=(lay.OUT_W, lay.OUT_H), layout=lay.graph(ps, 320, 240),
               layers=[lyr.placed(fx, boxes, ps, (320, 240))] * 2)
    v = probe(out)
    assert (v["width"], v["height"]) == (1080, 1920) and int(v["nb_frames"]) == 60
    assert abs(r.duration_s - 2.0) < 0.05
