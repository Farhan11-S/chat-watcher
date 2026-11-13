"""Inserts: an X post, a picture or a video from a link, kept with one clip and shown as a
full-screen cutaway (an overlay at "fill")."""

import io
import json
import shutil

import pytest
from PIL import Image

from chatwatcher.edit import autoedit as ae
from chatwatcher.edit import inserts
from chatwatcher.edit import overlays as ovl
from chatwatcher.edit.service import clip_ref
from chatwatcher.worker import Worker

from test_direct import WORDS, FakeDirector, answer
from test_overlays import pixel
from test_web import mid, site  # noqa: F401

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def png(color=(220, 20, 20), size=(400, 400)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


POST = {"tweet": {"text": "lock in womanosphere", "created_at": "Tue Mar 21 20:50:14 +0000 2026",
                  "author": {"name": "Some One", "screen_name": "someone", "avatar_url": "https://pbs.example/a.jpg"},
                  "media": {"photos": [{"url": "https://pbs.example/p.jpg"}]}}}


def fake_web(monkeypatch, calls=None):
    def get(url, limit=inserts.MAX_BYTES, timeout=30):
        if calls is not None:
            calls.append(url)
        if url.startswith("https://api.fxtwitter.com/status/"):
            return json.dumps(POST).encode(), "application/json"
        if "missing" in url:
            raise inserts.InsertError(f"couldn't download {url}: 404")
        return png(), "image/png"
    monkeypatch.setattr(inserts, "_get", get)


def test_links_and_post_ids():
    msg = "use this https://x.com/someone/status/123?s=20, and https://twitter.com/i/web/status/456. (thanks)"
    assert inserts.links(msg) == ["https://x.com/someone/status/123?s=20", "https://twitter.com/i/web/status/456"]
    assert inserts.x_post_id("https://x.com/someone/status/123?s=20") == "123"
    assert inserts.x_post_id("https://fixupx.com/a/status/9") == "9"
    assert inserts.x_post_id("https://youtube.com/watch?v=x") is None
    assert inserts._start_of("https://youtu.be/x?t=1m30s") == 90 and inserts._start_of("https://y/x?t=42") == 42
    assert inserts._start_of("https://y/x") == 0
    assert inserts.is_link("https://a.b/c") and not inserts.is_link("i3")


def test_post_card_draws_the_post():
    card = Image.open(io.BytesIO(inserts.post_card("Some One", "someone", "lock in " * 30, None, png(), "Mar 21 2026")))
    assert card.width == inserts.CARD_W and card.height > 600
    assert card.getpixel((inserts.CARD_W // 2, card.height - 200))[0] > 150      # the photo, near the bottom


def test_cutaways_from_claude():
    ins = [{"id": "x_post", "duration_s": 15.0, "tags": "insert, x post"},
           {"id": "clip", "duration_s": 3.0, "tags": "insert, video"}]
    fx = ae.cutaway_effects(["2 | 0.5 | x_post | the post", "3 | 9 | clip | overlaps the first", "12 | 9 | clip | x",
                             "40 | 2 | x_post | outside", "5 | 2 | nope | unknown"], [[0, 20]], ins)
    assert [(f["overlay"], f["t"], f["d"], f["at"]) for f in fx] == [("x_post", 2.0, 2.0, "fill"), ("clip", 12.0, 3.0, "fill")]


@needs_ffmpeg
def test_insert_from_a_link_and_its_cutaway(site, monkeypatch):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    calls = []
    fake_web(monkeypatch, calls)

    # An X post becomes a post card (a still video), kept with this clip only.
    got = inserts.fetch(cfg, conn, m, "https://x.com/someone/status/123")
    assert len(got) == 1 and got[0]["moment_id"] == m and got[0]["file"].endswith(".mp4") and not got[0]["key_color"]
    assert "lock in womanosphere" in got[0]["description"] and got[0]["duration_s"] == pytest.approx(15, abs=0.2)
    assert inserts.fetch(cfg, conn, m, "https://x.com/someone/status/123") == got     # fetched once
    assert sum("fxtwitter" in c for c in calls) == 1
    assert client.get(f"/media/overlay/{got[0]['id']}").headers["content-type"] == "video/mp4"
    assert got[0]["id"] not in client.get("/sounds").text                          # not in the library
    d = client.get(f"/api/moment/{m}/edit").json()
    assert d["overlays"][0]["id"] == got[0]["id"] and d["overlays"][0]["insert"]
    assert ovl.list_overlays(conn) == []

    # A picture link, shown full screen: fitted in the middle over the blurred picture.
    pic = inserts.fetch(cfg, conn, m, "https://example.com/red.png")[0]
    seg = {"segments": [[2, 6, 0]], "source": "long",
           "effects": [{"type": "overlay", "overlay": pic["id"], "t": 3.0, "d": 2.0, "at": "fill"}]}
    assert client.post(f"/moment/{m}/render", json=seg).json()["ok"]
    Worker(cfg, deps=deps).run(once=True)
    job = conn.execute("SELECT status, message FROM jobs WHERE moment_id = ? AND stage = 'render'", (m,)).fetchone()
    assert job["status"] == "done", job["message"]
    out = clip_ref(conn, m).out_path("long")
    r_, g_, b_ = pixel(out, 1.5, 0.5, 0.5)
    assert r_ > 170 and g_ < 90 and b_ < 90
    assert pixel(out, 1.5, 0.03, 0.5)[0] < 170 or pixel(out, 1.5, 0.03, 0.5)[1] > 90   # a square fits: sides show the clip
    assert pixel(out, 0.3, 0.5, 0.5) != (r_, g_, b_)

    with pytest.raises(inserts.InsertError):
        inserts.fetch(cfg, conn, m, "https://example.com/missing.png")

    # From the editor: the page gets the new list, the insert first.
    r = client.post(f"/api/moment/{m}/insert", json={"url": "https://example.com/blue.png"}).json()
    assert r["ok"] and r["overlays"][0]["insert"] and r["added"][0] in [o["id"] for o in r["overlays"]]
    r = client.post(f"/api/moment/{m}/insert", json={"url": "https://example.com/missing.png"})
    assert r.status_code == 400 and "404" in r.json()["error"]


def test_direct_brings_in_a_link(site, monkeypatch):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    (clip_ref(conn, m).folder / "words.json").write_text(json.dumps(WORDS))
    fake_web(monkeypatch)
    monkeypatch.setattr(inserts, "_still", lambda _png, dest: dest.write_bytes(b""))
    monkeypatch.setattr(inserts, "_add", lambda cfg, conn, pk, made, *, name, description, url, tags: _row(
        conn, pk, name, description, url, tags))

    first = answer(["p2-p4"]) | {"cutaways": ["2.5 | 3 | x_post_someone | the post she means"]}
    fake = FakeDirector(first, answer(["p2-p4"]) | {"cutaways": ["2.5 | 3 | https://example.com/missing.png | x"]})
    deps.claude = fake
    d = client.post(f"/moment/{m}/direct", json={"segments": [[0, 20, 0]], "kind": "long", "new": True,
                                                 "message": "put her post in: https://x.com/someone/status/123"}).json()
    assert d["ok"] and d["applied"] and d["counts"]["cutaways"] == 1, d
    call = fake.calls[0]
    assert call["tools"] == ["WebSearch", "WebFetch"] and "x_post_someone" in call["prompt"]
    fx = json.loads(conn.execute("SELECT effects_json FROM edits WHERE moment_id = ?", (m,)).fetchone()[0])
    assert [(f["overlay"], f["at"], f["d"]) for f in fx if f["type"] == "overlay"] == [("x_post_someone", "fill", 3.0)]

    # A link Claude gives that can't be downloaded: a warning, no cutaway.
    d = client.post(f"/moment/{m}/direct", json={"kind": "long", "message": "another"}).json()
    assert d["ok"] and d["counts"]["cutaways"] == 0 and any("missing.png" in w for w in d["warnings"])


def _row(conn, pk, name, description, url, tags):
    oid = ovl.make_id(conn, name)
    conn.execute("INSERT INTO overlays (id, name, description, tags, source, file, duration_s, width, height, moment_id) "
                 "VALUES (?, ?, ?, ?, ?, ?, 15, 1080, 1080, ?)", (oid, name, description, tags, url, oid + ".mp4", pk))
    return ovl.get_overlay(conn, oid)
