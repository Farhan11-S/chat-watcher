import json
import shutil
import subprocess
from pathlib import Path

import pytest

from chatwatcher.analyze.claude_runner import ClaudeResult
from chatwatcher.subs.polish import apply

from test_subs import words
from test_web import mid, site  # noqa: F401  (fixture)

EDITOR_JS = Path(__file__).resolve().parents[1] / "src/chatwatcher/web/static/editor.js"


def cue(i, t0, t1, text, by="auto"):
    return {"id": f"a{i}", "t0": t0, "t1": t1, "text": text, "by": by}


def test_apply_keeps_timing_and_your_lines():
    cues = [cue(1, 0, 1, "good thing i put a limiter in there"), cue(2, 1.1, 2, "um okay"),
            cue(3, 2.1, 3, "are you"), cue(4, 3.05, 4, "deaf"), cue(5, 4.1, 5, "Sicilia said so", "you"),
            cue(6, 9, 10, "outside the cut")]
    out = [{"id": "a1", "text": "Good thing I put a *limiter* in there.", "join_next": False},
           {"id": "a2", "text": "", "join_next": False},
           {"id": "a3", "text": "Are you deaf?", "join_next": True}, {"id": "a4", "text": "ignored", "join_next": False},
           {"id": "a5", "text": "Claude must not touch this", "join_next": False},
           {"id": "zz", "text": "unknown", "join_next": False}]
    new, changed, joined, dropped = apply(cues, out, {"a1", "a2", "a3", "a4", "a5"})
    assert [(c["id"], c["t0"], c["t1"], c["text"], c["by"]) for c in new] == [
        ("a1", 0, 1, "Good thing I put a *limiter* in there.", "claude"),
        ("a3", 2.1, 4, "Are you deaf?", "claude"),
        ("a5", 4.1, 5, "Sicilia said so", "you"),
        ("a6", 9, 10, "outside the cut", "auto")]
    assert (changed, joined, dropped) == (2, 1, 1)


class FakeClaude:
    def __init__(self, lines, names=()):
        self.lines, self.names, self.calls = lines, list(names), []

    def run(self, prompt, schema, *, system, cwd, allow_read=False, model=None, effort=None):
        self.calls.append({"prompt": prompt, "schema": schema, "system": system, "model": model, "effort": effort})
        return ClaudeResult({"lines": self.lines, "names": self.names, "note": "Fixed the names."}, "claude-test", 1, {})


def test_claude_cleans_up_the_lines_in_the_cut(site):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    folder = Path(conn.execute("SELECT folder FROM clips WHERE moment_id = ?", (m,)).fetchone()[0])
    (folder / "words.json").write_text(json.dumps(words(("good thing i put a limiter in there", 2.0, 0.25),
                                                         ("are you deaf sicilia", 6.0, 0.3),
                                                         ("not in the cut", 15.0, 0.3))), encoding="utf-8")
    d = client.get(f"/api/moment/{m}/edit").json()
    assert d["names"] == [] and d["output_language"] == "English"
    ids = [c["id"] for c in d["subs"]]
    client.post(f"/moment/{m}/names", json={"terms": "holo1GP"})
    mine = d["subs"][:1] + [d["subs"][1] | {"text": "Are you deaf, Cecilia?", "by": "you"}] + d["subs"][2:]
    deps.claude = FakeClaude([{"id": ids[0], "text": "Good thing I put a *limiter* in there.", "join_next": False},
                              {"id": ids[1], "text": "Claude would have changed this", "join_next": False}],
                             names=["Cecilia Immergreen", "holo1GP", "FL Studio"])
    r = client.post(f"/moment/{m}/subs/polish", json={"segments": [[1, 10, 0]], "subs": mine}).json()
    assert r["ok"] and r["changed"] == 1 and r["note"] == "Fixed the names."
    assert [c["text"] for c in r["subs"]] == ["Good thing I put a *limiter* in there.", "Are you deaf, Cecilia?", "not in the cut"]
    assert [c["by"] for c in r["subs"]] == ["claude", "you", "auto"]
    assert r["names"] == ["holo1GP", "Cecilia Immergreen", "FL Studio"] and r["names_added"] == ["Cecilia Immergreen", "FL Studio"]
    call = deps.claude.calls[0]
    assert (call["model"], call["effort"]) == ("opus", "high")            # claude.edit_model / edit_effort
    assert call["schema"]["properties"]["lines"]["items"]["properties"]["id"]["enum"] == ids[:2]   # only the cut
    assert "(fixed) Are you deaf, Cecilia?" in call["prompt"] and "Names list for this channel: holo1GP" in call["prompt"]
    assert "Speech recognition heard mostly: English" in call["prompt"] and "audience that reads English" in call["system"]
    e = client.get(f"/api/moment/{m}/edit").json()
    assert e["subs_source"] == "saved" and e["subs"][0]["text"].startswith("Good thing") and len(e["names"]) == 3
    assert client.get(f"/api/moment/{other_moment(conn, m)}/edit").json()["names"] == e["names"]   # the whole channel


def other_moment(conn, m):
    return conn.execute("SELECT m.id FROM moments m JOIN clips c ON c.moment_id = m.id WHERE m.id != ? LIMIT 1",
                        (m,)).fetchone()[0]


def test_polish_needs_lines_and_names_route(site):  # noqa: F811
    cfg, conn, client, deps = site
    m = mid(conn)
    deps.claude = FakeClaude([])
    r = client.post(f"/moment/{m}/subs/polish", json={"segments": [[1, 10, 0]]})
    assert r.status_code == 400 and "Transcribe" in r.json()["error"] and not deps.claude.calls
    assert client.post(f"/moment/{m}/names", json={"terms": "Cecilia, cecilia\nGigi Murin"}).json()["names"] == ["Cecilia", "Gigi Murin"]
    assert client.post(f"/moment/{m}/names", json={"add": ["gigi murin", "CeCe"]}).json() == {
        "ok": True, "names": ["Cecilia", "Gigi Murin", "CeCe"], "added": ["CeCe"]}


def test_retyped_names_are_spotted():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    js = EDITOR_JS.read_text(encoding="utf-8")
    start, end = js.index("  const NOT_NAMES = "), js.index("  async function addNames(")
    cases = [["are you deaf sicilia", "Are you deaf, Cecilia?"], ["the holo one gp", "The holo1GP is on"],
             ["okay i'm sorry", "Okay. I'm sorry, CeCe!"], ["Good thing I put", "Good thing I put"],
             ["she said hi", "She said hi to Gigi Murin."]]
    script = f"{js[start:end]}\nconsole.log(JSON.stringify({json.dumps(cases)}.map(([a, b]) => newNames(a, b))));"
    got = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout)
    assert got == [["Cecilia"], ["holo1GP"], ["CeCe"], [], ["Gigi", "Murin"]]
