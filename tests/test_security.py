"""Security audit (24 Sep): the fixes stay fixed.

- zoek_in_code: a search term can never become a grep option (it leaked .env).
- verbeter_jezelf: nothing reaches the Mac worker's queue without Wouter's tap in the app.
- zet_uit: only after a finger on the screen; the phone cannot run it as a tool.
- Face.confirm: hold = yes, a tap = no, nothing = no; a touch that was already there counts for nothing.
- memory on the robot: every read and write goes to the server, nothing to the SD card.
"""
import json
import threading
from datetime import datetime

import pytest

from willie.brain import memory
from willie.face.framebuffer import Framebuffer
from willie.face.renderer import Renderer, View
from willie.face.runtime import Face
from willie.voice import tools


# ---------------------------------------------------------------- zoek_in_code
@pytest.mark.parametrize("term", ["--include=*", "-r --include=*", "--include=.env", "-f.env"])
def test_search_term_is_never_a_grep_option(tmp_path, monkeypatch, term):
    (tmp_path / ".env").write_text("GEMINI_API_KEY=FAKE_SECRET_123\n")
    (tmp_path / "a.py").write_text("x = 1\n")
    monkeypatch.setattr(tools, "REPO", tmp_path)
    result = json.dumps(tools.zoek_in_code(term))
    assert "FAKE_SECRET" not in result


def test_search_still_finds_code(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("def wake_word():\n")
    monkeypatch.setattr(tools, "REPO", tmp_path)
    assert tools.zoek_in_code("wake_word")["aantal"] == 1


def test_read_code_stays_inside_the_repo(tmp_path, monkeypatch):
    repo = tmp_path / "willie"
    repo.mkdir()
    (tmp_path / "willie-old").mkdir()
    (tmp_path / "willie-old" / "x.py").write_text("secret\n")
    monkeypatch.setattr(tools, "REPO", repo)
    assert "fout" in tools.lees_code("../willie-old/x.py")
    assert "fout" in tools.lees_code(".env")


# ---------------------------------------------------------------- verbeter_jezelf
@pytest.fixture
def improve_files(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "REQUEST_FILE", tmp_path / "requests.jsonl")
    monkeypatch.setattr(tools, "QUEUE_FILE", tmp_path / "queue.jsonl")
    monkeypatch.setattr(tools, "RESULT_FILE", tmp_path / "results.jsonl")
    shown = []
    monkeypatch.setattr(tools, "APPROVAL_HOOK", lambda entry: shown.append(entry) or True)
    return tmp_path, shown


def test_improvement_waits_for_the_app(improve_files):
    tmp, shown = improve_files
    result = tools.verbeter_jezelf("maak de wake word gevoeliger in config", "gevoeliger", "laag", bevestigd=True)
    assert result["ok"] and "app" in result["wacht_op"]
    assert not (tmp / "queue.jsonl").exists()            # the model's "yes" queues nothing
    assert len(shown) == 1 and len(tools.pending_requests()) == 1


def test_approved_request_joins_the_queue(improve_files):
    tmp, shown = improve_files
    tools.verbeter_jezelf("maak de wake word gevoeliger in config", "gevoeliger", "laag")
    entry = shown[0]
    assert tools.decide(entry["id"], True, entry["hash"]) == "goedgekeurd"
    queued = [json.loads(l) for l in (tmp / "queue.jsonl").read_text().splitlines()]
    assert queued[0]["opdracht"] == entry["opdracht"] and tools.pending_requests() == []


def test_changed_text_or_rejection_never_queues(improve_files):
    tmp, shown = improve_files
    tools.verbeter_jezelf("maak de wake word gevoeliger in config", "gevoeliger", "laag")
    tools.verbeter_jezelf("verander de systemd unit van de voice", "unit", "hoog")
    first, second = shown
    assert tools.decide(first["id"], True, "0" * 64) == "afgewezen"      # hash of other text
    assert tools.decide(second["id"], False, second["hash"]) == "afgewezen"
    assert not (tmp / "queue.jsonl").exists()
    assert "niet goedgekeurd" in (tmp / "results.jsonl").read_text()


def test_old_requests_expire(improve_files):
    tmp, _ = improve_files
    old = {"id": "abc", "gevraagd": "2020-01-01T00:00:00", "opdracht": "x" * 20, "plan": "", "risico": "laag"}
    (tmp / "requests.jsonl").write_text(json.dumps(old) + "\n")
    assert tools.pending_requests() == []


def test_worker_parser_computes_the_same_fingerprint(improve_files):
    import subprocess
    import sys
    _, shown = improve_files
    tools.verbeter_jezelf("maak de wake word gevoeliger in config", "gevoeliger", "hoog")
    entry = shown[0]
    out = subprocess.run([sys.executable, str(tools.REPO / "tools" / "improve_parse.py")],
                         input=json.dumps(entry), capture_output=True, text=True).stdout.splitlines()
    assert out[3] == entry["id"] and out[4] == entry["hash"]


# ---------------------------------------------------------------- zet_uit
def test_power_off_needs_a_finger_on_the_screen(monkeypatch):
    done = []
    monkeypatch.setattr(tools, "power", lambda actie: done.append(actie) or {"ok": True})
    monkeypatch.setattr(tools, "CONFIRM", None)
    assert "fout" in tools.zet_uit("uit", bevestigd=True)            # the model's word is not enough
    monkeypatch.setattr(tools, "CONFIRM", lambda q, t: False)
    assert "niet_gedaan" in tools.zet_uit("uit")
    monkeypatch.setattr(tools, "CONFIRM", lambda q, t: None)
    assert "niet_gedaan" in tools.zet_uit("herstart")
    monkeypatch.setattr(tools, "CONFIRM", lambda q, t: True)
    assert tools.zet_uit("herstart")["ok"] and done == ["herstart"]
    assert "zet_uit" in {d["name"] for d in tools.DECLARATIONS}
    params = next(d for d in tools.DECLARATIONS if d["name"] == "zet_uit")["parameters"]["properties"]
    assert "bevestigd" not in params


def test_phone_cannot_run_power_as_a_tool(monkeypatch):
    from willie.remote import Remote
    called, sent = [], []
    monkeypatch.setattr(tools, "call", lambda name, args: called.append(name) or {"ok": True})
    remote = Remote()
    remote._publish = lambda topic, payload, **k: sent.append(payload)
    remote._tool({"id": "1", "name": "zet_uit", "args": {"actie": "uit"}})
    assert not called and "fout" in sent[0]["result"]
    remote._tool({"id": "2", "name": "status", "args": {}})
    assert called == ["status"]


# ---------------------------------------------------------------- Face.confirm
class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def face():
    f = Face(clock=Clock(), autostart=False)
    f.touch = object()                 # "a touch screen exists"; input comes via confirm_input
    return f


def ask(face, timeout=20.0):
    box = {}
    thread = threading.Thread(target=lambda: box.setdefault("answer", face.confirm("Uitzetten?", timeout)))
    thread.start()
    for _ in range(200):
        if face._confirm is not None:
            break
        threading.Event().wait(0.005)
    return thread, box


def test_hold_says_yes(face):
    thread, box = ask(face)
    start = face.clock.now + 1.0
    face.confirm_input(start + 0.5, True, start, [])
    assert face.snapshot(start + 0.5).state == "confirm"
    assert 0.3 < face.snapshot(start + 0.5).confirm[1] < 0.4          # the ring fills
    face.confirm_input(start + 1.6, True, start, [])
    thread.join(2)
    assert box["answer"] is True
    assert face.snapshot(start + 1.7).confirm[4] is True               # the tick shows
    assert face.snapshot(start + 3.0).state != "confirm"               # and goes away


def test_tap_says_no_and_a_pet_never_says_yes(face):
    thread, box = ask(face)
    start = face.clock.now + 1.0
    face.confirm_input(start + 0.3, False, start, [("tap", 10, 10)])
    thread.join(2)
    assert box["answer"] is False


def test_a_touch_that_was_already_there_counts_for_nothing(face):
    thread, box = ask(face, timeout=5)
    before = face.clock.now - 0.5                                      # finger down before the question
    face.confirm_input(face.clock.now + 3, True, before, [])
    face.confirm_input(face.clock.now + 3.1, False, before, [("tap", 1, 1)])
    assert face._confirm["answered"] is None
    face.snapshot(face.clock.now + 6)                                  # time out
    thread.join(2)
    assert box["answer"] is None


def test_no_touch_screen_is_never_a_yes():
    assert Face(clock=Clock(), autostart=False).confirm("Uitzetten?", 1) is None


@pytest.mark.parametrize("answer_age", [None, 0.1, 0.5])
@pytest.mark.parametrize("answer", [True, False, None])
def test_confirm_screen_renders(answer, answer_age):
    fb = Framebuffer.canvas()
    renderer = Renderer()
    view = View(state="confirm", mic=True, confirm=("Helemaal uitzetten?", 0.4, 0.7, 14.0, answer, answer_age, 0.2))
    for i in range(5):
        renderer.draw(fb, view, i * .04)
    assert any(fb.memory)


# ---------------------------------------------------------------- memory on the server
def test_robot_memory_goes_to_the_server_only(tmp_path, monkeypatch):
    calls = []

    def hub(name, args, timeout):
        calls.append(name)
        return {"context": "Wat je weet: X"} if name == "geheugen_context" else {"ok": True}

    monkeypatch.setattr(memory, "DIR", tmp_path / "memory")
    monkeypatch.setattr(memory, "ON_SERVER", True)
    monkeypatch.setattr(memory, "HUB_CALL", hub)
    monkeypatch.setattr(memory, "_unsent", [])
    assert memory.note("Wouter drinkt koffie zwart")["ok"]
    assert memory.search("koffie")["ok"]
    assert memory.context() == "Wat je weet: X"
    assert memory.digest([("user", "hoi"), ("willie", "hallo")], datetime.now())["ok"]
    assert calls == ["geheugen_onthoud", "geheugen_zoek", "geheugen_context", "geheugen_gesprek"]
    assert not (tmp_path / "memory").exists()                          # nothing on the card


def test_conversation_waits_in_ram_while_the_server_is_away(tmp_path, monkeypatch):
    up = {"on": False}
    sent = []

    def hub(name, args, timeout):
        if not up["on"]:
            return {"fout": "De thuisserver is niet bereikbaar"}
        sent.append(args)
        return {"ok": True}

    monkeypatch.setattr(memory, "DIR", tmp_path / "memory")
    monkeypatch.setattr(memory, "ON_SERVER", True)
    monkeypatch.setattr(memory, "HUB_CALL", hub)
    monkeypatch.setattr(memory, "_unsent", [])
    memory.digest([("user", "eerste")], datetime.now())
    assert len(memory._unsent) == 1 and not (tmp_path / "memory").exists()
    up["on"] = True
    memory.digest([("user", "tweede")], datetime.now())
    assert [a["gesprek"][0][1] for a in sent] == ["eerste", "tweede"] and memory._unsent == []


def test_server_side_digest_saves_the_conversation(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DIR", tmp_path)
    monkeypatch.setattr(memory, "FACTS", tmp_path / "facts.md")
    monkeypatch.setattr(memory, "SUMMARIES", tmp_path / "summaries.md")
    monkeypatch.setattr(memory, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(memory, "PENDING", tmp_path / "pending.md")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    result = memory.remote_digest([["user", "hoi"], ["willie", "hallo"], ["evil", "x"]], "2026-09-24T21:00:00")
    assert result["ok"]
    saved = (tmp_path / "sessions" / "2026-09-24.md").read_text()
    assert "Wouter: hoi" in saved and "evil" not in saved


# ---------------------------------------------------------------- free key first, paid as fallback
def test_talking_tries_the_free_key_then_the_paid_one(monkeypatch):
    from willie.voice import talk_key, talk_keys
    from willie.voice.gemini_live import GeminiLiveAdapter
    monkeypatch.setenv("GEMINI_API_KEY", "PAID")
    monkeypatch.setenv("GEMINI_API_KEY_FREE", "FREE")
    assert talk_key() == "FREE" and talk_keys() == ["FREE", "PAID"]
    assert GeminiLiveAdapter().keys == ["FREE", "PAID"]
    assert GeminiLiveAdapter(api_key="OTHER").keys == ["OTHER"]
    monkeypatch.delenv("GEMINI_API_KEY_FREE")
    assert talk_keys() == ["PAID"]
