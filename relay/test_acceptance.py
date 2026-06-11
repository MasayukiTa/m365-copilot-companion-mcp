"""Deterministic unit tests for relay/acceptance.py (the verification GATE primitives).

No browser, no network -- just the local check runner against scripted commands and
temp files. Proves: pass/fail on exit code, expect_stdout, py_compile on good/bad
source, file_exists / file_contains, timeout kill, the non-blocking poll() contract,
and run_all_blocking stopping at the first failure.

Run:  .venv\\Scripts\\python.exe relay\\test_acceptance.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from relay.acceptance import Check, normalize_checks, run_all_blocking, run_check_blocking

PY = sys.executable
results = []


def check(name, cond):
    results.append(bool(cond))
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


def main():
    # 1. shell exit 0 -> pass
    p, d = run_check_blocking({"type": "shell", "argv": [PY, "-c", "print('hi')"]})
    check("shell_exit0_pass", p)

    # 2. shell exit 1 -> fail
    p, d = run_check_blocking({"type": "shell", "argv": [PY, "-c", "import sys;sys.exit(1)"]})
    check("shell_exit1_fail", (not p) and "exit 1" in d)

    # 3. expect_stdout present -> pass
    p, d = run_check_blocking({"type": "shell", "argv": [PY, "-c", "print('BUILD-OK')"],
                               "expect_stdout": "BUILD-OK"})
    check("expect_stdout_pass", p)

    # 4. expect_stdout missing -> fail even though exit 0
    p, d = run_check_blocking({"type": "shell", "argv": [PY, "-c", "print('nope')"],
                               "expect_stdout": "BUILD-OK"})
    check("expect_stdout_fail", (not p) and "stdout missing" in d)

    # 5/6. py_compile on good and bad source
    tmp = tempfile.mkdtemp(prefix="acc_")
    good = os.path.join(tmp, "good.py")
    bad = os.path.join(tmp, "bad.py")
    open(good, "w", encoding="utf-8").write("x = 1\ndef f():\n    return x\n")
    open(bad, "w", encoding="utf-8").write("def f(:\n  pass\n")     # syntax error
    p, d = run_check_blocking({"type": "py_compile", "path": good})
    check("py_compile_good_pass", p)
    p, d = run_check_blocking({"type": "py_compile", "path": bad})
    check("py_compile_bad_fail", (not p) and ("SyntaxError" in d or "invalid syntax" in d))

    # 7. file_exists pass + relative-to-cwd resolution
    p, d = run_check_blocking({"type": "file_exists", "path": "good.py"}, cwd=tmp)
    check("file_exists_cwd_pass", p)
    p, d = run_check_blocking({"type": "file_exists", "path": "missing.py"}, cwd=tmp)
    check("file_exists_fail", not p)

    # 8. file_contains substring + regex
    logf = os.path.join(tmp, "log.txt")
    open(logf, "w", encoding="utf-8").write("run started\nRESULT: PASS\ndone\n")
    p, d = run_check_blocking({"type": "file_contains", "path": logf, "needle": "RESULT: PASS"})
    check("file_contains_substr_pass", p)
    p, d = run_check_blocking({"type": "file_contains", "path": logf,
                               "needle": r"RESULT:\s+PASS", "regex": True})
    check("file_contains_regex_pass", p)
    p, d = run_check_blocking({"type": "file_contains", "path": logf, "needle": "FAILURE"})
    check("file_contains_fail", not p)

    # 9. timeout -> fail with TIMEOUT
    t0 = time.time()
    p, d = run_check_blocking({"type": "shell", "argv": [PY, "-c", "import time;time.sleep(10)"],
                               "timeout": 1})
    check("timeout_fail", (not p) and "TIMEOUT" in d and (time.time() - t0) < 6)

    # 10. non-blocking poll() contract: returns None at least once while running
    c = Check({"type": "shell", "argv": [PY, "-c", "import time;time.sleep(1)"]}).start()
    saw_none = False
    for _ in range(200):
        r = c.poll()
        if r is None:
            saw_none = True
            time.sleep(0.02)
            continue
        break
    check("nonblocking_poll_contract", saw_none and c.poll()[0] is True)

    # 11. run_all_blocking stops at first failure (reports check 2/3)
    p, d = run_all_blocking([
        {"type": "shell", "argv": [PY, "-c", "print(1)"]},
        {"type": "shell", "argv": [PY, "-c", "import sys;sys.exit(2)"]},
        {"type": "shell", "argv": [PY, "-c", "print(3)"]},
    ])
    check("run_all_stops_at_first_fail", (not p) and "check 2/3" in d)

    # 12. run_all_blocking all-pass + normalize_checks shapes
    p, d = run_all_blocking([{"type": "file_exists", "path": good}])
    check("run_all_pass", p)
    check("normalize_none", normalize_checks(None) == [])
    check("normalize_dict", normalize_checks({"type": "shell"}) == [{"type": "shell"}])
    check("normalize_list", len(normalize_checks([{"a": 1}, "junk", {"b": 2}])) == 2)

    print("\n=== %d/%d acceptance checks passed ===" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
