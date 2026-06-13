"""test_feedback.py -- turn raw test-runner output into a sharp failure summary.

A generic, repo-agnostic version of the structured failure extraction that was first
written for SWE-bench (bench/swe_check.py::_parse_failure_log). The acceptance gate
(relay/acceptance.py) feeds the result back to the coding agent on a failed check, so the
agent sees the FAILING TEST NAMES, the ERROR TYPE / MESSAGE, and the FILE:LINE where it was
raised -- instead of a wall of raw stdout. That is the difference between an agent that can
locate the missed spot and one that retries blind.

The single public entry point is:

    summarize_test_failure(text) -> str

It understands the three test-runner output shapes a Python project is likely to emit:

  * pytest                : "FAILED path::test - reason" summary lines + "E   <Error>: ..."
                            assertion blocks + "path/file.py:NN: SomeError" source pointers.
  * unittest (django-style): "FAIL: test_x (module.Class)" / "ERROR: test_x (module.Class)"
                            result headers, a bare "Traceback (most recent call last):", and a
                            column-0 "<Error>: ..." line.
  * sympy custom runner   : "____ path.py:test_x ____" banners followed by a frame list and a
                            bare exception line.

Design contract:
  * Pure function, stdlib only, no I/O. Caller is responsible for reading the log.
  * COMPLETELY exception-safe: any internal error falls back to a raw tail; it never raises,
    so it can never break the verification flow that calls it.
  * Bounded output: at most a handful of failing tests / error lines / source pointers, and a
    raw-tail fallback for unknown formats -- enough to act on, not a flood.

NOTE (future dedup): bench/swe_check.py still carries its own near-identical parser because
that file is the critical path of a live SWE-bench batch and must not be touched while the
batch runs. When the fleet is idle, swe_check._parse_failure_log / _failure_feedback should be
re-pointed at this module so there is a single implementation. Until then the duplication is
intentional and accepted.
"""
from __future__ import annotations

import re

# How many raw, non-blank lines to surface when the format is unknown / parsing yields nothing.
_RAW_TAIL_LINES = 20
# Caps on each structured section so the summary stays terse.
_MAX_TESTS = 8
_MAX_ERRORS = 6
_MAX_POINTERS = 4

# --- format-recognition regexes (mirrors bench/swe_check.py, generic naming) -----------------

# a bare/raised exception line at column 0: "AssertionError", "ValueError: bad name",
# "django.core.exceptions.ImproperlyConfigured: ...". Matches django/sympy custom runners
# (and the "E " body once that prefix is stripped).
_EXC_RE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Warning|Failure)(?::|$)")
# pytest's source pointer printed under the traceback: "path/file.py:123: SomeError"
_PYTEST_PTR_RE = re.compile(r"^.+\.py:\d+: \w*(?:Error|Exception|Warning|Failed)\b")
# unittest (django) runner result header: "FAIL: test_x (module.Class)" / "ERROR: ..."
_UNITTEST_RES_RE = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([^)]+)\)")
# sympy custom-runner failure banner: "____ sympy/.../test_foo.py:test_bar ____"
_SYMPY_BANNER_RE = re.compile(r"^_+ (\S+\.py:\S+) _+$")
# a traceback frame line: '  File ".../x.py", line 12, in test_foo'
_TB_FRAME_RE = re.compile(r'^\s*File "([^"]+)", line (\d+), in (\S+)')
# ANSI color escapes pytest emits, stripped before parsing.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _raw_tail(text, n=_RAW_TAIL_LINES, header="Log tail"):
    """Last *n* non-blank lines of *text*, indented under a header. The universal fallback."""
    tail = [ln for ln in (text or "").splitlines() if ln.strip()][-n:]
    if not tail:
        return ""
    return header + ":\n" + "\n".join("  " + t for t in tail)


def summarize_test_failure(text):
    """Summarize a test runner's failure output into failing-test names + error + file:line.

    Returns a short multi-line string suitable to feed back to a coding agent. Never raises:
    on any parse error (or an unrecognized format) it falls back to a raw log tail, so the
    caller's verification flow is never interrupted.

    Args:
        text: the combined stdout/stderr of a test run (pytest / unittest / sympy).

    Returns:
        A human-readable summary string. Empty input yields a short placeholder.
    """
    try:
        log = text or ""
        if not log.strip():
            return "(no test output captured.)"
        log = _ANSI_RE.sub("", log)
        return _parse(log)
    except Exception as exc:  # never let a parser bug break the verify flow
        tail = _raw_tail(text, 25, header="--- TEST FAILURE (raw tail; parser error: %s)" % exc)
        return tail or ("(test-output summary failed: %s)" % exc)


def _parse(log):
    lines = log.splitlines()
    failed = _failing_tests(lines)
    err_tail = _error_lines(lines)
    ptr = _source_pointers(lines)

    parts = ["--- TEST FAILURE SUMMARY (use this to find the exact spot) ---"]
    if failed:
        parts.append("Failing tests (%d):" % len(failed))
        parts.extend("  " + f for f in failed[:_MAX_TESTS])
        if len(failed) > _MAX_TESTS:
            parts.append("  ... and %d more" % (len(failed) - _MAX_TESTS))
    if err_tail:
        parts.append("Error:")
        parts.extend("  " + e for e in err_tail)
    if ptr:
        parts.append("Raised at:")
        parts.extend("  " + p for p in ptr)
    if not failed and not err_tail and not ptr:
        # unknown format -> hand back the meaningful tail rather than nothing
        body = _raw_tail(log, _RAW_TAIL_LINES)
        if body:
            parts.append(body)
        else:
            parts.append("(no recognizable failure detail.)")
    return "\n".join(parts)


def _failing_tests(lines):
    """Collect failing test identifiers across pytest / unittest / sympy formats (deduped)."""
    failed = []
    seen = set()

    def add(name):
        if name and name not in seen:
            seen.add(name)
            failed.append(name)

    for raw in lines:
        ln = raw.rstrip()
        s = ln.lstrip()
        # pytest summary lines: "FAILED path::test - reason" / "ERROR path::test"
        if s.startswith("FAILED ") or (s.startswith("ERROR ") and "::" in s):
            name = s.split(" - ", 1)[0].strip()
            if not name.startswith(("FAILED (", "ERROR (")):  # skip "FAILED (errors=1)" footers
                add(name)
            continue
        m = _UNITTEST_RES_RE.match(ln)  # unittest (django) result header
        if m:
            add("%s (%s)" % (m.group(1), m.group(2)))
            continue
        m = _SYMPY_BANNER_RE.match(ln)  # sympy custom-runner banner
        if m:
            add(m.group(1))
    return failed


def _error_lines(lines):
    """The last few real error/assertion lines. pytest prefixes them with 'E '; unittest and
    sympy print them at column 0. Warnings (DeprecationWarning etc.) are noise, so prefer real
    errors and only fall back to warnings if nothing else surfaced."""
    err, warn = [], []
    for raw in lines:
        s = raw.lstrip()
        body = None
        if s.startswith("E ") or s == "E":
            body = s[2:].strip() or s.strip()
        elif _EXC_RE.match(raw.strip()):
            body = raw.strip()
        if body:
            head = body.split(":", 1)[0]
            (warn if "Warning" in head else err).append(body)
    return (err or warn)[-_MAX_ERRORS:]


def _source_pointers(lines):
    """The file:line where the failure was raised. pytest prints 'file.py:NN: Error'; unittest
    and sympy give traceback frames -- use the deepest (last) frames, where it was actually
    raised. Drop Warning-emission pointers (import-time DeprecationWarnings flood these)."""
    ptr = [ln.strip() for ln in lines
           if _PYTEST_PTR_RE.match(ln.strip()) and "Warning" not in ln]
    if not ptr:
        frames = ["%s:%s in %s" % m.groups() for ln in lines
                  for m in [_TB_FRAME_RE.match(ln)] if m and "Warning" not in ln]
        # prefer frames in the project's own test/source tree over library import frames
        own = [f for f in frames if "/tests/" in f or "/testbed/" in f or "\\tests\\" in f]
        ptr = (own or frames)
    return ptr[-_MAX_POINTERS:]
