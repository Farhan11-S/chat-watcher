from chatwatcher import db
from chatwatcher.subs import lines, speakers


def _words(*said):
    """[(start, end, text, voice)] → words.json with one line."""
    return {"voices": len({v for *_, v in said if v is not None}),
            "lines": [{"start": said[0][0], "end": said[-1][1], "text": "",
                       "words": [[a, b, f" {t}", 0.9, v] for a, b, t, v in said]}]}


def test_each_word_gets_the_voice_speaking_over_it():
    turns = [(0.0, 3.0, 0), (2.0, 4.0, 1), (6.0, 7.0, 1)]
    words = [(0.2, 0.6), (2.2, 2.6), (3.2, 3.6), (5.5, 5.8), (9.5, 9.8)]
    # 2.2 is in both: the voice before goes on; 5.5 is near voice 1's next stretch; 9.5 is
    # near nobody: the voice before.
    assert speakers.label(words, turns) == [0, 0, 1, 1, 1]
    # A lone word of another voice between two of one voice is a blip.
    assert speakers.label([(0.0, 0.3), (0.4, 0.6), (0.7, 1.0)], [(0, 0.35, 0), (0.35, 0.65, 1), (0.65, 1, 0)]) == [0, 0, 0]
    said = [[0.1 + 0.4 * i, 0.4 + 0.4 * i, " hi", 0.9] for i in range(4)] + [[6.2, 6.4, " yo", 0.9]] * 4
    data = speakers.assign({"lines": [{"words": said}]}, turns)
    assert data["voices"] == 2 and [w[4] for w in data["lines"][0]["words"]][::4] == [0, 1]
    # A voice with a few words (music, a game voice) joins its neighbours; renumbered by words.
    assert speakers.fold([3, 3, 3, 3, 5, 3, 3, 3, 3, 3, None]) == [0] * 10 + [None]
    assert speakers.fold([2] * 5 + [7] * 6) == [1] * 5 + [0] * 6


def test_two_voices_at_once_get_their_own_cues():
    one = _words((0.0, 0.4, "Hello", None), (0.5, 0.9, "there.", None))
    assert all("speaker" not in c for c in lines.build(one))
    # Voice 0 talks on while voice 1 shouts over her: two cues at the same time.
    both = _words((0.0, 0.3, "wait", 0), (0.3, 0.6, "NO", 1), (0.6, 0.9, "wait", 0), (0.9, 1.2, "WAY!", 1),
                  (1.2, 1.5, "wait.", 0))
    cues = lines.build(both)
    assert [(c["speaker"], c["text"]) for c in cues] == [(0, "wait wait wait."), (1, "NO WAY!")]
    assert cues[1]["t0"] < cues[0]["t1"]                       # they overlap: the render stacks them
    # A real answer in between ends the first voice's line.
    reply = _words((0.0, 0.4, "so", 0), (0.5, 1.4, "what do you mean", 1), (1.5, 1.8, "anyway", 0))
    assert [c["text"] for c in lines.build(reply)] == ["so", "what do you mean", "anyway"]
    # The editor keeps a cue's voice; a bad one is dropped.
    kept = lines.clean([{"t0": 0, "t1": 1, "text": "a", "speaker": 1}, {"t0": 1, "t1": 2, "text": "b", "speaker": "x"}], 5)
    assert kept[0]["speaker"] == 1 and "speaker" not in kept[1]


def test_voice_colours_names_and_the_render():
    v = speakers.clean({"0": {"name": " Gigi ", "color": "#ff8fc8"}, "1": {"name": "Kyoko", "color": "nope"},
                        "2": {"name": "gigi", "color": "00FF00"}, "3": {"name": ""}, "9": {"name": "x"}})
    assert v["0"] == {"name": "Gigi", "color": "FF8FC8"} and v["1"]["color"] == speakers.default_color(1)
    assert v["2"]["color"] == "FF8FC8"                          # same person, same colour
    assert "3" not in v and "9" not in v                        # nothing to keep / past the last person
    painted = speakers.paint([{"t0": 0, "t1": 1, "text": "hi", "speaker": 0}, {"t0": 1, "t1": 2, "text": "yo"}], v)
    assert painted[0]["color"] == "FF8FC8" and painted[0]["name"] == "Gigi" and "color" not in painted[1]
    shown = lines.local(painted, 0, 5)
    assert shown[0][3] == {"color": "FF8FC8", "name": "Gigi"} and shown[1][3] is None
    ass = lines.ass(shown, {"size": "normal", "position": "bottom"}, 1920, 1080)
    assert "{\\c&HC88FFF&}hi" in ass and "{\\c&HFFFFFF&}yo" in ass
    assert "Gigi" not in lines.srt(shown)                       # one named person: no names
    kyoko = shown + [(2, 3, "hey", {"color": "7FD3FF", "name": "Kyoko"})]
    assert "Gigi: hi" in lines.srt(kyoko) and "Kyoko: hey" in lines.srt(kyoko) and "\nyo\n" in lines.srt(kyoko)


def test_vtubers_list_is_the_colour_of_each_person(tmp_path):
    import pytest
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    conn.execute("PRAGMA foreign_keys = OFF")                # only the voices tables matter here
    assert speakers.get(conn, 1) == {} and speakers.vtubers(conn) == {}
    assert speakers.hex_color("#f8c") == "FF88CC" and speakers.hex_color("ff8fc8") == "FF8FC8"
    assert speakers.hex_color("pink") is None and speakers.hex_color("#12345") is None
    # Naming someone in a clip adds her to the list with her colour.
    speakers.put(conn, 1, {"1": {"name": "Kyoko", "color": "7FD3FF"}})
    assert {k: (v["name"], v["color"]) for k, v in speakers.vtubers(conn).items()} == {"kyoko": ("Kyoko", "7FD3FF")}
    # The list's colour is hers on every clip, even one that stored another.
    speakers.save_vtuber(conn, "kyoko", "#0f0")
    assert speakers.get(conn, 1)["1"]["color"] == "00FF00"
    speakers.save_vtuber(conn, "Hyakuto Kyoko", "00FF00", old="kyoko")      # renamed
    assert list(speakers.vtubers(conn)) == ["hyakuto kyoko"]
    with pytest.raises(speakers.SpeakerError):
        speakers.save_vtuber(conn, "Gigi", "pinkish")
    with pytest.raises(speakers.SpeakerError):
        speakers.save_vtuber(conn, " ", "FF8FC8")
    speakers.delete_vtuber(conn, "HYAKUTO kyoko")
    assert speakers.vtubers(conn) == {}
    # A line given person 0, who has no entry, still gets a colour.
    assert speakers.paint([{"t0": 0, "t1": 1, "text": "a", "speaker": 0}], {})[0]["color"] == speakers.default_color(0)


def test_import_a_csv_match_short_names_and_readable_colours(tmp_path):
    import pytest
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    csv = ("\ufeffname,japanese_name,status,main_hex,accent_hex,main_share_pct,profile_url\n"
           "Gigi Murin,ジジ・ムリン,Active,#F6AC3F,#99463B,38,https://x/gigi\n"
           "Kaela Kovalskia,カエラ・コヴァルスキア,Active,#580B0E,#6E4331,53,https://x/kaela\n"
           "Mococo Abyssgard,,Active,#E97CAA,,46,\n"
           "Fuwawa Abyssgard,,Active,#88ACEC,,81,\n"
           "Nobody,,Active,not-a-colour,,1,\n")
    added, updated, problems = speakers.import_csv(conn, csv)
    assert (added, updated) == (4, 0) and len(problems) == 1 and "line 6" in problems[0]
    m = speakers.vtubers(conn)
    assert m["kaela kovalskia"]["japanese_name"] == "カエラ・コヴァルスキア" and m["kaela kovalskia"]["accent"] == "6E4331"
    assert speakers.import_csv(conn, "name,main_hex\nGigi Murin,#F6AC3F\n")[:2] == (0, 1)
    assert m["gigi murin"]["status"] == "Active"                       # a short CSV keeps what it doesn't have
    with pytest.raises(speakers.SpeakerError):
        speakers.import_csv(conn, "who,colour_name\nGigi,orange\n")
    # Names: full, Japanese, or one word only she has.
    assert speakers.find(m, "gigi")["name"] == "Gigi Murin" and speakers.find(m, "ジジ・ムリン")["name"] == "Gigi Murin"
    assert speakers.find(m, "Abyssgard") is None and speakers.find(m, "Mococo")["name"] == "Mococo Abyssgard"
    # A dark colour is lightened for captions (same hue); a light one stays.
    assert speakers.readable("F6AC3F") == "F6AC3F"
    red = speakers.readable("580B0E")
    assert red != "580B0E" and speakers._luminance(red) >= speakers.TEXT_LUMINANCE and int(red[:2], 16) > int(red[2:4], 16)
    # A clip naming "Gigi" shows Gigi Murin in her colour, and doesn't recolour her on the list...
    speakers.put(conn, 1, {"0": {"name": "Gigi", "color": "FF8FC8"}, "1": {"name": "Kaela", "color": "7FD3FF"}})
    got = speakers.get(conn, 1)
    assert got["0"] == {"name": "Gigi Murin", "color": "F6AC3F", "text": "F6AC3F"} and got["1"]["text"] == red
    assert speakers.vtubers(conn)["gigi murin"]["color"] == "F6AC3F"
    # ...unless you changed her colour there.
    speakers.put(conn, 1, {"0": {"name": "Gigi Murin", "color": "FFAA00"}}, recolor=0)
    assert speakers.vtubers(conn)["gigi murin"]["color"] == "FFAA00"
    painted = speakers.paint([{"t0": 0, "t1": 1, "text": "a", "speaker": 1}], got)
    assert painted[0]["color"] == red and painted[0]["name"] == "Kaela Kovalskia"
