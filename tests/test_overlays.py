"""Green-screen overlays: keyed once when added, laid over the picture with their sound."""

import shutil
import subprocess
from pathlib import Path

import pytest

from chatwatcher.edit import overlays as ovl
from chatwatcher.edit.service import clip_ref, clean_effects, EditError
from chatwatcher.worker import Worker

from test_web import mid, site  # noqa: F401

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def green_clip(path: Path, seconds: float = 1.0, color: str = "0x10FF00") -> Path:
    """A green screen with a red box in the middle and a tone, like a meme overlay."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=640x360:d={seconds}:r=30",
                    "-f", "lavfi", "-i", f"sine=frequency=880:duration={seconds}",
                    "-vf", "drawbox=x=220:y=110:w=200:h=140:color=red:t=fill",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def pixel(video: Path, t: float, x: float, y: float) -> tuple[int, int, int]:
    """The colour at (x, y) (shares of the frame) at time t."""
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(video), "-frames:v", "1",
                          "-vf", "scale=64:64", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    k = (int(y * 63) * 64 + int(x * 63)) * 3
    return out[k], out[k + 1], out[k + 2]


def test_effects_check_overlays():
    fx = clean_effects([{"type": "overlay", "t": 1, "overlay": "pings", "d": 99, "size": 9, "at": "Face "}], 10, set(),
                       {"pings"})
    assert fx[0] | {"id": ""} == {"id": "", "type": "overlay", "t": 1.0, "overlay": "pings", "d": ovl.MAX_SECONDS,
                                  "at": "face", "dx": 0.0, "dy": 0.0, "size": ovl.SIZE_RANGE[1], "gain": 0.0,
                                  "mode": "both", "by": "you", "why": ""}
    # Moved by hand (kept to OFFSET_RANGE), and only its sound or only its picture.
    fx = clean_effects([{"type": "overlay", "t": 1, "overlay": "pings", "dx": 0.25, "dy": -7, "mode": "sound"},
                        {"type": "overlay", "t": 2, "overlay": "pings", "mode": "nonsense"}], 10, set(), {"pings"})
    assert (fx[0]["dx"], fx[0]["dy"], fx[0]["mode"]) == (0.25, ovl.OFFSET_RANGE[0], "sound")
    assert fx[1]["mode"] == "both"
    with pytest.raises(EditError, match="position"):
        clean_effects([{"type": "overlay", "t": 1, "overlay": "pings", "dx": "left"}], 10, set(), {"pings"})


def test_place_moves_from_the_anchor():
    boxes = {"face": {"x": 0.6, "y": 0.1, "w": 0.2, "h": 0.2}}
    assert ovl.place("full", boxes, None) == (0.5, 0.5)
    assert ovl.place("full", boxes, None, 0.2, -0.1) == pytest.approx((0.7, 0.4))
    assert ovl.place("face", boxes, None, -0.1, 0.05) == pytest.approx((0.6, 0.25))
    with pytest.raises(EditError, match="library"):
        clean_effects([{"type": "overlay", "t": 1, "overlay": "nope"}], 10, set(), {"pings"})


@needs_ffmpeg
def test_overlay_library_and_render(site, tmp_path):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)

    # Added on the Library page: keyed, with its sound; a clip with no screen colour is refused.
    files = [("files", ("pings.mp4", green_clip(tmp_path / "pings.mp4").read_bytes(), "video/mp4"))]
    r = client.post("/overlays/upload", files=files, data={"description": "dumb call"}, follow_redirects=False)
    assert "msg=" in r.headers["location"], r.headers["location"]
    o = ovl.get_overlay(conn, "pings")
    assert int(o["key_color"][2:4], 16) > 240 and int(o["key_color"][:2], 16) < 40 and o["sound"] and o["width"] == 640
    assert client.get("/media/overlay/pings").headers["content-type"] == "video/webm"
    assert client.get("/media/overlay/pings/sound").headers["content-type"] == "audio/flac"
    assert client.get("/media/overlay/..%2Fx").status_code == 404
    page = client.get("/sounds").text
    assert "Overlays" in page and 'src="/media/overlay/pings"' in page and ">Library<" in page
    plain = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=d=1:s=320x240",
                            str(tmp_path / "plain.mp4")], check=True)
    assert plain.returncode == 0
    r = client.post("/overlays/upload", files=[("files", ("plain.mp4", (tmp_path / "plain.mp4").read_bytes(),
                                                                       "video/mp4"))], follow_redirects=False)
    assert "error=" in r.headers["location"] and "green" in r.headers["location"]

    # On the long edit: the red box over the picture, no green left, and its sound mixed in.
    d = client.get(f"/api/moment/{m}/edit").json()
    assert d["overlays"][0]["id"] == "pings" and d["overlays"][0]["sound"]
    seg = {"segments": [[2, 6, 0]], "source": "long",
           "effects": [{"type": "overlay", "overlay": "pings", "t": 3.0, "d": 1.0, "at": "full", "size": 1.0}]}
    assert client.post(f"/moment/{m}/render", json=seg).json()["ok"]
    Worker(cfg, deps=deps).run(once=True)
    job = conn.execute("SELECT status, message FROM jobs WHERE moment_id = ? AND stage = 'render'", (m,)).fetchone()
    assert job["status"] == "done", job["message"]
    out = clip_ref(conn, m).out_path("long")
    r_, g_, b_ = pixel(out, 1.4, 0.5, 0.5)                   # 1.4 s into the edit = 3.4 s of the clip
    assert r_ > 170 and g_ < 90 and b_ < 90
    corner = pixel(out, 1.4, 0.05, 0.05)
    assert not (corner[1] > 200 and corner[0] < 80)           # the green is gone
    assert pixel(out, 0.3, 0.5, 0.5) != (r_, g_, b_)          # not there before it starts

    # Moved right by a quarter and made smaller: the box is off the middle, at 75 %.
    moved = {"segments": [[2, 6, 0]], "source": "long",
             "effects": [{"type": "overlay", "overlay": "pings", "t": 3.0, "d": 1.0, "at": "full", "size": 0.5,
                          "dx": 0.25, "dy": 0}]}
    assert client.post(f"/moment/{m}/render", json=moved).json()["ok"]
    Worker(cfg, deps=deps).run(once=True)
    r_, g_, b_ = pixel(out, 1.4, 0.75, 0.5)
    assert r_ > 170 and g_ < 90 and b_ < 90
    centre = pixel(out, 1.4, 0.5, 0.5)
    assert not (centre[0] > 170 and centre[1] < 90 and centre[2] < 90)

    # Sound only: no box on the picture.
    quiet = {"segments": [[2, 6, 0]], "source": "long",
             "effects": [{"type": "overlay", "overlay": "pings", "t": 3.0, "d": 1.0, "at": "full", "size": 1.0,
                          "mode": "sound"}]}
    assert client.post(f"/moment/{m}/render", json=quiet).json()["ok"]
    Worker(cfg, deps=deps).run(once=True)
    r_, g_, b_ = pixel(out, 1.4, 0.5, 0.5)
    assert not (r_ > 170 and g_ < 90 and b_ < 90)

    # On a Short, "full" covers the picture.
    short = {"kind": "short", "segments": [[2, 6, 0]],
             "effects": [{"type": "overlay", "overlay": "pings", "t": 3.0, "d": 1.0, "at": "full", "size": 1.78}]}
    assert client.post(f"/moment/{m}/render", json=short).json()["ok"]
    Worker(cfg, deps=deps).run(once=True)
    sp = clip_ref(conn, m).out_path("short")
    r_, g_, b_ = pixel(sp, 1.4, 0.5, 0.5)
    assert r_ > 170 and g_ < 90 and b_ < 90

    # Deleted from the library: the edit keeps its marker, the render skips it.
    client.post("/overlays/pings/delete")
    assert ovl.get_overlay(conn, "pings") is None and not list(ovl.overlays_dir(cfg).glob("pings.*"))
