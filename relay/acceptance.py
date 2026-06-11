"""acceptance.py -- machine-checkable acceptance criteria for the relay loop (spec 3-3).

The relay must NEVER trust Copilot's self-reported "DONE". When a goal carries an
acceptance check, the frame re-derives ground truth LOCALLY -- runs the test, compiles
the file, greps the output -- and only accepts DONE if the check passes; otherwise it
feeds the real failure back to Copilot and keeps iterating. This is exactly what makes
the loop Claude-Code-grade: "done" means proven-done, not claimed-done. It is the
"verification loop" of spec 3-3 (the frame calls the tool layer directly, bypassing the
oracle) wired into the control loop as an acceptance GATE.

Checks are NON-BLOCKING so a long check (a full test run) never freezes the fleet's
single-thread round-robin: build a Check, call start(), then poll() each tick until it
returns a (passed, detail) tuple. A separate OS process does the work; poll() only asks
proc.poll(), so every other worker keeps advancing meanwhile.

A check spec is a plain dict (so it can travel inside a goals-file JSON line):
    {"type": "shell",        "cmd": "python -m pytest -q", "expect_code": 0}
    {"type": "shell",        "argv": ["node", "build.js"], "expect_stdout": "OK"}
    {"type": "pytest",       "args": "-q tests/"}
    {"type": "py_compile",   "path": "pkg/mod.py"}
    {"type": "file_exists",  "path": "out/report.csv"}
    {"type": "file_contains","path": "out/log.txt", "needle": "PASS", "regex": false}

TRUST MODEL: a check is run with the same authority as the goal it rides on. Both come
from the local operator (the goals file / folder_coder / the cockpit), never from the
Copilot oracle -- so a check is allowed to run shell commands. Never build a check spec
from untrusted/oracle-produced text.

stdlib only -- runs anywhere the repo's Python runs.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time

# How much stdout/stderr to feed back to Copilot on failure. Enough to see the real
# error (a traceback tail / a failing assertion) without flooding the next turn.
MAX_DETAIL = 1500

# Process-backed check types vs. instant (filesystem) check types.
_PROC_TYPES = frozenset({"shell", "pytest", "python", "py_compile", "import_smoke"})
_FILE_TYPES = frozenset({"file_exists", "file_contains"})
VALID_TYPES = _PROC_TYPES | _FILE_TYPES


def normalize_checks(spec):
    """Coerce a goal's check spec into a list[dict]. Accepts None, a single dict, or a
    list of dicts; silently drops non-dict members. Returns []."""
    if spec is None:
        return []
    if isinstance(spec, dict):
        return [spec]
    if isinstance(spec, (list, tuple)):
        return [c for c in spec if isinstance(c, dict)]
    return []


def _tail(text, limit=MAX_DETAIL):
    text = text or ""
    if len(text) <= limit:
        return text
    return "...(truncated)...\n" + text[-limit:]


class Check:
    """One acceptance check, run as a non-blocking step.

    Lifecycle:  c = Check(spec, cwd).start();  while c.poll() is None: ...; passed, detail = c.poll()

    poll() returns None while a process-backed check is still running, otherwise a
    (passed: bool, detail: str) tuple. File checks resolve at start() and poll()
    returns their result immediately. Never raises; a setup error becomes a failed
    result with the exception in `detail`.
    """

    def __init__(self, spec, cwd=None, default_timeout=180):
        self.spec = dict(spec or {})
        self.type = str(self.spec.get("type", "shell")).lower()
        # per-check cwd wins; else the goal's cwd; else the current dir
        self.cwd = self.spec.get("cwd") or cwd or None
        try:
            self.timeout = float(self.spec.get("timeout", default_timeout))
        except (TypeError, ValueError):
            self.timeout = float(default_timeout)
        self._proc = None
        self._out = None          # TemporaryFile for stdout
        self._err = None          # TemporaryFile for stderr
        self._deadline = None
        self._instant = None      # cached (passed, detail) for file checks / setup errors

    # -- description (for logs / cockpit) ------------------------------------
    def describe(self):
        s = self.spec
        if self.type == "shell":
            return "shell: " + str(s.get("cmd") or " ".join(s.get("argv", [])))[:80]
        if self.type == "pytest":
            return "pytest " + str(s.get("args", ""))[:80]
        if self.type == "python":
            return "python " + str(s.get("path") or "-c")[:80]
        if self.type == "py_compile":
            return "py_compile " + str(s.get("path", ""))[:80]
        if self.type == "import_smoke":
            return "import_smoke " + str(s.get("module") or s.get("path", ""))[:80]
        if self.type == "file_exists":
            return "file_exists " + str(s.get("path", ""))[:80]
        if self.type == "file_contains":
            return "file_contains %s ~ %r" % (s.get("path", ""), str(s.get("needle", ""))[:40])
        return "check(%s)" % self.type

    # -- argv construction for process-backed checks -------------------------
    def _build(self):
        """Return (target, shell) for subprocess.Popen. `target` is an argv list
        (shell=False) or a command string (shell=True)."""
        s = self.spec
        if self.type == "shell":
            if s.get("argv"):
                return list(s["argv"]), False
            return str(s.get("cmd", "")), True          # cmd string -> shell
        if self.type == "pytest":
            args = s.get("args", "")
            extra = args if isinstance(args, list) else str(args).split()
            return [sys.executable, "-m", "pytest"] + extra, False
        if self.type == "python":
            if s.get("path"):
                return [sys.executable, str(s["path"])], False
            return [sys.executable, "-c", str(s.get("code", ""))], False
        if self.type == "py_compile":
            # compile a file with doraise -> non-zero exit + traceback on a syntax error.
            # path passed as argv (not interpolated) so Windows backslashes never bite.
            return [sys.executable, "-c",
                    "import py_compile,sys; py_compile.compile(sys.argv[1], doraise=True)",
                    str(s.get("path", ""))], False
        if self.type == "import_smoke":
            # actually IMPORT the module -> catches load-time errors a compile misses
            # (undefined names at module scope, bad imports, failing init). Derive a
            # dotted module from `module`, or from `path` relative to cwd if not given.
            mod = s.get("module")
            if not mod and s.get("path"):
                rel = str(s["path"])
                rel = rel[:-3] if rel.lower().endswith(".py") else rel
                mod = rel.replace("\\", "/").strip("/").replace("/", ".")
            return [sys.executable, "-c",
                    "import importlib,sys; importlib.import_module(sys.argv[1])",
                    str(mod or "")], False
        return None, False

    def start(self):
        """Kick off the check. Returns self so you can write Check(...).start()."""
        try:
            if self.type in _FILE_TYPES:
                self._instant = self._eval_file()
            elif self.type in _PROC_TYPES:
                target, shell = self._build()
                self._out = tempfile.TemporaryFile()
                self._err = tempfile.TemporaryFile()
                self._proc = subprocess.Popen(
                    target, cwd=self.cwd, shell=shell,
                    stdout=self._out, stderr=self._err,
                    stdin=subprocess.DEVNULL,
                )
                self._deadline = time.time() + self.timeout
            else:
                self._instant = (False, "[acceptance: unknown check type %r]" % self.type)
        except Exception as e:
            self._instant = (False, "[acceptance start error: %s: %s]" % (type(e).__name__, e))
        return self

    def poll(self):
        """None while still running; else (passed, detail). Idempotent after finish."""
        if self._instant is not None:
            return self._instant
        if self._proc is None:
            return (False, "[acceptance: check not started]")
        rc = self._proc.poll()
        if rc is None:
            if time.time() > (self._deadline or 0):
                try:
                    self._proc.kill()
                except Exception:
                    pass
                try:
                    self._proc.wait(timeout=5)
                except Exception:
                    pass
                self._instant = self._finish(rc=None, timed_out=True)
                return self._instant
            return None
        self._instant = self._finish(rc=rc)
        return self._instant

    # -- result assembly -----------------------------------------------------
    def _read(self, fh):
        try:
            fh.seek(0)
            return fh.read().decode("utf-8", "replace")
        except Exception:
            return ""
        finally:
            try:
                fh.close()
            except Exception:
                pass

    def _finish(self, rc, timed_out=False):
        out = self._read(self._out) if self._out else ""
        err = self._read(self._err) if self._err else ""
        desc = self.describe()
        if timed_out:
            detail = "[%s] TIMEOUT after %.0fs\n%s" % (desc, self.timeout, _tail(err or out))
            return (False, detail)
        expect_code = self.spec.get("expect_code", 0)
        expect_stdout = self.spec.get("expect_stdout")
        code_ok = (expect_code is None) or (rc == expect_code)
        stdout_ok = True
        if expect_stdout is not None:
            stdout_ok = str(expect_stdout) in out
        passed = bool(code_ok and stdout_ok)
        if passed:
            return (True, "[%s] PASS (exit %s)" % (desc, rc))
        why = []
        if not code_ok:
            why.append("exit %s != %s" % (rc, expect_code))
        if not stdout_ok:
            why.append("stdout missing %r" % str(expect_stdout)[:60])
        combined = (err + ("\n" + out if out else "")) if err else out
        return (False, "[%s] FAIL (%s)\n%s" % (desc, "; ".join(why), _tail(combined)))

    # -- instant filesystem checks -------------------------------------------
    def _abs(self, path):
        path = str(path or "")
        if self.cwd and not os.path.isabs(path):
            return os.path.join(self.cwd, path)
        return path

    def _eval_file(self):
        s = self.spec
        if self.type == "file_exists":
            paths = s.get("paths") or [s.get("path", "")]
            missing = [p for p in paths if not os.path.exists(self._abs(p))]
            if missing:
                return (False, "[file_exists] missing: %s" % ", ".join(missing))
            return (True, "[file_exists] PASS (%d path(s))" % len(paths))
        if self.type == "file_contains":
            p = self._abs(s.get("path", ""))
            needle = str(s.get("needle", ""))
            if not os.path.isfile(p):
                return (False, "[file_contains] no such file: %s" % p)
            try:
                text = open(p, encoding="utf-8", errors="replace").read()
            except Exception as e:
                return (False, "[file_contains] read error: %s" % e)
            if s.get("regex"):
                if re.search(needle, text):
                    return (True, "[file_contains] regex matched")
                return (False, "[file_contains] regex %r not found in %s" % (needle[:60], p))
            if needle in text:
                return (True, "[file_contains] substring found")
            return (False, "[file_contains] %r not found in %s" % (needle[:60], p))
        return (False, "[acceptance: unhandled file check %r]" % self.type)


def run_check_blocking(spec, cwd=None, poll_s=0.25):
    """Convenience for the single-relay loop (already blocking): run one check to
    completion and return (passed, detail). The fleet uses the non-blocking Check
    directly so it never stalls the round-robin."""
    c = Check(spec, cwd=cwd).start()
    while True:
        r = c.poll()
        if r is not None:
            return r
        time.sleep(poll_s)


def run_all_blocking(specs, cwd=None):
    """Run a list of check specs in order; stop at the first failure. Returns
    (passed, detail) -- detail is the failing check's detail, or a PASS summary."""
    specs = normalize_checks(specs)
    if not specs:
        return (True, "[acceptance] no checks")
    for i, sp in enumerate(specs, 1):
        passed, detail = run_check_blocking(sp, cwd=cwd)
        if not passed:
            return (False, "check %d/%d %s" % (i, len(specs), detail))
    return (True, "[acceptance] all %d check(s) passed" % len(specs))
