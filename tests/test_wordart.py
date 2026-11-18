"""Word art: one word of a caption in its own look (a red box, slime, a glow…)."""

import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from chatwatcher.edit import autoedit as ae
from chatwatcher.edit import captions as cap
from chatwatcher.edit import wordart as wa
from chatwatcher.edit.service import EditError
from chatwatcher.edit.validate import clean_effects
from chatwatcher.subs import lines
from chatwatcher.worker import Worker

from test_web import mid, site  # noqa: F401  (fixture)

UTIL_JS = Path(__file__).resolve().parents[1] / "src/chatwatcher/web/static/editor/util.js"
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def art(word, t=1.0, style="box"):
    return wa.placed([{"type": "word", "t": t, "word": word, "style": style}])[0]


def test_clean_words():
    a, b = clean_effects([{"type": "word", "t": 1, "word": " *so*  very  sticky 🔥 indeed", "style": "slime", "color": "nope"},
                          {"type": "word", "t": 2, "word": "x" * 50, "style": "weird", "color": "pink", "d": 99}], 10, set())
    assert (a["word"], a["style"], a["color"]) == ("so very sticky", "slime", "")
    assert (len(b["word"]), b["style"], b["color"], b["d"]) == (wa.MAX_CHARS, "box", "pink", wa.D_LIMITS[2])
    with pytest.raises(EditError, match="needs the word"):
        clean_effects([{"type": "word", "t": 1, "word": " ** "}], 10, set())


def test_find_and_pieces():
    assert wa.find("there was *blood* everywhere", "BLOOD") == (8, 13)
    assert wa.find("the bloodbath", "blood") == (3, 8)                       # no whole word: anywhere
    assert wa.find("ちょっと待って", "待って") == (4, 7)
    assert wa.find("nothing here", "blood") is None
    sp = [(*wa.find("so it is very sticky ew", "very sticky"), a := art("very sticky", style="slime"))]
    rows = ["so it is", "very sticky ew"]
    assert wa.pieces(rows, sp) == [[("so it is", False, None)],
                                   [("VERY STICKY", False, a), (" ew", False, None)]]
    # a phrase broken over two rows: the space at the break isn't part of it
    rows = ["she said very", "sticky ew"]
    sp = [(*wa.find(" ".join(rows), "very sticky"), a)]
    assert wa.pieces(rows, sp)[0][-1] == ("VERY", False, a) and wa.pieces(rows, sp)[1][0] == ("STICKY", False, a)
    # highlights stay; the word in one is still dressed
    b = art("blood")
    got = wa.pieces(["was *my blood* ok"], [(*wa.find("was *my blood* ok", "blood"), b)])
    assert got == [[("was ", False, None), ("my ", True, None), ("BLOOD", True, b), (" ok", False, None)]]


def test_on_cue_takes_the_caption_she_says_it_in():
    arts = [art("blood", 0.98), art("blood", 5.0), art("nope", 1.5), art("there", 1.2)]
    got = wa.on_cue("there was blood", 1.0, 3.0, arts)
    assert [(g0, g1, a.word, a.t) for g0, g1, a in got] == [(8, 13, "blood", 0.98), (0, 5, "there", 1.2)]


def test_events_draw_the_word_and_come_back():
    cue = {"t0": 2.0, "t1": 5.0, "text": "there was blood everywhere"}
    marks = cap.placed([{"type": "word", "t": 3.0, "word": "blood", "style": "box", "color": "yellow"},
                        {"type": "word", "t": 2.5, "word": "everywhere", "style": "slime"}], {}, None)
    assert cap.styled({"look": "classic"}, marks)                          # word art alone draws captions here
    shown = cap.local([cue], 1.0, 10.0, marks)
    assert [a.word for _g0, _g1, a in shown[0].arts] == ["everywhere", "blood"]
    ev = cap.events(shown, {"look": "classic"}, 1080, 1920)
    assert len(ev) == 1
    text = ev[0]
    assert "\\rBox" in text and "\\3c&H00D6FF&" in text and "\\1c&H151515&" in text   # black on a yellow box
    assert "\\hBLOOD\\h" in text and "\\fnRubik Wet Paint" in text and "EVERYWHERE" in text
    assert "\\t(1000,1090,\\fscx146" in text                                 # springs when she says it, 1 s in
    assert "\\t(500,590," in text
    one = cap.events(cap.local([{"t0": 0, "t1": 3, "text": "blood is everywhere"}], 0, 3,
                               cap.placed([{"type": "word", "t": 0.5, "word": "blood", "style": "glow"}], {}, None)),
                     {"look": "classic"}, 1080, 1920)[0]
    assert "}blood{\\r\\fn" in one                                            # the caption's own look comes back
    bubble = cap.events(shown, {"look": "bubble"}, 1080, 1920)
    assert len(bubble) == 2 and all("\\rBox" in e for e in bubble)          # the dark frame and the box


def test_a_word_not_in_the_caption_draws_nothing():
    marks = cap.placed([{"type": "word", "t": 3.0, "word": "nope", "style": "box"}], {}, None)
    shown = cap.local([{"t0": 2.0, "t1": 5.0, "text": "there was blood"}], 0, 10, marks)
    assert shown[0].arts == [] and "\\rBox" not in "".join(cap.events(shown, {"look": "bubble"}, 1080, 1920))


def test_preview_splits_like_the_render():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    js = UTIL_JS.read_text(encoding="utf-8")
    start, end = js.index("const WIDE = "), js.index("export const curSub = ")
    code = js[start:end].replace("\nexport ", "\n")
    rnd = random.Random(5)
    vocab = ["Okay,", "sorry", "*blood*", "I", "was", "really", "sticky", "ちょっと", "待って、", "so", "pretty!", "the", "very"]
    words = ["blood", "sticky", "pretty", "so pretty", "待って", "very sticky", "really", "the"]
    cases = []
    for _ in range(300):
        text = " ".join(rnd.choice(vocab) for _ in range(rnd.randint(1, 12)))
        picks = rnd.sample(words, rnd.randint(1, 3))
        cases.append([text, picks, lines.wrap(text, 14, 4)])
    script = (f"const S = {{ D: {{ sub_layout: {{ max_line: 38 }} }} }};\n{code}\n"
              f"const CASES = {json.dumps(cases, ensure_ascii=False)};\n"
              "console.log(JSON.stringify(CASES.map(([text, picks, rows]) => {"
              " const spans = []; picks.forEach((w, i) => { const at = artFind(text, w);"
              "  if (at && !spans.some(([a, b]) => a < at[1] && at[0] < b)) spans.push([at[0], at[1], { n: i, upper: i % 2 === 0 }]); });"
              " return artPieces(rows, spans).map(r => r.map(([t, h, a]) => [t, h, a ? a.n : null])); })));")
    got = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout)
    for (text, picks, rows), js_rows in zip(cases, got):
        spans = []
        for i, w in enumerate(picks):
            at = wa.find(text, w)
            if at and not any(a < at[1] and at[0] < b for a, b, _ in spans):
                spans.append((*at, wa.Art(1, w, "box", "FFFFFF", i % 2 == 0, 1, "")))
        want = [[[t, h, picks.index(a.word) if a else None] for t, h, a in r] for r in wa.pieces(rows, spans)]
        assert js_rows == want, (text, picks)


def test_ai_words():
    heard = {"lines": [{"words": [[9.6, 9.9, " there"], [10.2, 10.6, " Blood,"], [30.0, 30.4, " blood"]]}]}
    got = ae.word_effects(["10 | blood | box | yellow | the joke", "11 | sticky | weird | | x", "99 | late | box",
                           "nope | x | box", "10.1 | blood | glow"], [[0, 20, 0]], heard)
    assert [(w["t"], w["word"], w["style"], w["color"]) for w in got] == [(10.2, "blood", "box", "yellow"),
                                                                         (11.0, "sticky", "box", "")]
    assert got[0]["by"] == "claude" and got[0]["why"] == "the joke"
    assert "words" in ae.schema(3, 2)["required"]
    assert 'word art box on "blood"' in ae.placed_line({"type": "word", "t": 10.2, "word": "blood", "style": "box"})


@needs_ffmpeg
def test_word_art_in_the_editor_and_render(site):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    e = client.get(f"/api/moment/{m}/edit").json()
    assert set(e["word_info"]["styles"]) == set(wa.STYLES) and e["visual"]["types"]["word"]
    seg = {"segments": [[2, 8, 0]], "source": "long"}
    subs = [{"id": "a", "t0": 3.0, "t1": 6.0, "text": "there was blood everywhere"}]
    fx = [{"id": "u1", "type": "word", "t": 4.0, "word": "blood", "style": "slime"}]
    assert client.post(f"/moment/{m}/edit", json=seg | {"effects": fx, "subs": subs}).json()["ok"]
    client.post(f"/moment/{m}/render", json=seg | {"effects": fx, "subs": subs})
    Worker(cfg, deps=deps).run(once=True)
    assert client.get(f"/api/moment/{m}/render").json()["render"]["state"] == "done"
    info = json.loads(conn.execute("SELECT rendered_json FROM edits WHERE moment_id = ?", (m,)).fetchone()[0])
    assert info["visuals"] >= 1
