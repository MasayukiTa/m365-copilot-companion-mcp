"""trace_ops.py -- optional, signature-preserving tool-call TRACING for observability.

WHY: when the relay drives the Copilot impl agent, the agent's MCP tool calls
(read_file / replace_in_file / run_python ...) do NOT appear in the chat DOM -- only
the final prose does. So "what did it actually edit/run?" -- the thing Claude Code
shows you for free -- is invisible. This module lets the SERVER record each tool
invocation to an append-only per-day JSONL that the relay / cockpit can tail, turning
the autonomous run into a Claude-Code-style, inspectable trail of concrete actions.

SAFETY (why this cannot destabilize the live server):
  * It is OFF by default. `wrap_for_trace(fn)` returns `fn` UNCHANGED unless the env
    var MCP_TRACE_TOOLCALLS is truthy. So wiring it into register() is a no-op until
    the operator opts in.
  * When ON, the wrapper PRESERVES the wrapped function's signature, type annotations,
    name and docstring (FastMCP builds each tool's schema from those via
    inspect.signature -- a naive *args/**kwargs wrapper would make the tool vanish from
    Copilot). test_trace.py asserts the signature is identical.
  * Recording failures are swallowed: tracing never changes a tool's return value or
    raises into a tool call.

Records: <MCP_ALLOWED_BASE>/.companion_runs/toolcalls_YYYY-MM-DD.jsonl, one JSON object
per call: {ts, name, ok, dur_ms, args, result, error}. args/result are truncated.
"""
import functools
import inspect
import json
import os
import time
from pathlib import Path

try:
    from .file_ops import ALLOWED_BASE
except Exception:                       # pragma: no cover - standalone import fallback
    ALLOWED_BASE = Path(os.path.expanduser("~"))

RUNS_DIR = ALLOWED_BASE / ".companion_runs"
_ARG_CAP = 200          # max chars per argument repr
_RESULT_CAP = 400       # max chars of a result summary
_ENV_FLAG = "MCP_TRACE_TOOLCALLS"


def tracing_enabled():
    """True if the operator opted into tool-call tracing (env MCP_TRACE_TOOLCALLS)."""
    return str(os.environ.get(_ENV_FLAG, "")).strip().lower() in ("1", "true", "yes", "on")


def _tracelog_path(day=None):
    day = day or time.strftime("%Y-%m-%d")
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    return RUNS_DIR / ("toolcalls_%s.jsonl" % day)


def _summarize(value, cap):
    """One-line, length-capped repr of an arg/result -- never raises."""
    try:
        if isinstance(value, str):
            s = value
        else:
            s = repr(value)
    except Exception:
        s = "<unreprable>"
    s = " ".join(s.split())
    if len(s) > cap:
        s = s[:cap] + "...(%d chars)" % len(s)
    return s


def _summarize_args(fn, args, kwargs):
    """Bind call args to parameter names so the trace reads name=value, not positional
    junk. Falls back to a positional list if binding fails."""
    try:
        sig = inspect.signature(fn)
        bound = sig.bind_partial(*args, **kwargs)
        return {k: _summarize(v, _ARG_CAP) for k, v in bound.arguments.items()}
    except Exception:
        out = {}
        for i, a in enumerate(args):
            out["arg%d" % i] = _summarize(a, _ARG_CAP)
        for k, v in (kwargs or {}).items():
            out[k] = _summarize(v, _ARG_CAP)
        return out


def record_call(name, args_summary, ok, dur_ms, result_summary="", error=""):
    """Append one tool-call record to today's trace log. Best-effort; never raises."""
    try:
        entry = {"ts": time.time(), "name": name, "ok": bool(ok),
                 "dur_ms": int(dur_ms), "args": args_summary,
                 "result": result_summary, "error": error}
        with _tracelog_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def wrap_for_trace(fn):
    """Return a signature-preserving wrapper that records each call -- OR `fn` unchanged
    when tracing is disabled (the default). Apply this inside register() so it composes
    with FastMCP's tool() decorator without affecting the schema."""
    if not tracing_enabled():
        return fn

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        t0 = time.time()
        args_summary = _summarize_args(fn, args, kwargs)
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            record_call(fn.__name__, args_summary, ok=False,
                        dur_ms=(time.time() - t0) * 1000,
                        error="%s: %s" % (type(e).__name__, e))
            raise
        record_call(fn.__name__, args_summary, ok=True,
                    dur_ms=(time.time() - t0) * 1000,
                    result_summary=_summarize(result, _RESULT_CAP))
        return result

    # FastMCP/pydantic build the tool schema from the signature + annotations; make sure
    # the wrapper exposes the ORIGINAL ones (functools.wraps sets __wrapped__, but we set
    # __signature__ explicitly so inspect.signature(wrapper) is identical to fn's).
    try:
        wrapper.__signature__ = inspect.signature(fn)
    except (ValueError, TypeError):
        pass
    return wrapper


def toolcalls_tail(n=30, day=None):
    """Return the last `n` tool-call records of a day's trace as a readable block. For
    the cockpit / a quick 'what did the agent just do?' check."""
    try:
        p = _tracelog_path(day)
        if not p.is_file():
            return "(no tool-call trace for %s)" % (day or "today")
        lines = p.read_text(encoding="utf-8").splitlines()[-n:]
        out = []
        for ln in lines:
            try:
                r = json.loads(ln)
            except Exception:
                continue
            flag = "ok" if r.get("ok") else "ERR"
            args = ", ".join("%s=%s" % (k, v) for k, v in (r.get("args") or {}).items())
            out.append("[%s] %-18s %5dms  %s%s"
                       % (flag, r.get("name", "?"), r.get("dur_ms", 0), args[:120],
                          ("  -> " + r["error"]) if r.get("error") else ""))
        return "\n".join(out) if out else "(no records)"
    except Exception as e:
        return "[toolcalls_tail error: %s: %s]" % (type(e).__name__, e)
