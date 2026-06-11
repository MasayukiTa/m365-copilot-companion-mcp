"""Tests for tools/trace_ops.py -- the signature-preserving tool-call tracer.

The load-bearing assertion is signature preservation: FastMCP builds each tool's schema
from inspect.signature(fn), so if the trace wrapper changed the signature the tool would
disappear from Copilot. We prove the wrapped function is signature-identical, that it is
a no-op when disabled, that it records bound-named args + errors, and that the tail
reader reads back.

Run:  .venv\\Scripts\\python.exe tools\\test_trace.py
"""
import inspect
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools import trace_ops

results = []


def check(name, cond):
    results.append(bool(cond))
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


def sample(path: str, count: int = 3) -> str:
    """A sample tool. First line is the summary."""
    return "%s x%d" % (path, count)


def boom(x: int) -> int:
    """Raises."""
    raise ValueError("nope %d" % x)


def main():
    # 1. disabled by default -> returns the SAME function object (zero overhead/risk)
    os.environ.pop("MCP_TRACE_TOOLCALLS", None)
    check("disabled_is_identity", trace_ops.wrap_for_trace(sample) is sample)
    check("tracing_enabled_false", trace_ops.tracing_enabled() is False)

    # 2. enabled -> wrapper preserves signature, annotations, name, doc EXACTLY
    os.environ["MCP_TRACE_TOOLCALLS"] = "1"
    w = trace_ops.wrap_for_trace(sample)
    check("enabled_wraps", w is not sample)
    check("sig_identical", inspect.signature(w) == inspect.signature(sample))
    check("annotations_identical", w.__annotations__ == sample.__annotations__)
    check("name_doc_preserved", w.__name__ == "sample" and (w.__doc__ or "").startswith("A sample"))

    # 3. wrapper returns the real result and records a bound-named arg line
    out = w("foo.py", count=5)
    check("result_passthrough", out == "foo.py x5")
    tail = trace_ops.toolcalls_tail(50)
    check("recorded_call", "sample" in tail and "path=foo.py" in tail and "count=5" in tail)

    # 4. errors are recorded AND re-raised (tracing is transparent)
    wb = trace_ops.wrap_for_trace(boom)
    raised = False
    try:
        wb(7)
    except ValueError:
        raised = True
    check("error_reraised", raised)
    check("error_recorded", "boom" in trace_ops.toolcalls_tail(50))

    # 5. long args are truncated at record time (write_file content must not bloat trace)
    huge = "x" * 5000
    summ = trace_ops._summarize(huge, 200)
    check("args_truncated", len(summ) < 260 and "(5000 chars)" in summ)

    os.environ.pop("MCP_TRACE_TOOLCALLS", None)
    print("\n=== %d/%d trace checks passed ===" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
