"""Caption styles: the bubble look, and the effect / font / place the AI edit (or you) puts
on stretches of the captions, drawn by libass with the shipped fonts."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from chatwatcher.edit import autoedit as ae
from chatwatcher.edit import captions as cap
from chatwatcher.edit import layout as lay
from chatwatcher.edit import pieces as pc
from chatwatcher.edit.service import clean_effects, clip_ref
from chatwatcher.worker import Worker

from test_direct import WORDS, FakeDirector, answer
from test_web import mid, site  # noqa: F401

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")

STACK = [lay.Panel((0, 0, 1, 1), (0, 0, 1, 0.6)), lay.Panel((0.7, 0.6, 0.3, 0.4), (0, 0.6, 1, 0.4))]
BOXES = {"face": {"x": 0.7, "y": 0.6, "w": 0.3, "h": 0.4}, "game": {"x": 0, "y": 0, "w": 0.7, "h": 0.6}}


def test_fonts_ship_with_the_app():
    names = {p.name for p in cap.font_files()}
    assert set(cap.FONT_FILES.values()) | {cap.CJK_FILE} <= names
    assert (cap.FONT_DIR / "OFL.txt").is_file()


def test_bubble_colours():
    fill = cap.deep("FF8FC8")                       # a pastel voice colour → a deep pink, same hue
    h, l, s = cap._hls(fill)
    assert l <= 0.385 and s >= 0.59 and abs(h - cap._hls("FF8FC8")[0]) < 0.01
    assert cap.deep("FFFFFF") == cap.deep(None) == cap.DEFAULT_FILL
    assert cap._hls(cap.ring(fill))[1] < l / 2      # the ring is much darker
    assert cap.accent(fill) != cap.accent(cap.deep("7FD3FF"))   # pink fill → blue highlights


def test_effects_check_caption_styles():
    fx = clean_effects([{"type": "caption", "t": 1, "d": 999, "fx": "wave", "font": "angry", "at": "Face "},
                        {"type": "caption", "t": 2, "fx": "spin", "font": "comic sans", "at": "seam"},
                        {"type": "caption", "t": 3}], 10, set())
    assert [(f["fx"], f["font"], f["at"], f["d"]) for f in fx] == [
        ("wave", "angry", "face", cap.D_LIMITS[2]), ("pop", "", "seam", cap.D_LIMITS[0]), ("pop", "", "", cap.D_LIMITS[0])]


def test_places():
    assert cap.seam_y(STACK) == 0.6 and cap.seam_y(None) == 0.5
    assert cap.anchor("middle", BOXES, STACK) == (0.5, 0.5)
    x, y = cap.anchor("face", BOXES, STACK)          # low on the face panel
    assert x == pytest.approx(0.5) and y == pytest.approx(0.6 + cap.LOW * 0.4)
    assert cap.anchor("face", BOXES, None) == pytest.approx((0.85, 0.6 + cap.LOW * 0.4))   # 16:9: the clip's own frame
    assert cap.anchor("chat", BOXES, STACK) is None and cap.anchor("top", BOXES, STACK) is None
    marks = cap.placed([{"type": "caption", "t": 5, "d": 2, "fx": "shake", "font": "angry", "at": "face"},
                        {"type": "caption", "t": 1, "d": 10, "fx": "wave", "font": "", "at": "top"},
                        {"type": "text", "t": 1, "d": 1}], BOXES, STACK)
    assert [m.fx for m in marks] == ["wave", "shake"] and marks[0].xy is None and marks[1].xy is not None
    assert cap.mark_for(5.5, marks).fx == "shake" and cap.mark_for(2, marks).fx == "wave"
    assert cap.mark_for(12, marks) is None


def _events(cues, marks, style, w=1080, h=1920):
    return cap.events(cap.local(cues, 0, 20, marks), style, w, h)


def test_bubble_look_as_ass():
    style = {"look": "bubble", "size": "large", "position": "top"}
    ev = _events([{"t0": 1, "t1": 2, "text": "oke *SERIUS*", "color": "FFB35C"}], [], style)
    assert len(ev) == 2                                  # the dark ring under the white outline
    ring, front = ev
    assert ring.startswith("Dialogue: 0,") and front.startswith("Dialogue: 1,")
    assert "\\fnFredoka" in front and "\\3c&HFFFFFF&" in front and "\\an8" in front
    assert f"\\1c{cap._ass_color(cap.accent(cap.deep('FFB35C')))}" in front     # the highlight's colour…
    assert f"\\fscx{round(cap.HI_SCALE * 100)}" in front                         # …and size
    assert "\\fscx40" in front                                                     # springs in
    # Japanese: the rounded CJK font; classic look without styles isn't drawn here at all
    jp = _events([{"t0": 1, "t1": 2, "text": "ここ草", "color": None}], [], style)
    assert f"\\fn{cap.CJK_FONT}" in jp[1]
    assert cap.styled({"look": "classic"}, []) is False and cap.styled({"look": "classic"}, [object()]) is True


def test_effects_fonts_and_places_as_ass():
    style = {"look": "bubble", "size": "normal", "position": "bottom"}
    cues = [{"t0": 1, "t1": 2, "text": "ANDING", "color": None}, {"t0": 3, "t1": 4, "text": "lala", "color": None},
            {"t0": 5, "t1": 6, "text": "huh", "color": None}, {"t0": 7, "t1": 8, "text": "BOOM", "color": None}]
    marks = cap.placed([{"type": "caption", "t": 0.9, "d": 1, "fx": "shake", "font": "angry", "at": "face"},
                        {"type": "caption", "t": 2.9, "d": 1, "fx": "wave", "font": "", "at": "seam"},
                        {"type": "caption", "t": 4.9, "d": 1, "fx": "bounce", "font": "scary", "at": "top"},
                        {"type": "caption", "t": 6.9, "d": 1, "fx": "grow", "font": "loud", "at": "middle"}], BOXES, STACK)
    ev = _events(cues, marks, style)
    shake = [e for e in ev if "ANDING" in e]
    assert len(shake) > 20 and all("\\fnRubik Wet Paint" in e and "\\an5" in e for e in shake)
    assert len({re.search(r"\\pos\(([^)]*)\)", e)[1] for e in shake}) > 5          # it moves about
    wave = [e for e in ev if "\\org" in e]
    assert len(wave) == 2 and all(f"\\pos(540,{round(0.6 * 1920)})" in e for e in wave)        # on the seam
    assert any("\\move(" in e and "\\fnCreepster" in e and "\\an8" in e for e in ev)
    assert any("\\fnBangers" in e and "\\fscx125" in e for e in ev)


def test_caption_sizes():
    fx = clean_effects([{"type": "caption", "t": 1, "size": "huge"}, {"type": "caption", "t": 2, "size": "gigantic"}],
                       10, set())
    assert [f["size"] for f in fx] == ["huge", ""]
    style = {"look": "bubble", "size": "normal", "position": "bottom"}
    text = "no no no no no no no"                       # fits one line at the usual size
    marks = cap.placed([{"type": "caption", "t": 0.9, "d": 1, "fx": "none", "font": "", "at": "", "size": "huge"},
                        {"type": "caption", "t": 2.9, "d": 1, "fx": "none", "font": "", "at": "", "size": "small"}],
                       BOXES, STACK)
    ev = _events([{"t0": 1, "t1": 2, "text": text, "color": None}, {"t0": 3, "t1": 4, "text": text, "color": None},
                  {"t0": 5, "t1": 6, "text": text, "color": None}], marks, style)
    front = [e for e in ev if e.startswith("Dialogue: 1,")]
    fs = [int(re.search(r"\\fs(\d+)", e)[1]) for e in front]
    assert abs(fs[0] - fs[2] * cap.SIZES["huge"]) <= 1 and abs(fs[1] - fs[2] * cap.SIZES["small"]) <= 1
    assert "\\N" in front[0] and "\\N" not in front[2]          # huge text wraps so it still fits across


def test_captions_at_an_edge_stack():
    style = {"look": "bubble", "size": "normal", "position": "bottom"}
    ev = _events([{"t0": 1, "t1": 3, "text": "first", "color": "FF8FC8"},
                  {"t0": 1.5, "t1": 3, "text": "second", "color": "7FD3FF"}], [], style)
    ys = [int(re.search(r"\\pos\(\d+,(\d+)\)", e)[1]) for e in ev if e.startswith("Dialogue: 1,")]
    assert ys[0] > ys[1]                                  # the later one above the first


def test_caption_styles_from_claude():
    ps = pc.build(WORDS, 20.0)
    segs = [[0.0, 20.0, 0.0]]
    talk = [p for p in ps if p.kind == "talk"]
    first = ps.index(talk[0]) + 1
    items = [f"p{first} | shake | angry | face | she's furious",
             "9.0-11.0 | wave | round | seam | teasing",
             "p1 | spin | comic | nowhere | unknowns fall back",
             "30-31 | pop | | | outside the clip"]
    out = ae.caption_effects(items, ps, segs, BOXES, 20.0)
    assert [(f["fx"], f["font"], f["at"]) for f in out] == [("shake", "angry", "face"), ("wave", "", "seam"),
                                                           ("pop", "", "")]
    assert out[0]["t"] == pytest.approx(talk[0].w0 - ae.CAPTION_LEAD) and out[0]["by"] == "claude"
    assert out[1]["t"] == 9.0 and out[1]["d"] == 2.0
    assert out[0]["size"] == "" and out[0]["why"] == "she's furious"     # no size given: the why still lands
    sized = ae.caption_effects(["9.0-11.0 | bounce | | | huge | the one word", "9.0-11.0 | pop | | | usual | plain",
                                "9.0-11.0 | pop | | | tiny | not a size: kept as the why"], ps, segs, BOXES, 20.0)
    assert [(f["size"], f["why"]) for f in sized] == [("huge", "the one word"), ("", "plain"),
                                                      ("", "tiny | not a size: kept as the why")]


def test_pieces_say_who_talks():
    words = {"lines": [{"words": [[1.0, 1.5, " hi", 0.9, 0], [1.5, 2.0, " there.", 0.9, 0]]},
                       {"words": [[5.0, 5.5, " no", 0.9, 1], [5.5, 6.0, " way.", 0.9, 1]]}]}
    ps = pc.build(words, 10.0)
    pc.name_voices(ps, words, {"0": {"name": "Elaine"}})
    talk = [p for p in ps if p.kind == "talk"]
    assert [p.who for p in talk] == ["Elaine", "person 2"]
    assert 'Elaine: "hi there."' in talk[0].line()
    solo = pc.build(WORDS, 20.0)
    pc.name_voices(solo, WORDS, {})
    assert all(p.who == "" for p in solo)


def test_ai_edit_styles_captions(site):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    (clip_ref(conn, m).folder / "words.json").write_text(json.dumps(WORDS))
    a = answer(["p2-p4"]) | {"texts": [], "spots": [], "captions": ["9.0-11.0 | shake | angry | middle | she died"]}
    fake = FakeDirector(a)
    deps.claude = fake
    d = client.post(f"/moment/{m}/autoedit", json={"segments": [[0, 20, 0]], "kind": "short"}).json()
    assert d["ok"] and "1 caption style" in d["reply"]
    assert "captions" in fake.calls[0]["schema"]["required"]
    after = client.get(f"/api/moment/{m}/edit?kind=short").json()
    caps = [f for f in after["effects"] if f["type"] == "caption"]
    assert [(f["fx"], f["font"], f["at"], f["by"]) for f in caps] == [("shake", "angry", "middle", "claude")]
    assert after["subs_style"]["look"] == "bubble"
    assert after["sub_layout"]["captions"]["fonts"] == cap.FONTS


def test_ai_edit_gives_your_own_lines_the_look(site):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    (clip_ref(conn, m).folder / "words.json").write_text(json.dumps(WORDS))
    mine = [{"id": "s1", "t0": 2.0, "t1": 4.0, "text": "So this is EVIL.", "by": "you"}]
    assert client.post(f"/moment/{m}/edit", json={"segments": [[0, 20, 0]], "kind": "short", "subs": mine,
                                                  "subs_style": {"on": True, "mode": "lines"}}).json()["ok"]
    deps.claude = FakeDirector(answer(["p2-p4"]) | {"texts": [], "spots": [], "captions": []})
    assert client.post(f"/moment/{m}/autoedit", json={"segments": [[0, 20, 0]], "kind": "short"}).json()["ok"]
    after = client.get(f"/api/moment/{m}/edit?kind=short").json()
    assert after["subs_source"] == "saved" and [c["text"] for c in after["subs"]] == ["So this is EVIL."]
    assert after["subs_style"]["look"] == "bubble" and after["subs_style"]["mode"] == "lines"


def colour_share(video: Path, t: float, rgb: tuple[int, int, int], rows: tuple[int, int]) -> float:
    """How much of these rows of the frame at t (160×90) is near this colour."""
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(video), "-frames:v", "1",
                          "-vf", "scale=160:90", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    n = hit = 0
    for y in range(*rows):
        for x in range(160):
            k = (y * 160 + x) * 3
            n += 1
            hit += all(abs(out[k + i] - rgb[i]) < 40 for i in range(3))
    return hit / n


@needs_ffmpeg
def test_bubble_captions_are_rendered(site):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    (clip_ref(conn, m).folder / "words.json").write_text(json.dumps(WORDS))
    purple = tuple(int(cap.DEFAULT_FILL[i:i + 2], 16) for i in (0, 2, 4))
    edit = {"segments": [[1, 5, 0]], "source": "long", "subs": "auto",
            "subs_style": {"on": True, "size": "large", "position": "bottom", "mode": "words", "look": "bubble"},
            "effects": [{"type": "caption", "t": 1.9, "d": 3, "fx": "none", "font": "", "at": "middle"}]}
    assert client.post(f"/moment/{m}/render", json=edit).json()["ok"]
    Worker(cfg, deps=deps).run(once=True)
    job = conn.execute("SELECT status, message FROM jobs WHERE moment_id = ? AND stage = 'render'", (m,)).fetchone()
    assert job["status"] == "done", job["message"]
    out = clip_ref(conn, m).out_path("long")
    before, during = colour_share(out, 0.3, purple, (35, 55)), colour_share(out, 1.7, purple, (35, 55))
    assert during > before + 0.01, (before, during)       # "So this" in purple, in the middle
