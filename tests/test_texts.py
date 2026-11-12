"""Text callouts: big words over the picture, placed by you or the AI edit, drawn by libass."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from chatwatcher.edit import texts as txt
from chatwatcher.edit.service import EditError, clean_effects, clip_ref
from chatwatcher.worker import Worker

from test_direct import WORDS, FakeDirector, answer
from test_web import mid, site  # noqa: F401

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def test_effects_check_texts():
    fx = clean_effects([{"type": "text", "t": 1, "text": "  no  no 💀 NO ", "style": "stamp", "at": "Face ",
                         "size": 9, "d": 99, "dx": 3},
                        {"type": "text", "t": 2, "text": "wait", "style": "fancy", "color": "mauve"}], 10, set())
    assert fx[0] | {"id": ""} == {"id": "", "type": "text", "t": 1.0, "text": "no no NO", "style": "stamp",
                                  "d": txt.D_LIMITS[2], "color": "red", "at": "face", "dx": 1.0, "dy": 0.0,
                                  "size": txt.SIZE_RANGE[1], "by": "you", "why": ""}
    assert (fx[1]["style"], fx[1]["color"], fx[1]["at"], fx[1]["d"]) == ("pop", "white", "middle", txt.D_LIMITS[0])
    assert "size" not in fx[1]                                 # placed() picks one for a Short or a 16:9 picture
    with pytest.raises(EditError, match="nothing to say"):
        clean_effects([{"type": "text", "t": 1, "text": "🔥"}], 10, set())


def test_callouts_as_ass():
    boxes = {"face": {"x": 0.6, "y": 0.1, "w": 0.2, "h": 0.2}}
    fx = [{"type": "text", "t": 2.0, "d": 1.5, "text": "no no no", "style": "stamp", "color": "red", "at": "face",
           "dx": 0, "dy": 0.1},
          {"type": "text", "t": 5.0, "d": 1.0, "text": "← that was the *backup*", "style": "label", "at": "middle"}]
    items = txt.placed(fx, boxes, None)
    assert (items[0].x, round(items[0].y, 3)) == (0.7, 0.3) and items[1].size == txt.default_size("label", False)
    stamp, label = txt.events(txt.local(items, 0, 10), 1920, 1080)
    assert "\\pos(1344,324)" in stamp and "\\frz7" in stamp and "NO NO NO" in stamp and "\\fscx260" in stamp
    assert "\\c&H303BFF&" in stamp and stamp.startswith("Dialogue: 2,0:00:02.00,0:00:03.50,Callout,")
    assert ",Label," in label and "\\fad(" in label and "&H00D6FF&" in label      # *backup* in the highlight
    # Split by a cut: it springs in only in the first part, fades out only in the last.
    first, = txt.events(txt.local(items[:1], 0, 2.8), 1920, 1080)
    second, = txt.events(txt.local(items[:1], 2.8, 5), 1920, 1080)
    assert "\\fscx260" in first and "\\fad" not in first
    assert "\\fscx260" not in second and "\\fad(0," in second and ",0:00:00.00,0:00:00.70," in second


def test_ai_edit_adds_callouts(site):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    (clip_ref(conn, m).folder / "words.json").write_text(json.dumps(WORDS))
    a = answer(["p2-p4"]) | {"texts": ["9.6 | 1.2 | NO NO NO | stamp | middle | the fatal click",
                                       "9.8 | 2 | 💀 | pop | middle | emoji only: dropped",
                                       "40 | 1 | later | pop | middle | outside the cut",
                                       "10.2 | 9 | wait… | fancy | nowhere | defaults"]}
    fake = FakeDirector(a)
    deps.claude = fake
    d = client.post(f"/moment/{m}/autoedit", json={"segments": [[0, 20, 0]], "kind": "short"}).json()
    assert d["ok"] and "2 texts" in d["reply"]
    assert "texts" in fake.calls[0]["schema"]["required"]
    after = client.get(f"/api/moment/{m}/edit?kind=short").json()
    texts = [f for f in after["effects"] if f["type"] == "text"]
    assert [(f["text"], f["style"], f["color"], f["at"], f["by"]) for f in texts] == [
        ("NO NO NO", "stamp", "red", "middle", "claude"), ("wait…", "pop", "white", "middle", "claude")]
    assert texts[0]["size"] == txt.default_size("stamp", True) and texts[1]["d"] <= 20 - 10.2
    assert after["text_info"]["styles"] == list(txt.STYLES)


def red_share(video: Path, t: float) -> float:
    """How much of the middle of the frame at t is bright red."""
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(video), "-frames:v", "1",
                          "-vf", "scale=160:120", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    n = red = 0
    for y in range(40, 80):
        for x in range(30, 130):
            k = (y * 160 + x) * 3
            n += 1
            red += out[k] > 180 and out[k + 1] < 90 and out[k + 2] < 90
    return red / n


@needs_ffmpeg
def test_callout_is_rendered(site):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    edit = {"segments": [[2, 6, 0]], "source": "long",
            "effects": [{"type": "text", "t": 3.0, "d": 1.5, "text": "NO NO", "style": "stamp", "size": 0.16}]}
    assert client.post(f"/moment/{m}/render", json=edit).json()["ok"]
    Worker(cfg, deps=deps).run(once=True)
    job = conn.execute("SELECT status, message FROM jobs WHERE moment_id = ? AND stage = 'render'", (m,)).fetchone()
    assert job["status"] == "done", job["message"]
    out = clip_ref(conn, m).out_path("long")
    before, during = red_share(out, 0.4), red_share(out, 1.6)          # 1.6 s into the edit = 3.6 s of the clip
    assert during > before + 0.05, (before, during)
