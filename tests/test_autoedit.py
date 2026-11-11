"""AI edit: pieces, the cut's hard rules, the camera track and word captions."""

from chatwatcher.edit import autoedit as ae
from chatwatcher.edit import pieces as pc
from chatwatcher.subs import lines


def words(*items):
    """items: (start, end, text) per word, one Whisper line."""
    return {"lines": [{"words": [[a, b, f" {t}", 0.9] for a, b, t in items]}]}


CLIP = words((2.0, 2.4, "So"), (2.4, 3.0, "this"), (3.0, 3.5, "is"), (3.5, 4.0, "evil."),
             (9.0, 9.5, "No"), (9.5, 10.0, "way."), (10.2, 10.6, "I"), (10.6, 11.0, "died."))


def test_pieces_tile_the_clip():
    ps = pc.build(CLIP, 20.0, hits=[(7.0, 15.0)], chat=[9.5] * 10 + [1.0])
    assert [p.kind for p in ps] == ["quiet", "talk", "quiet", "talk", "quiet"]
    assert ps[0].t0 == 0 and ps[-1].t1 == 20.0
    assert all(a.t1 == b.t0 for a, b in zip(ps, ps[1:]))            # no holes
    assert ps[1].text == "So this is evil." and ps[3].text == "No way. I died."   # 0.2 s gap: same piece
    assert ps[2].hits == [(7.0, 15.0)] and "loud hit" in ps[2].line()
    assert ps[3].chat > 1.5 and "chat ×" in ps[3].line()


def test_parse_keep_and_ranges():
    k = ae.parse_keep(["p2-p4", "p5:0.6", "p7!", "junk", "p99"], 8)
    assert k == {1: ("all", None), 2: ("all", None), 3: ("all", None), 4: ("beat", 0.6), 6: ("action", None)}
    assert ae.parse_range("p3-p4", 8) == [2, 3] and ae.parse_range("", 8) == []


def test_assemble_rules():
    ps = pc.build(CLIP, 20.0)
    # Claude keeps the slow quiet start and the long quiet end: both go.
    plan = ae.assemble(ps, ae.parse_keep(["p1-p5"], len(ps)), [], 20.0)
    segs = plan.segments
    assert segs[0][0] >= 2.0 - ae.PAD_IN - 0.01                     # talk within half a second
    assert segs[-1][1] <= 11.0 + ae.TAIL_MAX + 0.01                 # ends soon after the last line
    # The 5 s quiet between the lines is trimmed to a short beat.
    assert sum(e - s for s, e, _ in segs) < 4.2 + 2.0 + ae.QUIET_MAX + ae.TAIL_MAX
    # "!" keeps all of it; a teaser goes first.
    plan = ae.assemble(ps, ae.parse_keep(["p2", "p3!", "p4"], len(ps)), ae.parse_range("p4", len(ps)), 20.0)
    assert plan.cold_open and plan.segments[0][0] > 8.5 and len(plan.segments) == 2
    assert plan.segments[1][0] < 2.0 < 4.0 < 9.0 < plan.segments[1][1]


def test_no_micro_cuts():
    ps = pc.build(words((1.0, 2.0, "a"), (2.65, 3.5, "b")), 5.0)    # quiet, a, 0.65 s quiet, b, quiet
    plan = ae.assemble(ps, ae.parse_keep(["p2", "p4"], len(ps)), [], 5.0)
    assert len(plan.segments) == 1                                  # a 0.3 s jump isn't worth a cut


def test_camera_never_idle():
    ps = pc.build(CLIP, 20.0)
    segs = [[1.9, 4.2, 0.0], [8.9, 12.0, 0.0]]
    boxes = {"face": {"x": 0.6, "y": 0.5, "w": 0.3, "h": 0.4}}
    cam = ae.camera_track(["p2 | face | close | cut", "p4 | face | tight | cut", "p3 | nowhere | wide"],
                          ps, segs, boxes, short=False)
    assert all(fx["by"] == "claude" and fx["type"] in ("punch", "push") for fx in cam)
    assert all(fx["d"] <= ae.IDLE_MAX for fx in cam if fx["type"] == "punch")
    assert {fx["region"] for fx in cam} == {"face"}
    zooms = sorted({fx["zoom"] for fx in cam})
    assert len(zooms) >= 2                                          # long holds re-frame
    # A long wide stretch gets one slow push, not a snap every few seconds.
    wide = ae.camera_track(["p1-p5 | face | wide"], ps, [[0.0, 10.0, 0.0]], boxes, short=False)
    assert len(wide) == 1 and wide[0]["type"] == "push" and wide[0]["d"] == 10.0


def test_effects_inside_the_cut():
    segs = [[1.0, 5.0, 0.0]]
    sfx = ae.sound_effects(["2.0 | bonk | -3 | -10 | dumb line", "9 | bonk | 0 | 0 | outside", "3 | nope | 0 | 0 | x"],
                           segs, [(2.3, 12.0)], {"bonk"}, 5)
    assert len(sfx) == 1 and sfx[0]["t"] == 2.3 and sfx[0]["duck"] == -10 and sfx[0]["why"] == "dumb line"
    fx = ae.hit_effects(["2 | shake | heavy | scream", "2 | flash | red | fail", "2 | wobble | x"], segs, [], {}, 6.0, 5)
    assert [f["type"] for f in fx] == ["shake", "flash"] and fx[0]["strength"] == 3 and fx[1]["color"] == "red"


def test_word_captions_with_emphasis():
    cues = ae.captions(CLIP, ["evil", "no way"])
    assert all(lines.width(c["text"]) <= lines.WORD_CUE + 4 for c in cues)
    text = " ".join(c["text"] for c in cues)
    assert "*evil*" in text and "*No*" in text or "*No way*" in text
    assert all(c["by"] == "auto" for c in cues)       # built from the words, so a new transcript updates them
    assert lines.emphasis_terms(["*evil*", "Evil", " no  way ", "", 3]) == ["evil", "no way", "3"]
    style = lines.clean_style({"mode": "words", "emphasis": ["evil"] * 2 + [f"w{i}" for i in range(20)]},
                              {"on": True, "size": "normal", "position": "top"})
    assert style["emphasis"][:2] == ["evil", "w0"] and len(style["emphasis"]) == lines.MAX_EMPHASIS
    assert lines.emphasise([{"text": "so evil, evilness"}], ["evil"])[0]["text"] == "so *evil*, evilness"
    said = {"lines": [{"words": [[35.24, 36.5, " Whaaat?", 0.9], [36.66, 36.78, " It", 0.9],
                                 [36.82, 37.14, " literally", 0.9]]}]}
    assert [c["text"] for c in lines.build(said, lines.WORD_CUE)] == ["Whaaat?", "It literally"]
    assert [c["text"] for c in lines.build(said)] == ["Whaaat? It literally"]     # lines: "Whaaat?" waits
    assert lines.clean_style({"mode": "words"}, {"on": True, "size": "normal", "position": "top"})["mode"] == "words"
    assert lines.clean_style({"mode": "bad"}, {"on": True, "size": "normal", "position": "top"})["mode"] == "lines"


def test_schema_is_flat():
    s = ae.schema(3, 2)
    assert all(v.get("type") != "object" for v in s["properties"].values())
    assert all(v.get("items", {}).get("type", "string") == "string" for v in s["properties"].values()
               if v.get("type") == "array")


def test_volume_per_piece():
    ps = pc.build(CLIP, 20.0)                         # p2 = "So this is evil." 2.0-4.0, p4 = "No way. I died." 9.0-11.0
    segs = [[1.9, 12.0, 0.0]]
    out, n = ae.apply_volume(["p4 | -12 | screech", "p2 | +40 | too much", "p3 | 0.5 | tiny", "bad"], ps, segs)
    assert n == 2
    # Split where p4 starts and ends; the parts still play back to back.
    assert all(a[1] == b[0] for a, b in zip(out, out[1:])) and out[0][0] == 1.9 and out[-1][1] == 12.0
    at = lambda t: next(g for s, e, g in out if s <= t < e)          # noqa: E731
    assert at(10.0) == -12.0 and at(3.0) == ae.VOLUME_RANGE[1] and at(6.0) == 0.0
    # A change that only grazes a segment's edge doesn't make a sliver.
    out, _ = ae.apply_volume(["p2 | -10 | x"], ps, [[3.9, 8.0, 0.0]])
    assert out == [[3.9, 8.0, 0.0]]
    assert ae.apply_volume([], ps, segs) == (segs, 0)


def _heard(T: int = 400):
    """Letter scores like the alignment model's: blank everywhere, letters where said."""
    import numpy as np
    from chatwatcher.subs import align
    em = np.full((T, 31), -9.0)
    em[:, align.BLANK] = -0.01

    def say(word: str, t0: float, t1: float) -> None:
        ids = align.letters(word)
        f0, f1 = int(t0 / align.FRAME_S), int(t1 / align.FRAME_S)
        step = (f1 - f0) / len(ids)
        for n, i in enumerate(ids):
            a, b = f0 + int(n * step), f0 + int((n + 1) * step)
            em[a:b, align.BLANK], em[a:b, i] = -9.0, -0.01
    return em, say


def test_align_finds_where_words_are_said():
    from chatwatcher.subs import align
    assert align.letters(" Whaaat?") == [align._INDEX[c] for c in "whaaat"]
    assert align.letters("Don’t") == align.letters("don't") and align.letters(" café") == align.letters("cafe")
    assert align.letters(" 123") == [] and align.letters("こんにちは") == [] and align.letters("'") == []
    em, say = _heard()
    say("so", 1.0, 1.3)
    say("ok", 3.0, 3.3)
    say("ok", 5.0, 5.3)                       # the same word again, louder, two seconds later
    words = [[0.6, 2.4, " so", 0.9],          # Whisper stretched it over the pause
             [2.4, 2.6, " 12", 0.9],          # no letters: stays where Whisper heard it
             [2.8, 3.6, " ok", 0.9]]          # held near Whisper's time, not the later "ok"
    em = align.with_star(em)
    out = align.retime(words, em, 8.0)
    assert abs(out[0][0] - 1.0) <= 0.04 and abs(out[0][1] - 1.3) <= 0.08
    assert abs(out[2][0] - 3.0) <= 0.04 and abs(out[2][1] - 3.3) <= 0.08
    assert out[1][:2] == [2.4, 2.6] and out[0][2:] == [" so", 0.9]
    assert all(a[1] <= b[0] for a, b in zip(out, out[1:]))
    # Nothing alignable (Japanese): Whisper's times, pauses and all.
    ja = [[0.5, 0.9, "こんにちは", 0.9], [2.0, 2.4, "ありがとう", 0.9]]
    assert align.retime(ja, em, 8.0) == ja
    # words.json: line edges follow the words; speech is VAD's stretches plus the words.
    data = align.align({"duration_s": 8.0, "lines": [{"start": 0.6, "end": 3.6, "words": words}]}, em, [(6.0, 6.5)])
    line = data["lines"][0]
    assert data["aligned"] == align.VERSION and line["start"] == out[0][0] and line["end"] == out[2][1]
    assert data["speech"][-1] == [6.0, 6.5] and data["speech"][0][0] == out[0][0]
    # A laugh with no words shows on the quiet piece.
    ps = pc.build(data | {"speech": [[1.0, 1.3], [4.0, 5.0]]}, 8.0)
    assert any(p.kind == "quiet" and p.voice == 1.0 and "voice without words" in p.line() for p in ps)


def test_frame_panels_and_split():
    from chatwatcher.edit import layout as lay
    from chatwatcher.edit.service import clean_effects, split_at
    narrow = lay.frame_panels({"x": 0.04, "y": 0.4, "w": 0.15, "h": 0.5}, 1920, 1080)
    assert len(narrow) == 1 and narrow[0].dst == (0.0, 0.0, 1.0, 1.0)
    x, y, w, h = narrow[0].src
    assert x <= 0.04 and x + w >= 0.19 and abs((w * 1920) / (h * 1080) - lay.OUT_W / lay.OUT_H) < 0.01
    wide = lay.frame_panels({"x": 0.0, "y": 0.0, "w": 0.7, "h": 0.6}, 1920, 1080)
    assert len(wide) == 2 and wide[0].blur and 0 < wide[1].dst[3] <= 0.6
    frames = [{"t": 2.0, "d": 3.0, "region": "a"}, {"t": 4.0, "d": 1.0, "region": "b"}]
    assert lay.frame_at(4.5, frames)["region"] == "b" and lay.frame_at(1.0, frames) is None
    parts = split_at([[0.0, 10.0, -3.0]], [2.0, 5.0, 0.1, 9.95], 30)
    assert parts == [[0.0, 2.0, -3.0], [2.0, 5.0, -3.0], [5.0, 10.0, -3.0]]
    fx = clean_effects([{"type": "frame", "t": 1, "d": 99, "region": "Limiter"}], 10, set())
    assert fx[0]["type"] == "frame" and fx[0]["d"] == lay.FRAME_LIMITS[2] and fx[0]["region"] == "limiter"


def test_short_shot_on_hidden_region_moves_the_picture():
    ps = pc.build(CLIP, 20.0)
    boxes = {"face": {"x": 0.6, "y": 0.5, "w": 0.3, "h": 0.4}, "limiter": {"x": 0.04, "y": 0.4, "w": 0.15, "h": 0.5}}
    cam = ae.camera_track(["p2 | limiter | close | cut", "p4 | face | close | cut"], ps,
                          [[1.9, 4.2, 0.0], [8.9, 12.0, 0.0]], boxes, short=True, visible={"face"})
    frames = [f for f in cam if f["type"] == "frame"]
    assert len(frames) == 1 and frames[0]["region"] == "limiter" and frames[0]["t"] == 1.9
    # The long edit has no layout: no frame shots.
    long = ae.camera_track(["p2 | limiter | close | cut"], ps, [[1.9, 4.2, 0.0]], boxes, short=False)
    assert not [f for f in long if f["type"] == "frame"]


def test_pieces_are_whole_sentences():
    data = words((1.0, 1.4, "It"), (1.4, 1.8, "was"), (1.8, 2.2, "loud,"), (3.0, 3.4, "right?"),   # 0.8 s pause mid-sentence
                 (3.8, 4.2, "Okay."), (6.5, 7.0, "Next"), (7.0, 7.4, "one"))
    ps = [p for p in pc.build(data, 9.0) if p.kind == "talk"]
    assert [p.text for p in ps] == ["It was loud, right?", "Okay.", "Next one"]
    assert ps[0].pauses == [(2.2, 3.0)]


def test_long_pause_inside_a_sentence_shrinks_and_lines_keep_a_breath():
    data = words((1.0, 1.5, "So"), (1.5, 2.0, "this"), (3.4, 4.0, "happened."),   # 1.4 s pause inside
                 (5.0, 5.6, "Wow."))
    ps = pc.build(data, 8.0)
    talk = [i for i, p in enumerate(ps) if p.kind == "talk"]
    plan = ae.assemble(ps, {i: ("all", None) for i in talk}, [], 8.0)
    segs = plan.segments
    assert len(segs) == 2                                    # one cut: the long pause; the 1 s gap keeps a breath
    gap = segs[1][0] - segs[0][1]
    assert 0.9 <= gap <= 1.2                                 # 1.4 s pause → ~KEEP_PAUSE left
    assert segs[1][1] >= 5.6                                 # "Wow." follows after a breath, not cut off


def test_transitions_render_pieces_and_ai_placement():
    from chatwatcher.edit import motion
    from chatwatcher.edit.service import clean_effects
    segs = [[0.0, 5.0, 0.0], [10.0, 15.0, 0.0], [15.0, 20.0, 0.0]]       # a real cut, then a split that plays on
    fx = [{"type": "transition", "t": 10.0, "style": "whip"}, {"type": "transition", "t": 15.0, "style": "fade"}]
    parts = motion.transitions(segs, fx)
    assert {(k, v.type) for k, v in parts} == {(0, "push"), (0, "blur"), (1, "pull"), (1, "blur")}   # none on the split
    out = [v for k, v in parts if k == 0 and v.type == "push"][0]
    assert abs(out.t + out.d - 5.0) < 1e-9                                 # the out half ends on the cut
    # pull starts zoomed and eases back; a fade-out rises
    pull = motion.Visual("pull", 0.0, 1.0, zoom=2.0)
    assert motion.camera(0.0, [pull])[2] < 0.3 and abs(motion.camera(0.999, [pull])[2] - 0.5) < 0.01
    assert motion.flashes(0.9, [motion.Visual("fadeout", 0.0, 1.0, color="black")])[0][1] > 0.7
    assert "boxblur" in motion.video_filter([motion.Visual("blur", 0.1, 0.2)], 30, 1.0)
    clean = clean_effects([{"type": "transition", "t": 3, "style": "zoom", "d": 9}], 10, set())
    assert clean[0]["style"] == "zoom" and clean[0]["d"] == motion.TRANSITION_D[1]
    ps = pc.build(CLIP, 20.0)                                              # p2 2-4, p4 9-11
    tr = ae.transition_effects(["p4 | whip | topic jump", "p4 | flash | dup", "p2 | spin | no"], ps,
                               [[2.5, 3.0, 0.0], [1.9, 4.2, 0.0], [8.9, 12.0, 0.0]], cold_open=True, limit=5)
    assert [(round(f["t"], 1), f["style"]) for f in tr] == [(1.9, "flash"), (8.9, "whip")]   # teaser → story gets a flash
