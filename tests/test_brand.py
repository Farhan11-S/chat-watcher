import io
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

from chatwatcher import db, jobs
from chatwatcher.brand import brand as br
from chatwatcher.brand import intro as it
from chatwatcher.brand import thumb as th
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

def test_thumbnail_settings_and_drawing():
    s = th.clean({"t": 99, "zoom": 9, "cx": -1, "text": " *BIG*   news ", "style": "nope", "logo": 0}, 20)
    assert s["t"] == pytest.approx(19.95) and s["zoom"] == 3.0 and s["cx"] == 0 and s["text"] == "*BIG* news"
    assert s["style"] == "band" and s["logo"] is False
    with pytest.raises(th.ThumbError):
        th.clean({"zoom": "x"}, 20)
    frame = Image.new("RGB", (1920, 1080), (40, 120, 90))
    for style in th.STYLES:
        img = th.draw(frame, th.clean({"text": "SHE *SCREAMED* 💀", "style": style, "cx": 0.8}, 20), br.Brand(), None,
                      None, None)
        assert img.size == (th.W, th.H)
    assert th.text_side(1920, 1080, th.clean({"cx": 0.8}, 20)) == "left"
    assert th.text_side(1920, 1080, th.clean({"cx": 0.2}, 20)) == "right"
    ink, lines = th.layout("SHE BLEW UP OUR EARS", None, 560, 500)
    widths = [ink.length("".join(p for p, _ in ln).rstrip()) for ln in lines]
    assert len(lines) >= 2 and max(widths) <= 560 and min(widths) > max(widths) * 0.4      # even lines


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

    # Thumbnail: Claude's text by default, a preview for any settings, saved as thumbnail.jpg.
    t = client.get(f"/api/moment/{m}/thumb").json()
    assert t["ok"] and t["saved"] is None and t["settings"]["text"] == t["defaults"]["text"]
    pv = client.get(f"/moment/{m}/thumb.jpg", params={"s": json.dumps({"text": "HELLO *THERE*", "zoom": 1.5})})
    assert pv.status_code == 200 and pv.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(pv.content)).size == (1280, 720)
    assert client.get(f"/moment/{m}/thumb.jpg", params={"s": "{nope"}).status_code == 400
    s = client.post(f"/moment/{m}/thumb", json={"settings": {"text": "SAVED", "t": 3, "style": "shade"}}).json()
    assert s["ok"] and s["saved"] and s["settings"]["text"] == "SAVED" and s["settings"]["t"] == pytest.approx(3)
    assert client.get(f"/api/moment/{m}/thumb").json()["settings"]["style"] == "shade"
    assert client.get(s["saved"]["url"]).headers["content-type"] == "image/jpeg"

    page = client.get(f"/moment/{m}").text
    assert 'data-intro="' in page and 'data-thumb="' in page and "/static/maker.js" in page

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
    s = th.clean({"text": "SHE *SCREAMED* 💀", "place": "bottom", "zoom": 1.5, "cx": 0.5, "cy": 0.6}, 20,
                 th.TALL_DEFAULTS)
    assert s["place"] == "bottom" and th.clean({"place": "nope"}, 20, th.TALL_DEFAULTS)["place"] == "top"
    for style in th.STYLES:
        for place in th.PLACES:
            img = th.draw_tall(crop, s | {"style": style, "place": place}, br.Brand(), None, None, None)
            assert img.size == (th.TW, th.TH)
    x0, y0, x1, y1 = th.tall_box(th.clean({"zoom": 2, "cx": 1, "cy": 0}, 20, th.TALL_DEFAULTS))
    assert (x1, y0) == (th.TW, 0) and x1 - x0 == th.TW // 2                   # clamped into the picture


def test_cover_on_the_moment_page(site):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    t = client.get(f"/api/moment/{m}/thumb?shape=tall").json()
    assert t["ok"] and t["shape"] == "tall" and t["size"] == [1080, 1920] and t["layout"] == "crop"
    assert not t["short_saved"] and t["settings"]["place"] == "top" and not t["saved"]
    pv = client.get(f"/moment/{m}/thumb.jpg", params={"shape": "tall", "s": json.dumps({"text": "HI *THERE*"})})
    assert pv.status_code == 200 and Image.open(io.BytesIO(pv.content)).size == (1080, 1920)
    assert client.get(f"/api/moment/{m}/thumb?shape=round").status_code == 400

    s = client.post(f"/moment/{m}/thumb", json={"settings": {"text": "COVER", "t": 2, "place": "middle"},
                                                 "shape": "tall"}).json()
    assert s["ok"] and s["saved"]["url"].startswith(f"/media/thumb/{m}?shape=tall")
    assert Path(s["saved"]["path"]).name == "short_cover.jpg"
    assert Image.open(s["saved"]["path"]).size == (1080, 1920)
    again = client.get(f"/api/moment/{m}/thumb?shape=tall").json()["settings"]
    assert (again["text"], again["place"], round(again["t"], 2)) == ("COVER", "middle", 2.0)
    # The video's thumbnail is untouched: still Claude's pick, not saved.
    wide = client.get(f"/api/moment/{m}/thumb").json()
    assert not wide["saved"] and wide["settings"] == wide["defaults"]
    media = client.get(f"/media/thumb/{m}?shape=tall&dl=1")
    assert media.status_code == 200 and "short_cover.jpg" in media.headers["content-disposition"]
    assert client.get(f"/media/thumb/{m}").status_code == 404

    # It follows the Short's layout.
    client.post(f"/moment/{m}/edit", json={"kind": "short", "segments": [[1, 5]], "layout": {"type": "fit"}})
    t = client.get(f"/api/moment/{m}/thumb?shape=tall").json()
    assert t["layout"] == "fit" and t["short_saved"]
    assert 'data-th-shape="tall"' in client.get(f"/moment/{m}").text
