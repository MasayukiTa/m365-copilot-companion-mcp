"""End-to-end (no browser) test that an acceptance check survives the whole pipeline:

    folder_coder.generate_goals(verify=True)   -- attaches a py_compile check
      -> write_goals_file                       -- serializes the dict goal as a JSON line
      -> fleet_runner._read_goals               -- parses the JSON line back to a dict
      -> relay_fleet.goal_fields                -- yields (text, [check], cwd)
      -> acceptance.run_all_blocking            -- the check actually runs on disk

Proves the gate a user gets from "point at a folder with --verify" is real all the way
to a live compile of the edited file.

Run:  .venv\\Scripts\\python.exe relay\\test_folder_verify.py
"""
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from relay.acceptance import run_all_blocking
from relay.folder_coder import generate_goals, write_goals_file
from relay.relay_fleet import goal_fields

results = []


def check(name, cond):
    results.append(bool(cond))
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


class _Args:
    goal = None


def main():
    tmp = tempfile.mkdtemp(prefix="fcv_")
    open(os.path.join(tmp, "mod.py"), "w", encoding="utf-8").write("def f():\n    return 1\n")
    open(os.path.join(tmp, "notes.md"), "w", encoding="utf-8").write("# notes\n")

    # 1. generate_goals --verify attaches a py_compile check to the .py EDIT goal only
    goals = generate_goals(tmp, "add type hints", mode="per-file", verify=True)
    py_goals = [g for g in goals if isinstance(g, dict)]
    md_goals = [g for g in goals if not isinstance(g, dict)]
    check("py_goal_has_check", len(py_goals) == 1
          and py_goals[0]["checks"][0]["type"] == "py_compile")
    check("md_goal_plain", len(md_goals) == 1)   # .md edit goal carries no check

    # 2. --check-cmd attaches a shell check to every per-file EDIT goal
    goals2 = generate_goals(tmp, "x", mode="per-file", check_cmd="python -c \"print(1)\"")
    check("check_cmd_on_all", all(isinstance(g, dict)
          and any(c["type"] == "shell" for c in g["checks"]) for g in goals2))

    # 3. round-trip through the goals file + fleet_runner's JSON-line parser
    gf = os.path.join(tmp, ".fleet_goals.txt")
    write_goals_file(goals, gf)
    a = _Args(); a.goals_file = gf
    from relay.fleet_runner import _read_goals
    parsed = _read_goals(a)
    check("roundtrip_count", len(parsed) == len(goals))
    dict_back = [g for g in parsed if isinstance(g, dict)]
    check("roundtrip_dict_preserved", len(dict_back) == 1
          and dict_back[0]["checks"][0]["type"] == "py_compile")

    # 4. goal_fields yields the check, and it actually runs against the real file
    text, checks, cwd = goal_fields(dict_back[0])
    check("goal_fields_cwd", cwd == os.path.abspath(tmp))
    passed, detail = run_all_blocking(checks, cwd=cwd)
    check("live_compile_pass", passed)

    # 5. break the file -> the same gate now fails (ground truth, not a claim)
    open(os.path.join(tmp, "mod.py"), "w", encoding="utf-8").write("def f(:\n  pass\n")
    passed2, detail2 = run_all_blocking(checks, cwd=cwd)
    check("live_compile_fail_on_broken", (not passed2)
          and ("SyntaxError" in detail2 or "invalid syntax" in detail2))

    print("\n=== %d/%d folder-verify checks passed ===" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
