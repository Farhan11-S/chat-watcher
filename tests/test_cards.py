"""Cards: full screens ("YOU DIED", a poster, one line) and comment stickers."""

import json
import shutil
import subprocess

import pytest

from chatwatcher.config import EditConfig
from chatwatcher.edit import autoedit as ae
from chatwatcher.edit import cards as cd
from chatwatcher.edit.render import render
from chatwatcher.edit.service import EditError
from chatwatcher.edit.validate import clean_effects
from chatwatcher.worker import Worker

from test_clip import make_video
from test_web import mid, site  # noqa: F401  (fixture)

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def test_clean_cards():
    died, comment, flat = clean_effects([
        {"type": "card", "t": 1, "style": "died", "text": "", "d": 99, "color": "nope"},
        {"type": "card", "t": 2, "style": "comment", "text": "does she 🔥 ever win?", "who": "x" * 40, "at": "Face ",
         "dy": -9, "bg": "black"},
        {"type": "card", "t": 3, "style": "weird", "text": "3 hours later"}], 10, set())
    assert (died["text"], died["d"], died["color"], died["bg"]) == ("YOU DIED", 10.0, "blood", "dim")
    assert comment["text"] == "does she ever win?" and len(comment["who"]) == 24 and comment["at"] == "face"
    assert comment["bg"] == "" and comment["dy"] == -1.0                    # clamped like a text callout
    assert flat["style"] == "flat" and "who" not in flat
    with pytest.raises(EditError):
        clean_effects([{"type": "card", "t": 1, "d": "long"}], 10, set())


def test_poster_words_and_reveal():
    rows, hi = cd.poster_words("you don't need to excel at anything")
    assert rows == [["YOU", "DON'T"], ["NEED", "TO"], ["EXCEL", "AT"], ["ANYTHING"]] and hi == {6}   # the last line
    rows, hi = cd.poster_words("put the *gift away* now")
    assert [w for r in rows for w in r] == ["PUT", "THE", "GIFT", "AWAY", "NOW"] and hi == {2, 3}
    assert cd.poster_step(7, 2.5) == pytest.approx(2.5 / 14) and cd.poster_step(100, 1) == cd.POSTER_STEP[0]


def test_events_layers_and_split_cards():
    fx = [{"type": "card", "t": 2.0, "d": 3.0, "style": "died", "text": "you died"},
          {"type": "card", "t": 6.0, "d": 2.0, "style": "poster", "text": "be normal *for once*"},
          {"type": "card", "t": 9.0, "d": 2.0, "style": "comment", "text": "hi", "who": "a"}]
    ps = cd.placed(fx, {}, None)
    ev = cd.events(cd.local(ps, 0, 12), 1080, 1920)
    assert all(int(e.split(",")[0].split()[1]) >= 10 for e in ev)          # above captions and callouts
    assert any("YOU DIED" in e and "\\fnCinzel" in e for e in ev)
    poster = [e for e in ev if "Archivo Black" in e]
    assert len(poster) == 4 and "&HFF&}NORMAL" in poster[0] and "&H00&}NORMAL" in poster[1]   # one word at a time
    assert any("Reply to a's comment" in e for e in ev) and any("\\move(" in e for e in ev)
    # A card split by a cut: the second part doesn't fade or pop in again.
    later = cd.events(cd.local(ps, 3.5, 10), 1080, 1920)
    died = [e for e in later if "YOU DIED" in e][0]
    assert "\\t(0," not in died and died.split(",")[1] == "0:00:00.00"
    assert cd.local(ps, 20, 5) == []


@needs_ffmpeg
def test_render_draws_a_card(tmp_path):
    src = make_video(tmp_path / "src.mp4", 6)
    out = tmp_path / "e.mp4"
    ps = cd.placed([{"type": "card", "t": 2.0, "d": 2.0, "style": "flat", "text": "later", "bg": "white"}], {}, None)
    render(EditConfig(), src, [[0.0, 6.0, 0]], out, fps=30, cards=[ps])

    def mean(at):
        px = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(out), "-frames:v", "1", "-vf",
                             "scale=32:18", "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True).stdout
        return sum(px) / len(px)
    assert mean(3.0) > 200 and mean(5.0) < 200                              # white while it shows, then the clip


def test_ai_cards():
    got = ae.card_effects(["10 | 3 | died | you died | the crash", "1 | 2 | bogus | x", "99 | 2 | flat | late",
                           "0.5 | 3 | comment | does she ever win?", "4 | 2 | flat | one too many"], [[0, 20, 0]])
    assert [(c["style"], c["t"]) for c in got] == [("died", 10.0), ("comment", 0.5)]
    assert got[1]["dy"] == cd.DEFAULT_DY["comment"] and got[0]["by"] == "claude" and got[0]["why"] == "the crash"
    assert "cards" in ae.schema(3, 2)["required"]
    assert "card died" in ae.placed_line({"type": "card", "t": 1.0, "d": 3.0, "style": "died", "text": "YOU DIED"})


@needs_ffmpeg
def test_cards_in_the_editor_and_render(site):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    e = client.get(f"/api/moment/{m}/edit").json()
    assert set(e["card_info"]["styles"]) == {"died", "poster", "flat", "comment"} and e["visual"]["types"]["card"]
    seg = {"segments": [[2, 8, 0]], "source": "long"}
    fx = [{"id": "u1", "type": "card", "t": 6.0, "d": 2.0, "style": "died", "text": "YOU DIED"}]
    assert client.post(f"/moment/{m}/edit", json=seg | {"effects": fx}).json()["ok"]
    client.post(f"/moment/{m}/render", json=seg | {"effects": fx})
    Worker(cfg, deps=deps).run(once=True)
    assert client.get(f"/api/moment/{m}/render").json()["render"]["state"] == "done"
    info = json.loads(conn.execute("SELECT rendered_json FROM edits WHERE moment_id = ?", (m,)).fetchone()[0])
    assert info["visuals"] >= 1
    assert client.get("/static/fonts/Cinzel.ttf").status_code == 200
