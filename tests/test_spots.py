"""Spotlights: dim the rest of the picture round the thing that matters, and ring it."""

import json
import shutil
import subprocess

import pytest

from chatwatcher.edit import layout as lay
from chatwatcher.edit import spots as spt
from chatwatcher.edit.service import EditError, clean_effects, clip_ref
from chatwatcher.worker import Worker

from test_direct import WORDS, FakeDirector, answer
from test_web import mid, site  # noqa: F401

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def test_effects_check_spots():
    fx = clean_effects([{"type": "spot", "t": 1, "x": 0.9, "y": -0.5, "w": 0.4, "h": 0.001, "d": 99, "style": "circle"},
                        {"type": "spot", "t": 2, "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2, "style": "?", "color": "?"}],
                       10, set())
    assert fx[0] | {"id": ""} == {"id": "", "type": "spot", "t": 1.0, "d": spt.D_LIMITS[2], "style": "circle",
                                  "color": "red", "x": 0.8, "y": -0.01, "w": 0.4, "h": spt.MIN_SIZE,
                                  "by": "you", "why": ""}
    assert (fx[1]["style"], fx[1]["color"]) == ("spotlight", "white")
    with pytest.raises(EditError, match="without a place"):
        clean_effects([{"type": "spot", "t": 1, "x": 0.1}], 10, set())


def test_spot_follows_the_shorts_panels():
    # Stacked: the game on top (clip x 0-0.6 → the top 60 %), her face below.
    panels = [lay.Panel((0.0, 0.0, 0.6, 1.0), (0.0, 0.0, 1.0, 0.6)), lay.Panel((0.6, 0.5, 0.4, 0.5), (0.0, 0.6, 1.0, 0.4))]
    x, y, w, h = spt.map_box((0.2, 0.4, 0.2, 0.2), panels)
    assert (round(x, 4), round(y, 4), round(w, 4), round(h, 4)) == (0.3333, 0.24, 0.3333, 0.12)
    assert spt.map_box((0.65, 0.1, 0.1, 0.1), panels) is None           # not in the Short's picture
    assert spt.map_box((0.1, 0.2, 0.3, 0.4), None) == (0.1, 0.2, 0.3, 0.4)


def test_mask():
    p = spt.Placed(0, 1, (0.25, 0.25, 0.5, 0.5), "spotlight", (255, 59, 48))
    m = spt.mask(p, 200, 100)
    assert m[50, 100, 3] == 0                                     # see-through in the middle
    assert abs(m[2, 2, 3] - round(spt.DIM * 255)) <= 1 and tuple(m[2, 2, :3]) == (0, 0, 0)   # dimmed outside
    ring = m[50, 50]                                              # on the ellipse's left edge
    assert ring[3] > 200 and ring[0] > 200 and ring[1] < 90
    assert spt.mask(spt.Placed(0, 1, p.box, "circle", (255, 59, 48)), 200, 100)[2, 2, 3] == 0   # ring only
    assert spt.mask(spt.Placed(0, 1, p.box, "dim", (255, 59, 48)), 200, 100)[50, 50, 3] < 80     # no ring


def test_ai_edit_adds_spotlights(site):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    (clip_ref(conn, m).folder / "words.json").write_text(json.dumps(WORDS))
    a = answer(["p2-p4"]) | {"spots": ["9.6 | 1.2 | x 40-60 y 20-35 | circle | the Yes button",
                                       "10 | 2 | nowhere | spotlight | not a region: dropped",
                                       "40 | 1 | x 1-5 y 1-5 | dim | outside the cut"]}
    fake = FakeDirector(a)
    deps.claude = fake
    d = client.post(f"/moment/{m}/autoedit", json={"segments": [[0, 20, 0]], "kind": "long"}).json()
    assert d["ok"] and "1 spotlight" in d["reply"] and "spots" in fake.calls[0]["schema"]["required"]
    spots = [f for f in client.get(f"/api/moment/{m}/edit").json()["effects"] if f["type"] == "spot"]
    assert [(f["style"], f["color"], f["x"], f["y"], f["w"], f["h"], f["by"]) for f in spots] == [
        ("circle", "red", 0.4, 0.2, 0.2, 0.15, "claude")]


def luma(video, t, x, y):
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(video), "-frames:v", "1",
                          "-vf", "scale=64:64", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True, check=True).stdout
    return out[int(y * 63) * 64 + int(x * 63)]


@needs_ffmpeg
def test_spotlight_is_rendered(site):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    plain = {"segments": [[2, 6, 0]], "source": "long", "effects": []}
    spot = plain | {"effects": [{"type": "spot", "t": 3.0, "d": 2.0, "x": 0.3, "y": 0.3, "w": 0.4, "h": 0.4,
                                 "style": "dim"}]}
    lit = {}
    for name, edit in (("plain", plain), ("spot", spot)):
        assert client.post(f"/moment/{m}/render", json=edit).json()["ok"]
        Worker(cfg, deps=deps).run(once=True)
        job = conn.execute("SELECT status, message FROM jobs WHERE moment_id = ? AND stage = 'render' "
                           "ORDER BY id DESC", (m,)).fetchone()
        assert job["status"] == "done", job["message"]
        out = clip_ref(conn, m).out_path("long")
        lit[name] = [luma(out, 1.8, 0.1, 0.5), luma(out, 1.8, 0.95, 0.9), luma(out, 1.8, 0.5, 0.5)]
    (c0, c1, mid0), (s0, s1, mid1) = lit["plain"], lit["spot"]
    assert s0 < c0 * 0.6 and s1 < c1 * 0.6, lit          # dimmed outside
    assert abs(mid1 - mid0) <= 6, lit                     # untouched inside
