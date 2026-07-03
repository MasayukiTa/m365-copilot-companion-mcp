"""golden.py -- golden-trajectory regression harness (MCP spec §21.10.3 / §18.7.4).

WHY: unit tests check individual functions; nothing in the repo previously replayed
a *sequence* of real tool calls end-to-end and asserted on the resulting state. A
change that quietly breaks a core tool's contract (e.g. write_file stops creating
parent dirs, read_file's error string format changes, list_directory drops an
entry) can slip through if every existing test mocks around it. This module lets
us pin down a small number of representative "golden trajectories" -- ordered
tool-call sequences over the SAFE file tools with declarative expectations -- and
replay them in CI. This is a regression fixture, NOT an LLM eval: no model is
involved, nothing here judges quality, it only proves "the tool still does what a
past trajectory recorded it doing."

SCHEMA ALIGNMENT (do not diverge): ToolCallRecord mirrors tools/trace_ops.py's
per-call JSONL record shape {ts, name, ok, dur_ms, args, result, error} field-for-
field, so a golden-trajectory record and a live trace record are the same shape
and (in principle) interchangeable/comparable. See trace_ops.record_call().

Design is deliberately small:
  * ToolCallRecord   -- one executed step, same fields as trace_ops.
  * Trajectory        -- an ordered list of steps; each step is a plain dict
                         {"tool": <callable>, "kwargs": {...}, "expect": {...}}.
  * run_trajectory()  -- executes each step against the REAL tool function,
                         records a ToolCallRecord, evaluates its `expect` spec,
                         and returns (records, failures). A failing step is
                         reported, never raised, so every step in the trajectory
                         still runs (mirrors trace_ops' "never raise" philosophy).

Expect-spec kinds (small, declarative; reuses tools/verify_ops.py primitives
rather than reinventing string/JSON checks):
  {"returns_contains": "substr"}        -- substring in the tool's return value.
  {"returns_equals": "exact"}           -- exact equality with the return value.
  {"file_exists": "path"}               -- path exists on disk (direct check;
                                            verify_ops has no bare-existence
                                            primitive, so this one is new).
  {"file_content_equals": ("path", "text")} -- exact file content match (direct
                                            check for the same reason above).
  {"file_contains": ("path", "needle")} -- delegates to
                                            verify_ops.verify_file_contains() and
                                            treats its "PASS:" prefix as success.
  {"raises_or_error": True/False}       -- True: the call must raise OR its return
                                            value must look like this repo's
                                            "[tool_name error: ...]" convention.
                                            False: the call must NOT raise and must
                                            NOT look like that error convention.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .verify_ops import verify_file_contains

_ERROR_MARKERS = ("error:", "[locked", "FAIL:")


@dataclass
class ToolCallRecord:
    """One executed step of a trajectory. Field names/meanings mirror
    trace_ops.record_call()'s JSONL entry {ts, name, ok, dur_ms, args, result,
    error} on purpose -- see module docstring "SCHEMA ALIGNMENT"."""

    name: str
    args: dict = field(default_factory=dict)
    ok: bool = True
    dur_ms: float = 0.0
    result: str = ""
    error: str = ""
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "ts": self.ts,
            "name": self.name,
            "ok": self.ok,
            "dur_ms": int(self.dur_ms),
            "args": self.args,
            "result": self.result,
            "error": self.error,
        }


Trajectory = list  # list[dict[str, Any]]; kept as a plain alias, see module docstring.


def _looks_like_error(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return any(marker in value for marker in _ERROR_MARKERS)


def _eval_expect(expect: dict, returned: Any, raised: Optional[Exception]) -> tuple[bool, str]:
    """Evaluate one expect-spec against a step's outcome.

    Returns (passed, detail). Never raises -- an unknown/malformed expect key is
    reported as a failure with an explanatory detail, not a crash, so a single bad
    fixture can't take down the whole trajectory run.
    """
    if not expect:
        return (raised is None), ("no exception" if raised is None else f"unexpected raise: {raised!r}")

    try:
        if "returns_contains" in expect:
            if raised is not None:
                return False, f"raised instead of returning: {raised!r}"
            needle = expect["returns_contains"]
            ok = isinstance(returned, str) and needle in returned
            return ok, f"expected substring {needle!r} in {returned!r}"

        if "returns_equals" in expect:
            if raised is not None:
                return False, f"raised instead of returning: {raised!r}"
            want = expect["returns_equals"]
            return (returned == want), f"expected {want!r}, got {returned!r}"

        if "file_exists" in expect:
            path = expect["file_exists"]
            from pathlib import Path

            ok = Path(path).exists()
            return ok, f"expected file to exist: {path}"

        if "file_content_equals" in expect:
            path, text = expect["file_content_equals"]
            from pathlib import Path

            p = Path(path)
            if not p.is_file():
                return False, f"expected file to exist: {path}"
            actual = p.read_text(encoding="utf-8")
            return (actual == text), f"content mismatch for {path}: expected {text!r}, got {actual!r}"

        if "file_contains" in expect:
            path, needle = expect["file_contains"]
            verdict = verify_file_contains(path, needle)
            return verdict.startswith("PASS:"), verdict

        if "raises_or_error" in expect:
            want_error = bool(expect["raises_or_error"])
            is_error = (raised is not None) or _looks_like_error(returned)
            return (is_error == want_error), (
                f"raised={raised!r} returned={returned!r} "
                f"(want_error={want_error}, is_error={is_error})"
            )

        return False, f"unknown expect spec: {expect!r}"
    except Exception as e:  # defensive: a bad fixture must not crash the harness
        return False, f"[expect evaluation error: {type(e).__name__}: {e}]"


def run_trajectory(steps: list, tools_by_name: Optional[dict] = None):
    """Execute a trajectory step by step against REAL tool functions.

    Each step is a dict:
        {"tool": <callable> or "name_in_tools_by_name",
         "kwargs": {...},
         "expect": {...}}       # see module docstring for expect-spec kinds
    An optional "name" key overrides the record name (else the callable's
    __name__, or the string itself when tool is looked up by name).

    Every step runs regardless of earlier failures -- a failing step is recorded
    as a failure, never raised, so a single broken step doesn't hide the state of
    the rest of the trajectory (mirrors trace_ops' "recording never raises into
    the call" philosophy, applied here to the *checking* side instead).

    Returns:
        (records, failures) -- records: list[ToolCallRecord] for every step, in
        order. failures: list[dict] with keys {step, name, detail} for each step
        whose expect-spec did not hold.
    """
    tools_by_name = tools_by_name or {}
    records: list[ToolCallRecord] = []
    failures: list[dict] = []

    for i, step in enumerate(steps):
        tool = step.get("tool")
        kwargs = step.get("kwargs") or {}
        expect = step.get("expect") or {}

        if isinstance(tool, str):
            fn = tools_by_name.get(tool)
            display_name = step.get("name", tool)
        else:
            fn = tool
            display_name = step.get("name") or getattr(tool, "__name__", "?")

        t0 = time.time()
        returned: Any = None
        raised: Optional[Exception] = None
        if fn is None:
            raised = LookupError(f"no tool registered for {tool!r}")
        else:
            try:
                returned = fn(**kwargs)
            except Exception as e:
                raised = e
        dur_ms = (time.time() - t0) * 1000

        ok, detail = _eval_expect(expect, returned, raised)

        rec = ToolCallRecord(
            name=display_name,
            args=dict(kwargs),
            ok=ok,
            dur_ms=dur_ms,
            result=("" if returned is None else str(returned))[:400],
            error=("" if raised is None else f"{type(raised).__name__}: {raised}"),
        )
        records.append(rec)

        if not ok:
            failures.append({"step": i, "name": display_name, "detail": detail})

    return records, failures
