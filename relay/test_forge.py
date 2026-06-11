"""Tests for operator-A forge-in-the-loop: marker extraction, frame-side forge_core, and
the run_relay FORGE branch. Forged files are written to tools/auto/ and cleaned up.

Run:  .venv\\Scripts\\python.exe relay\\test_forge.py
"""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from relay.copilot_autopilot_relay import extract_forge, run_relay
from tools.foundry import AUTO_DIR, forge_core

results = []


def check(name, cond):
    results.append(bool(cond))
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


def _rm(name):
    try:
        os.remove(os.path.join(str(AUTO_DIR), name + ".py"))
    except OSError:
        pass


class MockDriver:
    def __init__(self, responses):
        self.responses = list(responses)
        self.i = -1
        self.sent = []

    def send(self, text):
        self.i += 1
        self.sent.append(text)

    def wait_for_idle(self, timeout_s=0):
        return True

    def read_last_response(self):
        return self.responses[self.i] if self.i < len(self.responses) else "CONTINUE"


def main():
    # --- extract_forge ---
    nm = extract_forge("FORGE: slugify\n```python\ndef slugify(s):\n    return s.lower()\n```")
    check("extract_ok", nm and nm[0] == "slugify" and "def slugify" in nm[1])
    check("extract_no_block", extract_forge("FORGE: slugify (no code)") is None)
    check("extract_no_marker", extract_forge("just text\n```python\nx=1\n```") is None)

    # --- forge_core: good compiles + stages; bad is discarded ---
    _rm("test_forge_good")
    r = forge_core("test_forge_good", "def helper():\n    return 42\n")
    check("forge_good", "syntax OK" in r
          and os.path.isfile(os.path.join(str(AUTO_DIR), "test_forge_good.py")))
    r = forge_core("test_forge_bad", "def f(:\n  pass\n")
    check("forge_bad_discarded", r.startswith("[forge error")
          and not os.path.isfile(os.path.join(str(AUTO_DIR), "test_forge_bad.py")))
    check("forge_bad_name", forge_core("123bad", "x=1").startswith("[forge error"))
    _rm("test_forge_good")

    # --- run_relay FORGE branch: forge mid-run, then DONE ---
    _rm("test_forge_loop")
    drv = MockDriver([
        "新ツールを作ります。\nFORGE: test_forge_loop\n```python\ndef tfl():\n    return 7\n```",
        "ツールを配置できました。完了 DONE",
    ])
    outcome = run_relay(drv, goal="g", run_id="test_forge", notify=lambda *a: None,
                        sleep_s=0, forge=True, max_turns=6)
    forged = os.path.isfile(os.path.join(str(AUTO_DIR), "test_forge_loop.py"))
    check("loop_forged_and_done", outcome == "DONE" and forged)
    # the frame fed the forge result back into the next turn
    check("loop_fed_result", any("ツール作成結果" in s for s in drv.sent))
    _rm("test_forge_loop")

    # --- forge OFF: a FORGE marker is ignored (no file, normal flow) ---
    _rm("test_forge_off")
    drv = MockDriver(["FORGE: test_forge_off\n```python\ndef x():\n    return 1\n```\nDONE"])
    outcome = run_relay(drv, goal="g", run_id="test_forge_off", notify=lambda *a: None,
                        sleep_s=0, forge=False, max_turns=4)
    check("forge_off_ignored",
          not os.path.isfile(os.path.join(str(AUTO_DIR), "test_forge_off.py")))
    _rm("test_forge_off")

    print("\n=== %d/%d forge checks passed ===" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
