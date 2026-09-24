"""The core process end to end: fake MCU <- core (safety, motion, mood) <- control socket."""
import os
import shutil
import subprocess
import tempfile
import sys
import time

import pytest

from willie import control

ROOT = os.path.join(os.path.dirname(__file__), "..")


@pytest.fixture
def core(tmp_path):
    port = str(tmp_path / "mcu")
    mcu = subprocess.Popen([sys.executable, os.path.join(ROOT, "tools", "fake_mcu.py"), port],
                           stdout=subprocess.PIPE, text=True)
    mcu.stdout.readline()
    (tmp_path / "willie.yaml").write_text(f"link:\n  port: {port}\n  baud: 115200\n")
    data = tempfile.mkdtemp(prefix="w", dir="/tmp")   # a Unix socket path must stay < 104 chars
    env = dict(os.environ, WILLIE_CONFIG=str(tmp_path / "willie.yaml"), WILLIE_DATA=data)
    proc = subprocess.Popen([sys.executable, "-m", "willie"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sock = control.Path(data) / "control.sock"
    end = time.time() + 15
    while time.time() < end:
        if sock.exists() and control.request("state", path=sock).get("state_age_s") is not None:
            break
        time.sleep(0.1)
    yield sock
    proc.terminate()
    proc.wait(5)
    mcu.kill()
    shutil.rmtree(data, ignore_errors=True)


def test_move_turn_and_mood_through_the_socket(core):
    ask = lambda cmd, **kw: control.request(cmd, path=core, **kw)   # noqa: E731
    state = ask("state")
    assert state["link"] and state["estop"] == [] and set(state["mood"]) >= {"energy", "boredom"}
    moved = ask("move", m=0.2)
    assert moved["ok"] and abs(moved["afstand_mm"] - 200) <= 25, moved
    assert ask("turn", deg=45)["ok"]
    assert ask("event", name="pet") == {"ok": True}
    assert ask("mood")["context"].startswith("stemming: ")
    assert "onbekend" in ask("dans")["fout"]
    assert ask("state")["behaviour"] == "respect"            # just driven: he stays put
    assert ask("event", name="conversation_start") == {"ok": True}
    time.sleep(0.7)
    assert ask("state")["behaviour"] == "talk"
    assert "antwoordt niet" in control.request("state", path=core.parent / "nope.sock")["fout"]
