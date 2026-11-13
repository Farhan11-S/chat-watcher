import io
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

from chatwatcher import db, jobs
from chatwatcher.brand import brand as br
from chatwatcher.brand import intro as it
from chatwatcher.brand import canvas as cv
from chatwatcher.brand import cutout as co
from chatwatcher.brand import layers as L
from chatwatcher.brand import thumb as th
from chatwatcher.brand import thumbai as ta
from chatwatcher.analyze.claude_runner import ClaudeResult
from chatwatcher.clip.media import probe
from chatwatcher.config import Config, EditConfig, PathsConfig
from chatwatcher.edit.render import render
from chatwatcher.worker import Worker

from test_clip import make_video
from test_web import mid, site  # noqa: F401  (fixture)

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")

SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <circle cx="256" cy="256" r="200" fill="#2E1E52"/><polygon points="230,190 230,320 330,256" fill="#FFD36E"/></svg>"""


# ── teasers ──────────────────────────────────────────────────────────────────

def test_pick_takes_loud_lines_and_a_blast_with_its_lead_in():
    rate = 50
    peaks = [10] * (60 * rate)
    for a, b, level in ((20, 23, 70), (40, 42.5, 80), (50, 51, 90)):
        peaks[int(a * rate):int(b * rate)] = [level] * int((b - a) * rate)
    cues = [{"t0": 5, "t1": 6.5, "text": "quiet line"},
            {"t0": 20, "t1": 23, "text": "It was loud, right?"},
            {"t0": 28, "t1": 31.2, "text": "If I record this..."},      # leads into the scream at 31
            {"t0": 40, "t1": 44.5, "text": "A long line that goes on and on"},
            {"t0": 50, "t1": 51, "text": "Oh."},
            {"t0": 70, "t1": 72, "text": "outside the edit"}]
    hits = [(31.1, 24.0), (10.0, 4.0)]
    out = it.pick([[2, 60, 0]], cues, [], peaks, rate, hits, [{"start": 39, "end": 45, "beat": "payoff"}],
                  words=[[40, 40.4], [40.5, 41.2], [41.3, 42.6], [42.7, 44.5]])
    assert out == sorted(out)
    blast = [t for t in out if t[3] == "blast"]
    assert len(blast) == 1 and blast[0][0] == pytest.approx(31.0) and blast[0][2] < 0
    lead = [t for t in out if t[0] == 28]
    assert lead and lead[0][1] <= 31.0                         # ends where the blast starts
    long = [t for t in out if t[0] == 40][0]
    assert long[1] == pytest.approx(42.72)                     # cut after ~3 s, at a word's end
    assert all(t[1] <= 60 for t in out) and not [t for t in out if t[0] == 5]


def test_pick_without_lines_uses_the_loudest_stretches():
    rate = 50
    peaks = [5] * (30 * rate)
    peaks[10 * rate:12 * rate] = [90] * (2 * rate)
    out = it.pick([[0, 30, 0]], [], [], peaks, rate, [], [])
    assert out and any(t[0] <= 10.5 and t[1] >= 11 for t in out)


def test_clean_teasers():
    assert it.clean([[1, 3, 2, "blast"], [5, 6]], 10) == [[1.0, 3.0, 2.0, "blast"], [5.0, 6.0, 0.0, "normal"]]
    assert it.clean([[1, 3, 99, "weird"]], 10) == [[1.0, 3.0, 6.0, "normal"]]
    for bad in ([[1, 1.1]], "x", [[0, 9]] * 9, [[0, 9]] * 3):
        with pytest.raises(it.IntroError):
            it.clean(bad, 10)


@needs_ffmpeg
def test_intro_renders_teasers_then_logo_and_can_start_an_edit(tmp_path):
    src = make_video(tmp_path / "src.mp4", 12)
    b = br.Brand(name="Test Clips", tagline="clips for testing")
    seen = []
    r = it.render_intro(b, None, None, None, src, [[1.0, 2.0, 0, "normal"], [4.0, 4.5, -6, "blast"]],
                        tmp_path / "intro.mp4", fps=30, size=(320, 240), on_progress=seen.append)
    want = 1.5 + it.logo_seconds()
    assert abs(r.duration_s - want) < 0.1 and seen[-1] == 1.0
    pr = probe(r.path)
    assert (pr.width, pr.height) == (320, 240) and pr.acodec and not list(tmp_path.glob(".render_*"))

    out = render(EditConfig(), src, [[6.0, 9.0, 0]], tmp_path / "edit.mp4", fps=30, size=(320, 240),
                 intro=r.path, intro_s=r.duration_s)
    assert abs(out.duration_s - (3.0 + want)) < 0.1


# ── thumbnails ───────────────────────────────────────────────────────────────

def test_layers_are_checked_and_clamped():
    raw = [{"kind": "text", "id": "aa", "text": " *BIG*   news ", "x": 9, "size": "0.2", "colour": "nope", "style": "?"},
           {"kind": "bg", "source": "blur", "t": 99, "zoom": 0},
           {"kind": "bg", "source": "brand"},
           {"kind": "cutout", "id": "aa", "sx": 0.9, "sw": 0.8, "outline_colour": "#ff0000"},
           {"kind": "sticker", "shape": "image", "image": "../../etc/passwd"}]
    out = L.clean(raw, 20)
    assert [ly["kind"] for ly in out] == ["bg", "text", "cutout", "sticker"]          # one background, first
    bg, text, cut, st = out
    assert bg["source"] == "blur" and bg["t"] == pytest.approx(19.95) and bg["zoom"] == 1.0
    assert text["text"] == "*BIG* news" and text["x"] == 1.5 and text["size"] == 0.2
    assert text["colour"] == "white" and text["style"] == "stroke"                    # bad values → defaults
    assert cut["sw"] == pytest.approx(0.1) and cut["outline_colour"] == "#FF0000"     # the part stays inside the frame
    assert len({ly["id"] for ly in out}) == 4 and "/" not in st["image"]
    for bad in ("x", [{"kind": "video"}], [{"kind": "text", "x": "far"}], [{"kind": "text"}] * 40):
        with pytest.raises(L.LayerError):
            L.clean(bad, 20)
    # Old fixed-style settings become a background and a text layer; times shift both ways.
    old = L.from_settings({"t": 3, "text": "HI", "zoom": 1.5, "cx": 0.8, "side": "auto"})
    assert [ly["kind"] for ly in old] == ["bg", "text"] and old[0]["zoom"] == 1.5 and old[1]["align"] == "left"
    assert L.shift_times(old, 100)[0]["t"] == 103 and "t" not in L.shift_times(old, 100)[1]
    assert L.hex_of("accent", br.Brand()) == br.Brand().accent and L.rgb("red", br.Brand()) == (255, 43, 43)


def _source(cutout=None, size=(th.W, th.H)):
    frame = Image.new("RGB", (1920, 1080), (40, 120, 90))
    frame.paste((230, 30, 30), (1400, 300, 1800, 900))

    def her(t, box):
        if cutout == "fail":
            raise co.CutoutError("nobody there")
        img = Image.new("RGBA", (300, 500), (0, 0, 0, 0))
        img.paste((240, 200, 210, 255), (50, 0, 250, 500))
        return img
    return cv.Source(size, br.Brand(), None, {"title": None, "text": None}, lambda t: frame, lambda t: frame, her, None)


def test_canvas_draws_every_kind_as_sprites():
    src = _source()
    pad = src.pad
    layers = L.clean([
        L.make("bg", source="frame", t=1, zoom=2, cx=0.83, cy=0.55),
        L.make("frame", t=1, sx=0.7, sy=0.25, sw=0.25, sh=0.6, w=0.3, x=0.2, y=0.3, shape="circle"),
        L.make("cutout", t=1, w=0.3, x=0.8, y=0.6, glow=0.5),
        L.make("text", text="SHE *SCREAMED* 💀", w=0.5, x=0.3, y=0.2, rot=-6),
        L.make("text", text="BOX", style="box", w=0.3, x=0.3, y=0.8),
        L.make("sticker", shape="arrow", w=0.2, x=0.5, y=0.5, rot=30),
        L.make("sticker", shape="ring", w=0.2, x=0.5, y=0.5, aspect=0.5),
        L.make("sticker", shape="burst", w=0.2, x=0.5, y=0.5),
        L.make("sticker", shape="logo", w=0.1, x=0.1, y=0.1),
        L.make("sticker", shape="image", image="gone.png", w=0.1, x=0.1, y=0.1),
    ], 20)
    img = cv.render(layers, src)
    assert img.size == (th.W, th.H)
    assert img.getpixel((640, 360))[0] > 200 or img.getpixel((1100, 420))[0] > 150     # zoomed onto the red box
    for ly in layers[1:]:
        spr = cv.sprite(ly, src)
        w, h = cv.content_size(ly, spr, src)
        assert spr.mode == "RGBA" and w == pytest.approx(ly["w"], abs=2 / th.W) and h > 0
        assert spr.getpixel((1, 1))[3] == 0                                            # the margin is clear
    long = L.make("text", text="NOOOOOOOOOO", w=0.2, size=0.2)                        # one word, too wide
    ink, lines, _ = cv.text_lines(long, src)
    assert len(lines) == 1 and ink.size < 0.2 * th.H                                   # smaller, not broken up
    _, jp, _ = cv.text_lines(L.make("text", text="こわれたあああああ", w=0.2, size=0.2), src)
    assert len(jp) > 1                                                                 # Japanese breaks anywhere
    arrow = cv.sprite(layers[5], src)
    assert arrow.height - 2 * pad == round(0.2 * th.W * 0.42)
    circle = cv.sprite(layers[1], src)
    assert circle.width == circle.height                                               # a circle is square
    # A hidden layer isn't drawn; a cut-out that fails is left out (or raises without on_error).
    plain = cv.render([layers[0]], src)
    hidden = cv.render([layers[0], layers[3] | {"hidden": True}], src)
    assert plain.tobytes() == hidden.tobytes()
    bad = _source("fail")
    errors = []
    cv.render(layers[:3], bad, on_error=lambda ly, e: errors.append(ly["kind"]))
    assert errors == ["cutout"]
    with pytest.raises(co.CutoutError):
        cv.render(layers[:3], bad)
    # Every background source, and the cover's size.
    tall = _source(size=(th.TW, th.TH))
    for source in L.BG_SOURCES:
        assert cv.render([L.make("bg", source=source, dim=0.3)], tall).size == (th.TW, th.TH)


def _board(**kw):
    def measure(ly):
        if ly["kind"] == "text":
            return ly["w"], ly["size"] * 1.1
        if ly["kind"] == "cutout":
            return ly["w"], ly["w"] * (th.W / th.H) * 1.6                              # she's tall and thin
        return ly["w"], ly["w"] * 0.7
    face = {"x": 0.7, "y": 0.3, "w": 0.2, "h": 0.4}
    return ta.Board(kw.get("shape", "wide"), (th.W, th.H), [3.0, 7.5, 12.0], {"face": face, "game": {"x": 0, "y": 0, "w": 0.6, "h": 0.8}},
                    (1920, 1080), lambda x, y: (x, y), kw.get("measure", measure), ["shocked.png"])


def test_claude_lines_become_layers():
    answer = {"versions": [
        {"name": "reaction", "why": "her face", "lines": [
            "bg | blur | 2", "cutout | 2 | face | right | 0.95 | white", "text | MY *EARS* | left top | 0.18 | yellow | stroke | -4",
            "arrow | text | cutout | red", "emoji | 💀 | text | 0.1", "ring | face | red", "sticker | shocked | left bottom | 0.15",
            "sticker | nope | left | 0.1", "explode | everything", "text | | left"]},
        {"name": "situation", "why": "the moment", "lines": [
            "bg | frame | 3 | 2 | face", "text | LATE | right bottom | 0.2 | white | box", "frame | 1 | face | right top | 0.3 | circle",
            "ring | face | yellow", "burst | left top | 0.3 | yellow"]},
        {"name": "no background", "why": "", "lines": ["text | HI | centre | 0.2"]},
        {"name": "one too many", "why": "", "lines": []}]}
    ideas = ta.read(answer, _board(), 20)
    assert len(ideas) == 3
    one = ideas[0]
    kinds = [ly["kind"] for ly in one.layers]
    assert kinds == ["bg", "cutout", "text", "sticker", "sticker", "sticker", "sticker"]
    bg, cut, text, arrow, emoji, ring, sticker = one.layers
    assert bg["source"] == "blur" and bg["t"] == 7.5 and bg["dim"] > 0
    assert cut["x"] + cut["w"] / 2 == pytest.approx(1.0, abs=0.01) and cut["y"] > 0.4         # right, standing on the bottom
    assert text["align"] == "left" and text["x"] - text["w"] / 2 == pytest.approx(0.035) and text["rot"] == -4
    assert text["colour"] == "yellow" and text["accent"] == "white"
    assert arrow["shape"] == "arrow" and -60 < arrow["rot"] < 60                               # left → right
    assert emoji["emoji"] == "💀" and sticker["image"] == "shocked.png"
    assert len(one.warnings) == 3                                                              # the bad lines only
    two = ideas[1]
    bg2, text2, frame2, ring2, burst = two.layers[0], two.layers[1], two.layers[2], two.layers[3], two.layers[4]
    assert bg2["zoom"] == 2 and (bg2["cx"], bg2["cy"]) == (0.8, 0.5)                          # centred on her face
    assert text2["style"] == "box" and text2["box_colour"] == "white" and text2["colour"] == "black"
    assert text2["y"] + 0.2 * 1.1 / 2 < 1 - 0.05 - 0.1                                        # off YouTube's timestamp
    assert frame2["shape"] == "circle" and frame2["x"] > 0.5 and frame2["y"] < 0.5
    assert ring2["x"] == pytest.approx(0.6, abs=0.02)                                          # her face, the zoom clamped at the edge
    assert burst["colour"] == "yellow"
    whole = ta.read({"versions": [{"name": "x", "why": "", "lines": ["bg | brand", "text | *NOOO* | right | 0.2 | accent"]}]},
                    _board(), 20)[0].layers[1]
    assert whole["text"] == "NOOO" and whole["colour"] == "accent"
    assert ideas[2].layers[0]["source"] == "blur" and ideas[2].warnings                         # a background is added
    # A cut-out that can't be made becomes a framed picture of her.
    def broken(ly):
        if ly["kind"] == "cutout":
            raise co.CutoutError("no model")
        return ly["w"], ly["w"] * 0.7
    fallback = ta.read({"versions": [{"name": "x", "why": "", "lines": ["bg | brand", "cutout | 1 | face | left | 0.9"]}]},
                       _board(measure=broken), 20)[0]
    assert [ly["kind"] for ly in fallback.layers] == ["bg", "frame"] and "framed" in fallback.warnings[0]
    p = ta.build_prompt(shape="tall", meta={"title": "stream"}, analysis={"summary": "she screams",
                        "titles": [{"lang": "en", "text": "A TITLE"}]}, frames=[{"clip_s": 3, "is_peak": True}],
                        boxes={"face": {"x": 0.7, "y": 0.3, "w": 0.2, "h": 0.4}}, stickers=[], brand=br.Brand(), words=["no way"])
    assert "1080×1920" in p and "face: x 70–90%" in p and "A TITLE" in p and "1 at 0:03 (peak)" in p
    assert ta.place_of("Right Top") == (1.0, 0.0) and ta.place_of("face") is None


# ── brand ────────────────────────────────────────────────────────────────────

@needs_ffmpeg
def test_brand_logo_fonts_and_colours(tmp_path):
    cfg = Config(paths=PathsConfig(data_dir=tmp_path / "data", output_dir=tmp_path / "out"))
    conn = db.open_db(cfg.paths.db_path)
    assert br.get(conn).name == "MY CLIPS" and not br.get(conn).logo
    svg = tmp_path / "logo.svg"
    svg.write_text(SVG)
    b = br.import_logo(cfg, conn, svg, "logo.svg")
    logo = Image.open(br.logo_path(cfg))
    assert b.logo and logo.mode == "RGBA" and logo.getchannel("A").getbbox() == (0, 0, *logo.size)   # trimmed
    with pytest.raises(br.BrandError):
        br.import_logo(cfg, conn, svg, "logo.gif")
    before = br.fingerprint(cfg, br.get(conn))
    b = br.update(conn, name="  Jone   Clips ", tagline="t", colours={"accent": "#ff0000"})
    assert b.name == "Jone Clips" and b.accent == "#FF0000" and br.fingerprint(cfg, b) != before
    with pytest.raises(br.BrandError):
        br.update(conn, colours={"deep": "purple"})
    font = br.font_file(cfg, b, "title")
    if font:
        b = br.import_font(cfg, conn, "title", Path(font), Path(font).name)
        assert br.font_label(cfg, b, "title") == "your upload"
        assert br.remove_font(cfg, conn, "title").title_font == ""
    junk = tmp_path / "x.ttf"
    junk.write_bytes(b"not a font")
    with pytest.raises(br.BrandError):
        br.import_font(cfg, conn, "title", junk, "x.ttf")
    conn.close()


# ── the moment page: intro, thumbnail, brand page ────────────────────────────

@needs_ffmpeg
def test_intro_and_thumbnail_on_the_moment_page(site, tmp_path):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    d = client.get(f"/api/moment/{m}/intro").json()
    assert d["ok"] and d["source"] == "auto" and d["state"]["state"] == "none" and not d["on_edit"]
    assert d["brand"]["name"] == "MY CLIPS" and d["logo_s"] > 3

    r = client.post(f"/moment/{m}/intro", json={"teasers": [[2, 3.5, 0, "normal"], [5, 5.6, -6, "blast"]]}).json()
    assert r["ok"] and r["source"] == "saved" and r["teasers"] == [[2.0, 3.5, 0.0, "normal"], [5.0, 5.6, -6.0, "blast"]]
    zero = conn.execute("SELECT zero_s FROM clips WHERE moment_id = ?", (m,)).fetchone()["zero_s"]
    stored = json.loads(conn.execute("SELECT teasers_json FROM intros WHERE moment_id = ?", (m,)).fetchone()[0])
    assert stored[0][0] == pytest.approx(2 + zero)                                         # stream time
    assert client.post(f"/moment/{m}/intro", json={"teasers": [[1, 1.05]]}).status_code == 400

    r = client.post(f"/moment/{m}/intro/render", json={}).json()
    assert r["ok"] and r["state"]["state"] == "queued"
    v = conn.execute("SELECT video_id FROM moments WHERE id = ?", (m,)).fetchone()["video_id"]
    jobs.enqueue(conn, v, "analyze")
    assert jobs.claim(conn)["stage"] == "intro"                                            # jumps the queue
    conn.execute("UPDATE jobs SET status = 'pending' WHERE status = 'running'")
    conn.execute("DELETE FROM jobs WHERE stage = 'analyze' AND moment_id IS NULL")
    Worker(cfg, deps=deps).run(once=True)
    st = client.get(f"/api/moment/{m}/intro/state").json()["state"]
    assert st["state"] == "done" and abs(st["length"] - (2.1 + it.logo_seconds())) < 0.15
    assert client.get(st["url"]).headers["content-type"] == "video/mp4"

    # The edit starts with the intro once it's switched on; changing a teaser makes both stale.
    client.post(f"/moment/{m}/edit", json={"segments": [[4, 8, 0]]})
    client.post(f"/moment/{m}/render", json={"segments": [[4, 8, 0]]})
    Worker(cfg, deps=deps).run(once=True)
    plain = client.get(f"/api/moment/{m}/render").json()["render"]
    assert plain["state"] == "done" and abs(plain["length"] - 4.0) < 0.1
    r = client.post(f"/moment/{m}/intro", json={"on_edit": True}).json()
    assert r["on_edit"] and r["render"]["state"] == "stale"
    client.post(f"/moment/{m}/render", json={"segments": [[4, 8, 0]]})
    Worker(cfg, deps=deps).run(once=True)
    full = client.get(f"/api/moment/{m}/render").json()["render"]
    assert full["state"] == "done" and abs(full["length"] - (4.0 + st["length"])) < 0.2
    client.post(f"/moment/{m}/intro", json={"teasers": [[2, 3, 0, "normal"]]})
    assert client.get(f"/api/moment/{m}/intro/state").json()["state"]["state"] == "stale"
    assert client.get(f"/api/moment/{m}/render").json()["render"]["state"] == "stale"
    assert client.post(f"/moment/{m}/intro", json={"teasers": "auto"}).json()["source"] == "auto"

    # Thumbnail: layers, starting from the analysis's frame and words; saved as thumbnail.jpg.
    t = client.get(f"/api/moment/{m}/thumb").json()
    assert t["ok"] and t["saved"] is None and t["source"] == "default" and t["size"] == [1280, 720]
    assert [ly["kind"] for ly in t["layers"]] == ["bg", "text"] and t["layers"][1]["text"] == "壊れた!?"
    assert t["spec"]["text"]["size"][1] == 0.16 and "x" in t["spec"]["text"] and "x" not in t["spec"]["bg"]
    assert t["frames"] and t["frames"][0]["url"].startswith("/media/frame/") and not t["cutouts"]
    bg = client.post(f"/moment/{m}/thumb/sprite", json={"layer": t["layers"][0]})
    assert bg.headers["content-type"] == "image/jpeg" and Image.open(io.BytesIO(bg.content)).size == (1280, 720)
    txt = client.post(f"/moment/{m}/thumb/sprite", json={"layer": t["layers"][1] | {"w": 0.5}})
    pad = round(L.PAD * 720)
    assert txt.headers["content-type"] == "image/png" and Image.open(io.BytesIO(txt.content)).width == 640 + 2 * pad
    cut = client.post(f"/moment/{m}/thumb/sprite", json={"layer": L.make("cutout", t=2)})
    assert cut.status_code == 400 and "model" in cut.json()["error"]                    # not downloaded in tests
    assert client.post(f"/moment/{m}/thumb/sprite", json={"layer": {"kind": "nope"}}).status_code == 400
    layers = t["layers"] + [L.make("sticker", shape="arrow", x=0.5, y=0.5, w=0.2, rot=45), L.make("cutout", t=2)]
    pv = client.post(f"/moment/{m}/thumb/preview", json={"layers": layers})
    assert pv.headers["content-type"] == "image/jpeg" and Image.open(io.BytesIO(pv.content)).size == (1280, 720)
    s = client.post(f"/moment/{m}/thumb", json={"layers": layers}).json()
    assert s["ok"] and s["saved"] and s["source"] == "saved" and len(s["layers"]) == 4
    assert s["warnings"] and "cutout" in s["warnings"][0]                               # drawn without her cut-out
    zero = conn.execute("SELECT zero_s FROM clips WHERE moment_id = ?", (m,)).fetchone()["zero_s"]
    stored = json.loads(conn.execute("SELECT layers_json FROM thumbs WHERE moment_id = ?", (m,)).fetchone()[0])
    assert stored[0]["t"] == pytest.approx(s["layers"][0]["t"] + zero)                  # stream time
    assert client.get(s["saved"]["url"]).headers["content-type"] == "image/jpeg"
    assert client.post(f"/moment/{m}/thumb", json={"layers": "x"}).status_code == 400
    # Made before thumbnails were layers: the old settings come back as layers.
    conn.execute("UPDATE thumbs SET layers_json = NULL, settings_json = ? WHERE moment_id = ?",
                 (json.dumps({"text": "OLD", "zoom": 1.4, "side": "right", "t_stream": zero + 1}), m))
    old = client.get(f"/api/moment/{m}/thumb").json()
    assert old["source"] == "old" and old["layers"][1]["text"] == "OLD" and old["layers"][0]["zoom"] == 1.4
    client.post(f"/moment/{m}/thumb", json={"layers": layers})

    page = client.get(f"/moment/{m}").text
    assert 'data-intro="' in page and 'data-thumb="' in page and "/static/maker.js" in page and "/static/thumb.js" in page

    # "Delete videos only" removes the intro with the other videos but keeps the thumbnail.
    out = Path(conn.execute("SELECT output_dir FROM videos").fetchone()["output_dir"])
    assert list(out.glob("*/intro.mp4")) and list(out.glob("*/thumbnail.jpg"))
    client.post("/storage/delete", data={"key": f"{out.parent.name}/{out.name}", "mode": "videos"})
    assert not list(out.glob("*/intro.mp4")) and list(out.glob("*/thumbnail.jpg"))


@needs_ffmpeg
def test_brand_page(site, tmp_path):  # noqa: F811
    cfg, conn, client, _ = site
    assert client.get("/brand").status_code == 200
    assert client.get("/brand/logo.png").status_code == 404
    r = client.post("/brand", data={"name": "Jone Clips", "tagline": "VTuber clips", "deep": "#2E1E52",
                                    "purple": "#7A5CD6", "lavender": "#B9A0F2", "light": "#DACCFA", "accent": "#FFD36E"},
                    files={"logo": ("jone.svg", SVG.encode(), "image/svg+xml")}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert br.get(conn).name == "Jone Clips" and br.get(conn).logo
    assert client.get("/brand/logo.png").status_code == 200
    card = client.get("/brand/card.jpg")
    assert card.headers["content-type"] == "image/jpeg" and Image.open(io.BytesIO(card.content)).size == (960, 540)
    bad = client.post("/brand", data={"name": "x", "deep": "red"}, follow_redirects=False)
    assert "error=" in bad.headers["location"]
    client.post("/brand/remove/logo")
    assert not br.get(conn).logo
    assert "Brand" in client.get("/").text


# ── the Short's cover ────────────────────────────────────────────────────────

def test_cover_draws_the_short_layout_upright():
    from chatwatcher.edit import layout as lay
    frame = Image.new("RGB", (1920, 1080), (40, 120, 90))
    frame.paste((230, 30, 30), (1400, 300, 1800, 900))                        # "her face": red, on the right
    face = {"x": 1400 / 1920, "y": 300 / 1080, "w": 400 / 1920, "h": 600 / 1080}
    crop = th.compose(frame, lay.panels(lay.default({"face": face}, "talk"), {"face": face}, 1920, 1080))
    assert crop.size == (th.TW, th.TH) and crop.getpixel((540, 960))[0] > 200   # centred on her face
    stack = th.compose(frame, lay.panels({"type": "stack", "top": "", "bottom": "face", "split": 0.6},
                                         {"face": face}, 1920, 1080))
    assert stack.getpixel((540, 1600))[0] > 200 and stack.getpixel((100, 300))[0] < 100
    fit = th.compose(frame, lay.panels({"type": "fit"}, {}, 1920, 1080))
    assert sum(fit.getpixel((20, 40))) < sum(fit.getpixel((20, 960)))         # darker blurred background


def test_cover_on_the_moment_page(site):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    t = client.get(f"/api/moment/{m}/thumb?shape=tall").json()
    assert t["ok"] and t["shape"] == "tall" and t["size"] == [1080, 1920] and t["layout"] == "crop"
    assert not t["short_saved"] and t["layers"][1]["text"] == "壊れた!?" and not t["saved"]
    pv = client.post(f"/moment/{m}/thumb/preview", json={"layers": t["layers"], "shape": "tall"})
    assert pv.status_code == 200 and Image.open(io.BytesIO(pv.content)).size == (1080, 1920)
    assert client.get(f"/api/moment/{m}/thumb?shape=round").status_code == 400

    layers = t["layers"][:1] + [L.make("text", text="COVER", x=0.5, y=0.5, w=0.8, size=0.07)]
    s = client.post(f"/moment/{m}/thumb", json={"layers": layers, "shape": "tall"}).json()
    assert s["ok"] and s["saved"]["url"].startswith(f"/media/thumb/{m}?shape=tall")
    assert Path(s["saved"]["path"]).name == "short_cover.jpg"
    assert Image.open(s["saved"]["path"]).size == (1080, 1920)
    again = client.get(f"/api/moment/{m}/thumb?shape=tall").json()
    assert again["source"] == "saved" and again["layers"][1]["text"] == "COVER"
    # The video's thumbnail is untouched: still the default, not saved.
    wide = client.get(f"/api/moment/{m}/thumb").json()
    assert not wide["saved"] and wide["source"] == "default"
    media = client.get(f"/media/thumb/{m}?shape=tall&dl=1")
    assert media.status_code == 200 and "short_cover.jpg" in media.headers["content-disposition"]
    assert client.get(f"/media/thumb/{m}").status_code == 404

    # Its background follows the Short's layout.
    client.post(f"/moment/{m}/edit", json={"kind": "short", "segments": [[1, 5]], "layout": {"type": "fit"}})
    t = client.get(f"/api/moment/{m}/thumb?shape=tall").json()
    assert t["layout"] == "fit" and t["short_saved"]
    assert 'data-th-shape="tall"' in client.get(f"/moment/{m}").text


class ThumbRunner:
    """Claude's thumbnail versions, as the 'thumb' job asks for them."""

    def __init__(self):
        self.calls = []

    def run(self, prompt, schema, *, system, cwd, allow_read=False, model=None, effort=None, images=None):
        self.calls.append({"prompt": prompt, "schema": schema, "system": system, "images": images})
        return ClaudeResult({"versions": [
            {"name": "reaction", "why": "her face", "lines": ["bg | blur | 1", "cutout | 1 | auto | right | 0.9",
                                                              "text | NO *WAY* | left top | 0.18", "arrow | text | cutout"]},
            {"name": "moment", "why": "what happened", "lines": ["bg | frame | 2 | 1.5 | centre", "ring | centre | red",
                                                                 "made up | line"]}],
            "note": "frame 2 is blurry"}, "claude-opus-5-5", 1, {"usage": {"output_tokens": 50}})


def test_claude_composes_thumbnail_versions(site, monkeypatch):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    monkeypatch.setattr(co, "download", lambda d: (_ for _ in ()).throw(co.CutoutError("offline")))
    deps.claude = runner = ThumbRunner()
    r = client.post(f"/moment/{m}/thumb/ai", json={"shape": "wide"}).json()
    assert r["ok"] and r["ai"]["state"] == "queued"
    assert client.post(f"/moment/{m}/thumb/ai", json={"shape": "round"}).status_code == 400
    v = conn.execute("SELECT video_id FROM moments WHERE id = ?", (m,)).fetchone()["video_id"]
    jobs.enqueue(conn, v, "analyze")
    assert jobs.claim(conn)["stage"] == "thumb"                                         # you're waiting for it
    conn.execute("UPDATE jobs SET status = 'pending' WHERE status = 'running'")
    conn.execute("DELETE FROM jobs WHERE stage = 'analyze' AND moment_id IS NULL")
    Worker(cfg, deps=deps).run(once=True)
    call = runner.calls[0]
    assert call["images"] and "1280×720" in call["prompt"] and "3 versions" in call["prompt"]
    assert "cutout | N | WHO" in call["system"] and call["schema"]["required"] == ["versions"]
    d = client.get(f"/api/moment/{m}/thumb/ai").json()
    assert d["ai"]["state"] == "done" and d["ideas"]["note"] == "frame 2 is blurry"
    one, two = d["ideas"]["versions"]
    assert one["name"] == "reaction" and [ly["kind"] for ly in one["layers"]] == ["bg", "frame", "text", "sticker"]
    assert "framed" in one["warnings"][0]                                               # no cut-out model: a framed crop
    assert two["layers"][0]["zoom"] == 1.5 and len(two["warnings"]) == 1
    img = client.get(one["url"])
    assert img.headers["content-type"] == "image/jpeg" and Image.open(io.BytesIO(img.content)).size == (1280, 720)
    assert client.get(f"/moment/{m}/thumb/idea/7.jpg").status_code == 404
    # Nothing saved yet: the maker opens on Claude's first version.
    t = client.get(f"/api/moment/{m}/thumb").json()
    assert t["source"] == "claude" and t["layers"] == one["layers"] and t["ideas"]["versions"]
    msg = conn.execute("SELECT message FROM jobs WHERE stage = 'thumb'").fetchone()["message"]
    assert "2 thumbnail version(s)" in msg


def test_cutouts_are_made_once_and_kept(site, monkeypatch):  # noqa: F811
    from chatwatcher.brand import service as bs
    from chatwatcher.edit import service as es
    cfg, conn, client, _ = site
    m = mid(conn)
    calls = []

    def fake_cut(data_dir, frame, box):
        calls.append(box)
        return Image.new("RGBA", (100, 200), (255, 0, 0, 255))
    monkeypatch.setattr(co, "cut", fake_cut)
    clip = es.clip_ref(conn, m)
    a = bs.cutout_of(cfg, clip, 2.0, (0.1, 0.1, 0.5, 0.9))
    b = bs.cutout_of(cfg, clip, 2.0, (0.1, 0.1, 0.5, 0.9))
    assert a.size == b.size == (100, 200) and len(calls) == 1 and list((clip.folder / "thumbs").glob("cut_*.png"))
    spr = client.post(f"/moment/{m}/thumb/sprite", json={"layer": L.make("cutout", t=2, sx=0.2, sy=0.1, sw=0.5, sh=0.9, w=0.25)})
    assert spr.headers["content-type"] == "image/png" and len(calls) == 2              # another part of the frame
    assert co.grow({"x": 0.7, "y": 0.3, "w": 0.2, "h": 0.4}) == (0.55, 0.16, 0.45, 0.84)   # clamped at the right edge


def test_sticker_library(site):  # noqa: F811
    cfg, conn, client, _ = site
    buf = io.BytesIO()
    pic = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
    pic.paste((255, 0, 0, 255), (50, 50, 250, 150))
    pic.save(buf, "PNG")
    r = client.post("/brand/stickers", files=[("files", ("Shocked Face!.png", buf.getvalue(), "image/png"))],
                    follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert br.list_stickers(cfg) == ["shocked_face.png"]
    with Image.open(br.sticker_dir(cfg) / "shocked_face.png") as im:
        assert im.size == (200, 100)                                                    # trimmed
    assert client.get("/brand/stickers/shocked_face.png").status_code == 200
    assert client.get("/brand/stickers/nope.png").status_code == 404
    assert "shocked_face" in client.get("/brand").text
    bad = client.post("/brand/stickers", files=[("files", ("x.txt", b"hi", "text/plain"))], follow_redirects=False)
    assert "error=" in bad.headers["location"]
    client.post("/brand/stickers/remove/shocked_face.png")
    assert br.list_stickers(cfg) == []


def test_cutout_keeps_only_the_character_in_the_middle():
    a = Image.new("L", (600, 400), 0)
    a.paste(255, (250, 50, 350, 400))          # her, across the middle
    a.paste(255, (20, 100, 120, 400))          # someone beside her
    kept = co.keep_middle(a)
    assert kept.getpixel((300, 200)) == 255 and kept.getpixel((70, 200)) == 0
    empty = Image.new("L", (600, 400), 0)
    empty.paste(255, (20, 100, 120, 400))      # nobody in the middle: left as it was
    assert co.keep_middle(empty).getpixel((70, 200)) == 255
