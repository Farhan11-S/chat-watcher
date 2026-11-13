"""Directing the AI edit: one Claude session per clip edit, resumed for each message."""

import json

from chatwatcher.analyze.claude_runner import ClaudeError, ClaudeResult, ClaudeRunner
from chatwatcher.config import ClaudeConfig
from chatwatcher.edit import direct
from chatwatcher.edit.service import clip_ref

from test_web import mid, site  # noqa: F401

WORDS = {"language": "en", "lines": [
    {"start": 2.0, "end": 4.0, "text": " So this is evil.",
     "words": [[2.0, 2.4, " So", 0.9], [2.4, 3.0, " this", 0.9], [3.0, 3.5, " is", 0.9], [3.5, 4.0, " evil.", 0.9]]},
    {"start": 9.0, "end": 11.0, "text": " No way. I died.",
     "words": [[9.0, 9.5, " No", 0.9], [9.5, 10.0, " way.", 0.9], [10.2, 10.6, " I", 0.9], [10.6, 11.0, " died.", 0.9]]}]}


def answer(keep, note="Cut the dead air.", apply=True, cold_open="", overlays=(), camera=()):
    return {"apply": apply, "note": note, "hook_title": "", "cold_open": cold_open, "keep": keep,
            "camera": list(camera), "sounds": [], "hits": [], "volume": [], "transitions": [], "emphasis": [],
            "overlays": list(overlays)}


class FakeDirector:
    """Stands in for the claude CLI: answers in turn, and remembers how it was called."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def run(self, prompt, schema, *, system, cwd, allow_read=False, model=None, effort=None, images=None,
            session=None, resume=False, tools=None):
        self.calls.append({"prompt": prompt, "schema": schema, "images": images, "session": session,
                           "resume": resume, "cwd": cwd, "tools": tools})
        step = self.script.pop(0)
        if isinstance(step, ClaudeError):
            raise step
        return ClaudeResult(step, "claude-test", 1, {"usage": {"output_tokens": 5}})


def _segments(conn, m):
    return json.loads(conn.execute("SELECT segments_json FROM edits WHERE moment_id = ?", (m,)).fetchone()[0])


def test_directing_the_ai_edit(site):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    (clip_ref(conn, m).folder / "words.json").write_text(json.dumps(WORDS))
    assert client.get(f"/api/moment/{m}/direct?kind=long").json() == {"ok": True, "session": False, "messages": []}

    # ✨ AI edit starts a session: the whole material, with the frames.
    fake = FakeDirector(answer(["p2-p4"]), answer(["p4", "p2"], note="Opened on the death."),
                        answer(["p2"], apply=False, note="Because p3 is only silence."))
    deps.claude = fake
    d = client.post(f"/moment/{m}/autoedit", json={"segments": [[0, 20, 0]], "kind": "long"}).json()
    assert d["ok"] and d["applied"] and d["fresh"] and [x["role"] for x in d["messages"]] == ["user", "assistant"]
    first = fake.calls[0]
    assert first["session"] and not first["resume"] and first["images"] and "<material>" in first["prompt"]
    assert first["schema"]["properties"]["apply"]["type"] == "boolean"
    assert d["messages"][1]["applied"] and "Cut the dead air." in d["reply"]

    # A message resumes it: no material again, but the edit as it is now (changed by hand).
    hand = [[2.0, 4.2, 0], [8.9, 11.2, -6]]
    d = client.post(f"/moment/{m}/direct", json={"segments": hand, "kind": "long",
                                                 "message": "Open on the death"}).json()
    call = fake.calls[1]
    assert call["session"] == first["session"] and call["resume"] and not call["images"]
    assert "Open on the death" in call["prompt"] and "<material>" not in call["prompt"]
    assert "- 2.0–4.2 s: p2" in call["prompt"] and "at -6 dB" in call["prompt"]
    assert d["applied"] and not d["fresh"] and len(d["messages"]) == 4
    assert _segments(conn, m)[0][0] > 8                                 # the death plays first now

    # A question: Claude answers and the edit stays.
    before = _segments(conn, m)
    d = client.post(f"/moment/{m}/direct", json={"kind": "long", "message": "Why did you drop p3?"}).json()
    assert not d["applied"] and d["reply"] == "Because p3 is only silence." and _segments(conn, m) == before
    assert d["messages"][-1] == d["messages"][-1] | {"role": "assistant", "applied": False}

    # A lost session starts again with the full material and what was said.
    fake.script = [ClaudeError("No conversation found with session ID: x", kind="no_session"), answer(["p2-p4"])]
    d = client.post(f"/moment/{m}/direct", json={"kind": "long", "message": "Shorter please"}).json()
    lost, again = fake.calls[3], fake.calls[4]
    assert lost["resume"] and again["session"] != first["session"] and not again["resume"]
    assert "<material>" in again["prompt"] and "Earlier in this conversation" in again["prompt"]
    assert "Open on the death" in again["prompt"] and "Shorter please" in again["prompt"]
    assert d["fresh"] and "couldn't be resumed" in d["reply"] and len(d["messages"]) == 2

    # New session on purpose; the Short has its own conversation.
    fake.script = [answer(["p2"])]
    d = client.post(f"/moment/{m}/direct", json={"kind": "long", "message": "Only the setup", "new": True}).json()
    assert d["fresh"] and "couldn't be resumed" not in d["reply"] and fake.calls[5]["session"] != again["session"]
    assert client.get(f"/api/moment/{m}/direct?kind=short").json()["messages"] == []
    assert client.post(f"/moment/{m}/direct", json={"kind": "long", "message": "  "}).status_code == 400


def test_session_flags():
    r = ClaudeRunner.__new__(ClaudeRunner)
    r.exe, r.cfg = "claude", ClaudeConfig()
    plain = r.command({}, system="s")
    assert "--no-session-persistence" in plain and "--resume" not in plain
    start = r.command({}, system="s", session="abc")
    assert start[start.index("--session-id") + 1] == "abc" and "--no-session-persistence" not in start
    go_on = r.command({}, system="s", session="abc", resume=True)
    assert go_on[go_on.index("--resume") + 1] == "abc" and "--session-id" not in go_on
    assert "--restricted" in go_on and go_on[go_on.index("--tools") + 1] == ""
    from chatwatcher.analyze.claude_runner import classify
    assert classify("No conversation found with session ID: 1") == "no_session"


def test_describe_and_ids():
    assert direct._ids([2, 3, 4, 7, 9, 10]) == "p2-p4, p7, p9-p10" and direct._ids([]) == ""


def test_directing_keeps_your_subtitles_and_can_use_overlays(site, tmp_path):  # noqa: F811
    from test_overlays import green_clip
    from chatwatcher.edit import overlays as ovl
    cfg, conn, client, deps = site
    m = mid(conn)
    (clip_ref(conn, m).folder / "words.json").write_text(json.dumps(WORDS))
    ovl.import_overlay(cfg, conn, green_clip(tmp_path / "p.mp4"), original="p.mp4", name="pings",
                       description="someone says something dumb")
    fake = FakeDirector(answer(["p2-p4"]),
                        answer(["p2-p4"], overlays=["9.6 | pings | middle | the bad call", "9.9 | nope | full | gone"],
                               camera=["p4 | full | wide | cut"]))
    deps.claude = fake
    client.post(f"/moment/{m}/autoedit", json={"segments": [[0, 20, 0]], "kind": "short"})
    assert "pings" in fake.calls[0]["prompt"] and "someone says something dumb" in fake.calls[0]["prompt"]
    assert "- full: the whole picture" in fake.calls[0]["prompt"]

    # You retype a line and say who says it: directing again keeps both (and so their colour).
    d = client.get(f"/api/moment/{m}/edit?kind=short").json()
    mine = [dict(c, text="So THIS is evil.", by="you", speaker=1) if k == 0 else c for k, c in enumerate(d["subs"])]
    client.post(f"/moment/{m}/edit", json={"kind": "short", "segments": d["segments"], "subs": mine})
    style = client.get(f"/api/moment/{m}/edit?kind=short").json()["subs_style"] | {"size": "small"}
    client.post(f"/moment/{m}/edit", json={"kind": "short", "segments": d["segments"], "subs_style": style})
    d = client.post(f"/moment/{m}/direct", json={"kind": "short", "message": "Add question pings on the death"}).json()
    assert "Overlay library now" in fake.calls[1]["prompt"] and "- pings: pings" in fake.calls[1]["prompt"]
    assert d["applied"] and "1 overlay" in d["reply"]
    after = client.get(f"/api/moment/{m}/edit?kind=short").json()
    assert after["subs"][0]["text"] == "So THIS is evil." and after["subs"][0]["speaker"] == 1
    assert after["subs_source"] == "saved" and after["subs_style"]["size"] == "small"
    ov = [f for f in after["effects"] if f["type"] == "overlay"]
    assert len(ov) == 1 and ov[0]["overlay"] == "pings" and ov[0]["at"] == "middle" and ov[0]["by"] == "claude"
    assert any(f["type"] == "frame" and f["region"] == "full" for f in after["effects"])     # the whole picture


def test_follow_up_tells_of_new_sounds(site):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    (clip_ref(conn, m).folder / "words.json").write_text(json.dumps(WORDS))
    conn.execute("INSERT INTO sounds (id, name, description, file, duration_s, added_at) "
                 "VALUES ('old_one', 'old', 'was there', 'old.flac', 1, '2000-01-01 00:00:00')")
    fake = FakeDirector(answer(["p2-p4"]), answer(["p2-p4"], apply=False))
    deps.claude = fake
    assert client.post(f"/moment/{m}/autoedit", json={"segments": [[0, 20, 0]], "kind": "long"}).json()["ok"]
    conn.execute("UPDATE edit_chats SET created_at = '2001-01-01 00:00:00'")
    conn.execute("INSERT INTO sounds (id, name, description, file, duration_s) VALUES ('faaah', 'FAAAH', 'cringe', 'f.flac', 1.9)")
    assert client.post(f"/moment/{m}/direct", json={"kind": "long", "message": "use faah"}).json()["ok"]
    follow = fake.calls[1]["prompt"]
    assert "since this conversation started" in follow and "- faaah: FAAAH" in follow and "old_one" not in follow
