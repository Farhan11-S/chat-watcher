"""Named formats: the briefs AI edit follows (eurobeat, tuff, hopecore, the YOU DIED ending…)."""

import json

from chatwatcher.edit import formats
from chatwatcher.edit import music as mus
from chatwatcher.edit.service import clip_ref

from test_direct import WORDS, FakeDirector, answer
from test_web import mid, site  # noqa: F401  (fixture)

MUSIC = [{"id": "drift", "mood": "eurobeat"}, {"id": "wand", "mood": "phonk"}, {"id": "soft", "mood": "hopecore"}]
SOUNDS = [{"id": "vine_boom", "name": "Vine boom", "description": "bass hit"}, {"id": "you_died", "name": "YOU DIED",
                                                                             "description": ""}]
OVERLAYS = [{"id": "3_hours_later", "name": "3 hours later", "description": "time card"},
            {"id": "shock_ring", "name": "Shock ring", "description": ""}]


def test_every_format_is_known_and_its_moods_exist():
    assert formats.clean("TUFF ") == "tuff" and formats.clean("auto") == "auto" and formats.clean("nope") == ""
    for f in formats.FORMATS.values():
        assert set(f["moods"]) <= set(mus.MOODS) and f["brief"] and f["for"]
    assert [x["key"] for x in formats.info()] == list(formats.FORMATS)


def test_briefs_name_what_fits():
    assert formats.fits("tuff", MUSIC, SOUNDS, OVERLAYS) == {"music": ["wand"], "sounds": ["vine_boom"],
                                                             "overlays": ["shock_ring"]}
    b = formats.brief("died", MUSIC, SOUNDS, OVERLAYS)
    assert "FORMAT" in b and "format = died" in b and "you_died" in b and "Music for it" not in b
    assert "none in the library with mood sad" in formats.brief("demotivational", MUSIC, SOUNDS, OVERLAYS)
    assert "3_hours_later" in formats.brief("sitcom", MUSIC, SOUNDS, OVERLAYS)
    auto = formats.brief("auto", MUSIC, SOUNDS, OVERLAYS)
    assert all(k + " (" in auto for k in formats.FORMATS) and "or none" in auto
    assert formats.brief("", MUSIC, SOUNDS, OVERLAYS) == ""


def test_ai_edit_in_a_format(site):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    (clip_ref(conn, m).folder / "words.json").write_text(json.dumps(WORDS))
    fake = FakeDirector(answer(["p2-p4"]) | {"format": "died"}, answer(["p2-p4"], note="Longer card.") | {"format": "died"})
    deps.claude = fake
    d = client.post(f"/moment/{m}/autoedit", json={"segments": [[0, 20, 0]], "kind": "long", "format": "died"}).json()
    assert d["ok"] and "as Ironic death (YOU DIED)" in d["reply"]
    assert "FORMAT: the director wants this edit in the Ironic death (YOU DIED) format" in fake.calls[0]["prompt"]
    assert "format" in fake.calls[0]["schema"]["required"]
    # Directing it keeps the format in view.
    d = client.post(f"/moment/{m}/direct", json={"kind": "long", "message": "hold the card longer", "format": "died"}).json()
    assert d["ok"] and "format = died" in fake.calls[1]["prompt"]


def test_no_format_no_brief(site):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    (clip_ref(conn, m).folder / "words.json").write_text(json.dumps(WORDS))
    fake = FakeDirector(answer(["p2-p4"]))
    deps.claude = fake
    assert client.post(f"/moment/{m}/autoedit", json={"segments": [[0, 20, 0]], "kind": "long"}).json()["ok"]
    assert "FORMAT:" not in fake.calls[0]["prompt"]
