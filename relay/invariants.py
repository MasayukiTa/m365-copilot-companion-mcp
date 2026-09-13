# -*- coding: utf-8 -*-
"""Post-conditions that are checked when the code runs, not when somebody remembers to look.

WHERE THIS COMES FROM. An external harness (DeepSeek's `dsh`,
`packages/runtime-diagnostics/invariants/src/index.ts`) has each package register its own
invariants and raise, at runtime, with a machine-readable code and the name of the package that
was violated. Its own analysis of this repository named the contrast precisely: our baselines
turn a broken state into a KNOWN state and then leave it green, and an invariant is the opposite
-- it is verified on every execution and says so out loud the moment it stops holding. Its port
note asked for nothing fancy: "a thin layer that asserts post-conditions on important paths and
raises a machine-readable code -- an assert_invariant(name, cond, msg) minimum is enough."

WHAT IS DIFFERENT HERE, AND WHY. dsh raises on every violation. In this system a raise on the
fleet's hot path costs a fifty-minute run, and "telemetry must not be able to fail a run" is a
rule this repository already paid for. So a violation has TWO dispositions, declared per
invariant and never decided at the call site:

    RAISE   the caller's answer would be wrong, and a wrong answer is worse than a stop.
            The reconciler reading 2 transcripts out of 1557 and reporting on them is the
            case this was written for.
    RECORD  the caller is on a recovery path where stopping is the worse outcome. The
            violation is written to the ledger and the caller carries on.

A RECORD-mode invariant is not a weaker RAISE. It is the honest encoding of a system where
some failures must not propagate, and it still ends the silence, which is the whole point:
before this, a broken post-condition left nothing behind at all.

THE LIMIT, STATED BY THE SOURCE AND WORTH REPEATING: "if nobody writes the invariants, nothing
is protected". This is not a general impossibility proof. It makes the assertions somebody
actually wrote hold at runtime, and `test_an_invariant_has_a_call_site.py` makes sure a
registered one is not another layer with no caller.
"""
from __future__ import annotations

import io
import json
import os
import re
import time

#: Where violations are written. Readable after the fact, unlike a raise nobody caught.
LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   ".fleet", "invariants.jsonl")

RAISE = "raise"
RECORD = "record"

#: name -> {"owner", "disposition", "why"}. Declared up front so the set of promises this
#: repository makes at runtime can be listed, and so a promise nobody checks is findable.
REGISTRY: dict[str, dict] = {}


class InvariantViolated(AssertionError):
    """Carries the machine-readable code and the owner, like the design it is ported from.

    AssertionError rather than a bare Exception so an `except Exception` on a recovery path
    still catches it -- the alternative is an invariant that can kill a run through a handler
    written years before it existed.
    """

    code = "INVARIANT"

    def __init__(self, owner: str, name: str, message: str):
        self.owner = owner
        self.name = name
        super().__init__("[INVARIANT] %s (%s): %s" % (name, owner, message))


def register(name: str, owner: str, disposition: str, why: str) -> str:
    """Declare an invariant. Returns `name` so a module can register at import and keep the
    handle in one statement."""
    if disposition not in (RAISE, RECORD):
        raise ValueError("disposition must be %r or %r" % (RAISE, RECORD))
    if not (why or "").strip():
        raise ValueError("an invariant with no stated reason is a line of code, not a promise")
    REGISTRY[name] = {"owner": owner, "disposition": disposition, "why": why}
    return name


def enabled(name: str) -> bool:
    """MCP_INVARIANTS_OFF is a regex of names to skip; MCP_INVARIANTS, when set, is a regex
    that a name must match. Default is on, because an invariant that ships off is a comment."""
    off = os.environ.get("MCP_INVARIANTS_OFF", "").strip()
    if off and re.search(off, name):
        return False
    only = os.environ.get("MCP_INVARIANTS", "").strip()
    return bool(re.search(only, name)) if only else True


def _write(row: dict) -> None:
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with io.open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def assert_invariant(name: str, condition, message: str, **context) -> bool:
    """Assert a registered post-condition. True when it holds.

    Raises InvariantViolated for a RAISE invariant; returns False for a RECORD one. Both write
    a row first, so a violation is on disk whether or not anything catches the exception.
    """
    entry = REGISTRY.get(name)
    if entry is None:
        raise KeyError("invariant %r is not registered; declare it where it is checked" % name)
    if bool(condition) or not enabled(name):
        return True
    _write({"ts": time.time(), "code": InvariantViolated.code, "invariant": name,
            "owner": entry["owner"], "disposition": entry["disposition"],
            "message": message, "context": context or None})
    if entry["disposition"] == RAISE:
        raise InvariantViolated(entry["owner"], name, message)
    print("[INVARIANT] %s (%s): %s" % (name, entry["owner"], message), flush=True)
    return False


def violations(path: str = None) -> list[dict]:
    """Every violation written so far. [] when there are none -- the expected state."""
    p = path or LOG
    out = []
    try:
        for line in io.open(p, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return out
    return out


def main(argv=None) -> int:
    """`python -m relay.invariants [list|violations]` -- what this repository promises at
    runtime, and what has stopped being true."""
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", nargs="?", default="list", choices=("list", "violations"))
    args = ap.parse_args(argv)

    if args.cmd == "list":
        # Importing the modules that register is the only way to enumerate them, which is also
        # the reason registration happens at import rather than at first check.
        import relay.fleet_reconcile  # noqa: F401
        import relay.relay_fleet      # noqa: F401

        if not REGISTRY:
            print("no invariants registered")
            return 0
        for name in sorted(REGISTRY):
            e = REGISTRY[name]
            print("%-46s %-7s %s" % (name, e["disposition"], e["owner"]))
        return 0

    rows = violations()
    if not rows:
        print("no violations recorded")
        return 0
    for r in rows:
        print("%s %-46s %s" % (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r.get("ts", 0))),
            r.get("invariant"), r.get("message")))
    return 0


if __name__ == "__main__":
    # THROUGH THE PACKAGE, NOT THROUGH THIS COPY. `python -m relay.invariants` executes this
    # file as `__main__`, so every module that registers via `from relay import invariants`
    # imports a SECOND copy with its own REGISTRY -- and the copy doing the printing is the
    # empty one. Measured: `list` said "no invariants registered" with two registered. Calling
    # the package's own main makes the registry that the rest of the process shares the one
    # being read.
    from relay.invariants import main as _packaged_main

    raise SystemExit(_packaged_main())
