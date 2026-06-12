"""eval_one.py -- run the HIDDEN canonical test for one HumanEval problem against the
agent's solution.py. Prints OK (exit 0) if all asserts pass, else HIDDEN_TESTS_FAILED
(exit 1) WITHOUT leaking the test/expected values (so the agent must solve from the spec,
not by reading the tests). Used both as the verification-gate check (drives iteration) and
by score.py for the final ground-truth tally.

  python bench/eval_one.py <safe_id> <solution_folder>
"""
import json
import os
import sys


def main():
    if len(sys.argv) < 3:
        print("USAGE eval_one.py <safe_id> <folder>")
        return 2
    safe_id, folder = sys.argv[1], sys.argv[2]
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_data")
    try:
        prob = json.load(open(os.path.join(data_dir, safe_id + ".json"), encoding="utf-8"))
    except Exception:
        print("NO_PROBLEM_DATA")
        return 2
    sol_path = os.path.join(folder, "solution.py")
    if not os.path.isfile(sol_path):
        print("NO_SOLUTION")
        return 1
    sol = open(sol_path, encoding="utf-8").read()
    program = sol + "\n\n" + prob["test"] + "\n\ncheck(" + prob["entry_point"] + ")\n"
    ns = {}
    try:
        exec(compile(program, "<bench>", "exec"), ns)
    except Exception:
        print("HIDDEN_TESTS_FAILED")     # generic -- do not reveal expected values
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
