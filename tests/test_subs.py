import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from chatwatcher.config import EditConfig
from chatwatcher.edit.render import render
from chatwatcher.subs import lines
from chatwatcher.subs.lines import SubsError, build, clean, on_edit, wrap
from chatwatcher.worker import Worker

from test_web import mid, site  # noqa: F401  (fixture)

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
EDITOR_JS = Path(__file__).resolve().parents[1] / "src/chatwatcher/web/static/editor.js"


def words(*spoken):
    """words.json from ("text", start, seconds per word) phrases."""
    out = []
    for text, start, per in spoken:
        ws, t = [], start
        for k, w in enumerate(text.split(" ")):
            ws.append([round(t, 3), round(t + per * 0.9, 3), (" " if k else "") + w, 0.9])
            t += per
        out.append({"start": start, "end": t, "text": text, "words": ws})
    return {"model": "fake", "language": "en", "lines": out}


def test_cues_break_at_pauses_sentences_and_length():
    data = words(("Good thing I put a limiter in there.", 1.0, 0.25),
                 ("Okay. Are you okay? Are you deaf?", 4.0, 0.3),     # a pause first: a new cue
                 ("It was loud, but there was a limiter, right, and then I said that it was really fine actually", 9.0, 0.2))
    cues = build(data)
    texts = [c["text"] for c in cues]
    assert texts[0] == "Good thing I put a limiter in there."
    assert texts[1] == "Okay. Are you okay?"                       # "Okay." alone waits for company
    assert texts[2] == "Are you deaf?"
    assert all(lines.width(t) <= lines.MAX_CUE for t in texts) and len(texts) == 5
    assert texts[3].endswith(",")                                  # a long one breaks after a comma
    for a, b in zip(cues, cues[1:]):
        assert a["t1"] <= b["t0"] and a["t1"] - a["t0"] >= min(lines.MIN_CUE_S, b["t0"] - a["t0"] - 0.04)
    assert cues[0]["t0"] == 1.0 and cues[0]["t1"] == pytest.approx(1.0 + 7 * 0.25 + 0.225 + lines.HOLD_S, abs=1e-3)
    assert all(c["by"] == "auto" for c in cues)


def test_wrap_balances_two_lines():
    assert wrap("Short line.") == ["Short line."]
    assert wrap("It was loud, but there was a limiter, right?") == ["It was loud, but there", "was a limiter, right?"]
    jp = "ちょっと待って、リミッター入れといてよかった"
    a, b = wrap(jp)
    assert a + b == jp and abs(lines.width(a) - lines.width(b)) <= 2
    a, b = wrap("so *this whole highlighted phrase runs right across the middle* end")
    assert a.count("*") == 1 and b.count("*") == 1                     # a highlight carries over the break
    assert lines.runs(b, True)[0][0] == ("runs right across the middle", True)


@pytest.mark.parametrize("rules", [(lines.MAX_LINE, 2), (lines.SHORT_LINE, lines.SHORT_LINES), (lines.TITLE_LINE, 3)])
def test_preview_wraps_like_the_render(rules):
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    max_line, max_lines = rules
    js = EDITOR_JS.read_text(encoding="utf-8")
    start, end = js.index("  const WIDE = "), js.index("  const curSub = ")
    rnd = random.Random(3)
    vocab = ["Okay,", "sorry", "*limiter*", "I", "was", "really", "loud", "ちょっと", "待って、", "リミッター", "deaf?", "hololive"]
    samples = [" ".join(rnd.choice(vocab) for _ in range(rnd.randint(1, 14))) for _ in range(300)]
    samples += ["リミッター入れといてよかったちょっと待ってよ本当に", "x" * 50]
    script = (f"const D = {{ sub_layout: {{ max_line: {lines.MAX_LINE} }} }};\n{js[start:end]}\n"
              f"const S = {json.dumps(samples, ensure_ascii=False)};\n"
              f"console.log(JSON.stringify(S.map(t => {{ let on = false; return wrapSub(t, {max_line}, {max_lines})"
              ".map(l => { const [r, a] = runsOf(l, on); on = a; return r; }); })));")
    got = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout)
    for text, js_lines in zip(samples, got):
        on, want = False, []
        for line in wrap(text, max_line, max_lines):
            r, on = lines.runs(line, on)
            want.append([[p, h] for p, h in r])
        assert js_lines == want, text


def test_clean_and_timelines():
    cues = clean([{"t0": 5, "t1": 6, "text": " two\nlines ", "by": "auto"}, {"t0": 1, "t1": 2, "text": "one"},
                  {"t0": 3, "t1": 3.05, "text": "too short"}, {"t0": 7, "t1": 8, "text": "  "},
                  {"t0": 9, "t1": 99, "text": "past the end"}], 10)
    assert [(c["t0"], c["t1"], c["text"], c["by"]) for c in cues] == [
        (1.0, 2.0, "one", "you"), (5.0, 6.0, "two lines", "auto"), (9.0, 10.0, "past the end", "you")]
    with pytest.raises(SubsError):
        clean([{"text": "no times"}], 10)
    # Cut at 5.5 and played first: each part shows where its words play; slivers aren't shown.
    shown = on_edit(cues, [[5.5, 10, 0], [0, 5.45, 0]], [4.5, 5.45])
    assert shown == [(0.0, 0.5, "two lines", None), (3.5, 4.5, "past the end", None), (5.5, 6.5, "one", None),
                     (9.5, 9.95, "two lines", None)]
    srt = lines.srt(shown[:1])
    assert srt == "1\n00:00:00,000 --> 00:00:00,500\ntwo lines\n\n"
    ass = lines.ass([(0.0, 1.5, "Good thing I put a *limiter* in there, said nobody ever")], {"position": "top"}, 1280, 720)
    assert "PlayResY: 720" in ass and ",47,&H00FFFFFF" in ass and ",8,43,43,43,1" in ass     # 6.5% of 720, top centre
    assert "Dialogue: 0,0:00:00.00,0:00:01.50,Clip,,0,0,0,," in ass
    assert "{\\c&H00D6FF&}limiter\\N{\\c&HFFFFFF&}in there" in ass         # yellow word, then a white line


def _black_clip(path: Path, seconds: float = 3) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s=320x180:r=30:d={seconds}",
                    "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def _bottom_brightness(path: Path, t: float) -> float:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t}", "-i", str(path), "-frames:v", "1",
                          "-vf", "crop=320:60:0:120,format=gray", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    return max(raw)


@needs_ffmpeg
def test_render_burns_subtitles_in(tmp_path):
    src = _black_clip(tmp_path / "black.mp4")
    out = tmp_path / "e.mp4"
    render(EditConfig(loudness_lufs=0), src, [[0.0, 3.0, 0]], out, fps=30, has_audio=False,
           subs=[{"t0": 0.5, "t1": 2.0, "text": "HELLO THERE"}], sub_style={"size": "large", "position": "bottom"},
           size=(320, 180))
    assert _bottom_brightness(out, 0.2) < 40 and _bottom_brightness(out, 1.0) > 200 and _bottom_brightness(out, 2.5) < 40


@needs_ffmpeg
def test_editor_subtitles_follow_words_until_you_edit(site):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    folder = Path(conn.execute("SELECT folder FROM clips WHERE moment_id = ?", (m,)).fetchone()[0])
    d = client.get(f"/api/moment/{m}/edit").json()
    assert d["subs"] == [] and d["subs_source"] == "none" and d["subs_style"] == {"on": True, "size": "normal", "position": "bottom", "mode": "lines", "emphasis": []}
    (folder / "words.json").write_text(json.dumps(words(("Good thing I put a limiter in there.", 2.0, 0.25),
                                                         ("Are you deaf?", 6.0, 0.3))), encoding="utf-8")
    d = client.get(f"/api/moment/{m}/edit").json()
    assert d["subs_source"] == "auto" and [c["text"] for c in d["subs"]] == ["Good thing I put a limiter in there.", "Are you deaf?"]
    assert d["sub_words"][0] == [2.0, 2.225] and d["subs_auto"] == d["subs"]

    seg = {"segments": [[1, 8, 0]], "subs": "auto", "subs_style": {"on": True, "size": "large", "position": "top"}}
    client.post(f"/moment/{m}/render", json=seg)
    Worker(cfg, deps=deps).run(once=True)
    job = conn.execute("SELECT status, message FROM jobs WHERE moment_id = ? AND stage = 'render'", (m,)).fetchone()
    assert job["status"] == "done" and "2 subtitle line(s)" in job["message"]
    r = client.get(f"/api/moment/{m}/render").json()["render"]
    assert r["state"] == "done" and r["srt"].startswith(f"/media/edit-srt/{m}")
    got = client.get(f"/media/edit-srt/{m}")
    assert got.status_code == 200 and "00:00:01,000 -->" in got.text and "Are you deaf?" in got.text

    # Your edit is kept (and makes the render out of date); "auto" follows the words again.
    mine = d["subs"][:1] + [d["subs"][1] | {"text": "Are you *deaf*?!", "by": "you"}]
    client.post(f"/moment/{m}/edit", json=seg | {"subs": mine})
    d = client.get(f"/api/moment/{m}/edit").json()
    assert d["subs_source"] == "saved" and d["subs"][1]["text"] == "Are you *deaf*?!" and d["subs"][1]["by"] == "you"
    assert d["subs_style"] == {"on": True, "size": "large", "position": "top", "mode": "lines", "emphasis": []}
    assert client.get(f"/api/moment/{m}/render").json()["render"]["state"] == "stale"
    client.post(f"/moment/{m}/edit", json=seg | {"subs": "auto"})
    assert client.get(f"/api/moment/{m}/edit").json()["subs_source"] == "auto"
    assert client.get(f"/api/moment/{m}/render").json()["render"]["state"] == "done"
    # Emphasis words (the AI edit's) stay highlighted in cues that follow the words.
    client.post(f"/moment/{m}/edit", json=seg | {"subs_style": seg["subs_style"] | {"emphasis": ["*deaf*"]}})
    d = client.get(f"/api/moment/{m}/edit").json()
    assert d["subs_source"] == "auto" and d["subs"][1]["text"] == "Are you *deaf*?"
    assert d["subs_auto_by_mode"]["lines"][1]["text"] == "Are you *deaf*?"
    t = client.get(f"/api/moment/{m}/transcript?kind=long").json()
    assert t["subs_auto"][1]["text"] == "Are you *deaf*?"
    assert client.get(f"/api/moment/{m}/transcript?kind=wide").status_code == 400
    # People: none until you add them; a line you give person 0 gets her colour.
    assert client.get(f"/api/moment/{m}/edit").json()["voices"]["count"] == 0
    r = client.post(f"/moment/{m}/voices", json={"voices": {"0": {"name": "Gigi", "color": "#ff8fc8"}}}).json()
    assert r["voices"]["0"]["name"] == "Gigi" and r["voices"]["0"]["color"] == "FF8FC8" and r["people"] == {"gigi": "FF8FC8"}
    tagged = [c | {"speaker": 0, "by": "you"} if i == 0 else c for i, c in enumerate(d["subs"])]
    client.post(f"/moment/{m}/edit", json=seg | {"subs": tagged})
    d = client.get(f"/api/moment/{m}/edit").json()
    assert d["voices"]["count"] == 1 and d["voices"]["map"]["0"]["name"] == "Gigi"
    assert d["subs"][0]["speaker"] == 0 and d["subs"][0]["color"] == "FF8FC8" and "speaker" not in d["subs"][1]
    # The VTubers page: her colour there is her colour in the clip.
    page = client.get("/vtubers")
    assert page.status_code == 200 and "Gigi" in page.text and "1 clip" in page.text
    assert client.post("/vtubers/save", data={"name": "Gigi", "color": "#0f0", "old": "Gigi"},
                       follow_redirects=False).status_code == 303
    assert client.get(f"/api/moment/{m}/edit").json()["subs"][0]["color"] == "00FF00"
    bad = client.post("/vtubers/save", data={"name": "Gigi", "color": "green"}, follow_redirects=False)
    assert "error=" in bad.headers["location"]
    client.post("/vtubers/save", data={"name": "Kyoko", "color": "7FD3FF"})
    client.post("/vtubers/delete", data={"name": "kyoko"})
    assert "Kyoko" not in client.get("/vtubers").text
    bad = client.post(f"/moment/{m}/edit", json=seg | {"subs": [{"text": "no times"}]})
    assert bad.status_code == 400
