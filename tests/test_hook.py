"""The Short's hook: shown for its first seconds only, in a style you or the AI edit pick."""

import shutil

import pytest

from chatwatcher.config import EditConfig
from chatwatcher.edit import autoedit as ae
from chatwatcher.edit import hook
from chatwatcher.edit import layout as lay
from chatwatcher.edit.render import render

from test_clip import make_video
from test_shorts import probe

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def test_clean_checks_every_field():
    st = hook.clean({"s": 99, "font": "LOUD", "fx": "explode", "look": "box", "color": "teal", "at": "upper",
                     "size": "huge", "tilt": -40})
    assert st["s"] == hook.HOOK_S[2] and st["font"] == "loud" and st["fx"] == hook.DEFAULT["fx"]
    assert (st["look"], st["color"], st["at"], st["size"], st["tilt"]) == ("box", "white", "upper", "huge", -hook.TILT)
    assert hook.clean(None) == hook.DEFAULT
    assert lay.clean({"title": "x"}, lay.default({}, None))["hook"] == hook.DEFAULT     # every layout has one


def test_ai_hook_style_parses_and_reads_back():
    st = hook.parse("2.5s | default | slam | bubble | pink | blue | red | middle | big | -6°")
    assert (st["s"], st["font"], st["fx"], st["look"], st["color"], st["accent"], st["at"], st["size"], st["tilt"]) == \
        (2.5, "", "slam", "bubble", "pink", "blue", "middle", "big", -6.0)
    assert hook.parse(hook.line(st)) == st
    assert hook.parse("") == hook.DEFAULT and hook.parse("nonsense | | |") == hook.DEFAULT
    schema = ae.schema(3, 3, short=True)
    assert "hook_style" in schema["required"] and "hook_style" not in ae.schema(3, 3)["properties"]


def test_events_only_in_the_first_seconds():
    st = hook.clean({"s": 3, "fx": "pop"})
    first = hook.events("She *KICKED* it", st, 1080, 1920, into=0.0, length=2.0)
    assert first and all(",Callout," in e for e in first) and "\\fad" not in "".join(first)   # goes on into the next part
    second = hook.events("She *KICKED* it", st, 1080, 1920, into=2.0, length=4.0)
    assert second and "0:00:01.00" in second[0] and "\\fad(0,200)" in second[0]           # the last second, then fades
    assert hook.events("She *KICKED* it", st, 1080, 1920, into=3.5, length=2.0) == []      # over
    assert hook.events("", st, 1080, 1920, into=0.0, length=2.0) == []


def test_looks_and_motions():
    text = "1 HP. *One* hit left."
    bubble = hook.events(text, hook.clean({"look": "bubble"}), 1080, 1920, 0.0, 5.0)
    assert len(bubble) == 2                                         # the dark ring under the white outline
    box = hook.events(text, hook.clean({"look": "box", "box": "red"}), 1080, 1920, 0.0, 5.0)
    assert ",Label," in box[0] and "\\3c&H303BFF&" in box[0]
    shake = hook.events(text, hook.clean({"fx": "shake", "s": 2}), 1080, 1920, 0.0, 5.0)
    assert len(shake) > 20 and not any("\\fad" in e for e in shake)    # each step fades by its own alpha
    assert "\\alpha&H" in shake[-1]
    slam = hook.events(text, hook.clean({"fx": "slam", "tilt": 6}), 1080, 1920, 0.0, 5.0)[0]
    assert "\\fscx260" in slam and "\\frz6.0" in slam and "\\t(0,160,\\alpha&H00&)" in slam


@needs_ffmpeg
def test_render_a_short_with_a_hook(tmp_path):
    src = make_video(tmp_path / "src.mp4", 4)
    ps = lay.panels({"type": "crop", "x": 0.5, "y": 0.5, "scale": 1.0}, {}, 320, 240)
    out = tmp_path / "short.mp4"
    render(EditConfig(loudness_lufs=0), src, [[0.5, 1.5, 0], [2.0, 3.5, 0]], out, fps=30, size=(lay.OUT_W, lay.OUT_H),
           layout=lay.graph(ps, 320, 240), title="*Big* news",
           hook=hook.clean({"s": 1.5, "font": "loud", "fx": "slam", "look": "box"}), sub_style={"on": False})
    v = probe(out)
    assert (v["width"], v["height"]) == (1080, 1920) and int(v["nb_frames"]) == 75
