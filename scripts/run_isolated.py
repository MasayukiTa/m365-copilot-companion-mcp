# -*- coding: utf-8 -*-
"""Run one script-style suite with the live-record redirection pytest tests already get.

THE HOLE. `relay/test_live_record_isolation.py` exists so a test can never write into a place
where the operator's records accumulate, and conftest's autouse fixture is what enforces it.
`scripts/run_script_style_tests.py` runs twenty files that pytest collects nothing from -- as
plain scripts, in their own interpreter -- so none of that applies to them. Every record module
those suites touch has been writing to the real `.fleet` for as long as they have existed.

MEASURED 2026-09-13. A preflight run appended 67 rows to the operator's
`.fleet/mechanisms.jsonl`, each a per-goal fan-out judgement with an empty run_id, timestamped
inside the script-style phase. They became visible that day only because the call site that
writes them had been raising NameError into a bare except until an hour earlier; the exposure is
as old as the runner.

ONE TABLE, NOT TWO. The redirection is applied from `conftest.LIVE_RECORD_REDIRECTS` itself
rather than re-listed here. A second copy of that list would drift from the first, and the whole
point of that table is that it is the one place a new shared record has to be classified.

WHAT THIS DOES NOT COVER: the same caveat conftest carries. A path assembled at call time, or
built from a name the table does not know, is not redirected here either.
"""
from __future__ import annotations

import importlib
import os
import runpy
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def apply_redirects(base: Path) -> int:
    """Point every constant in conftest's table at `base`. Returns how many were moved."""
    sys.path.insert(0, str(ROOT))
    import conftest as C

    base.mkdir(parents=True, exist_ok=True)
    moved = 0
    for module_path, consts in C.LIVE_RECORD_REDIRECTS.items():
        if module_path in C.ONLY_IF_ALREADY_IMPORTED and module_path not in sys.modules:
            # Same reasoning as conftest's: a suite that never imports it cannot write through
            # it, and importing it here would cost every run.
            continue
        try:
            mod = importlib.import_module(module_path)
        except Exception:
            continue
        for const, filename in consts.items():
            try:
                current = getattr(mod, const, None)
                target = base / filename
                # Match the type the module already stores: a module holding a Path and handed a
                # str breaks on `.parent`, and the reverse breaks on concatenation.
                value = Path(str(target)) if isinstance(current, Path) else str(target)
                setattr(mod, const, value)
                moved += 1
            except Exception:
                pass
    # The variable task_router resolves at call time, which is why it is exempt from the table
    # rather than listed in it. conftest sets it for the same reason.
    os.environ.setdefault("FLEET_STATE_DIR", str(base / "fleet_state"))
    return moved


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python scripts/run_isolated.py <script.py> [args...]", file=sys.stderr)
        return 2
    script = argv[0]
    base = Path(tempfile.mkdtemp(prefix="live_records_"))
    apply_redirects(base)
    sys.argv = [script] + argv[1:]
    # run_name="__main__" so a suite's `if __name__ == "__main__":` block runs, and its
    # sys.exit propagates as this process's exit code -- which is the whole result the runner
    # reads.
    runpy.run_path(script, run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
