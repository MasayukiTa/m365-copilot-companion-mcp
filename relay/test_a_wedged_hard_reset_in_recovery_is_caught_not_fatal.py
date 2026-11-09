# -*- coding: utf-8 -*-
"""A hard reset that blows its deadline during recovery must be CAUGHT and retried, never kill
the fleet process.

THE BUG THIS PINS (observed 2026-11-09, coordinator_20261109_183127_p35696.log).
The resume loop in relay/fleet_runner.main() recovers a lost Edge leg by leg inside
``while pending:``. One recovery step -- the pre-connect "Edge unreachable -> hard reset" --
ran its ``_with_deadline(lambda: hard_reset(port), ..., "hard_reset")`` call ABOVE the leg's
``try``. ``_with_deadline`` raises ``FleetRecoveryTimeout`` when the wrapped call does not
return within its 60 s ceiling, and a genuinely wedged browser can make the hard reset itself
blow that ceiling. Because the call sat outside the ``try``, that exception was caught by
NOTHING in the recover loop: it tore through ``main()`` to ``SystemExit`` and the whole fleet
process died mid-recovery.

The log shows exactly that sequence:

    22:35:15 [recover] Edge context lost; resuming 30 goal(s) (attempt 2/3)
    22:37:17 [recover] Edge unreachable -> hard reset before (re)connecting
    FleetRecoveryTimeout: recovery step 'hard_reset' did not return within 60s

30 goals were abandoned, and the loop never reached attempt 3 -- the process was gone.
``FleetRecoveryTimeout``'s own docstring promises it is "counted as a failed attempt and
RETRIED through the ordinary reconnect path"; that promise only holds if the call that raises
it is INSIDE a ``try`` whose handler does the attempt-counting.

WHAT IS PINNED. By static analysis of relay/fleet_runner.py (no Edge, no Playwright, no
Windows-only calls -- runs on the Linux CI):

  1. Every ``_with_deadline(..., "hard_reset")`` call inside ``main()`` is lexically nested
     inside a ``try`` statement -- i.e. a blown hard-reset deadline is catchable, not fatal.
  2. At least one such enclosing ``try`` has a handler that catches a base ``Exception`` (the
     generic recover handler that calls ``_after_failed_leg`` and backs off), so
     ``FleetRecoveryTimeout`` (a subclass of ``Exception``) is actually caught there.

A pure AST check is the honest test here: the regression is a control-flow/placement bug, and
wiring a real wedged hard reset would need a real browser. The check reads the source the CI
actually ships.
"""
from __future__ import annotations

import ast
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

SOURCE = os.path.join(REPO, "relay", "fleet_runner.py")


def _main_func(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node
    raise AssertionError("relay/fleet_runner.py has no top-level main()")


def _is_hard_reset_deadline_call(node):
    """True for a call shaped like _with_deadline(<fn>, <deadline>, "hard_reset")."""
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    if not (isinstance(fn, ast.Name) and fn.id == "_with_deadline"):
        return False
    # The label is the last positional argument, a constant string.
    for arg in node.args:
        if isinstance(arg, ast.Constant) and arg.value == "hard_reset":
            return True
    return False


def _tries_enclosing(func):
    """Map every node to the list of ast.Try nodes that lexically enclose it, within func."""
    enclosing = {}

    def walk(node, stack):
        for child in ast.iter_child_nodes(node):
            enclosing[child] = list(stack)
            if isinstance(child, ast.Try):
                walk(child, stack + [child])
            else:
                walk(child, stack)

    walk(func, [])
    return enclosing


def _handler_catches_exception(try_node):
    """True if the Try has an `except Exception` (or bare except) handler."""
    for h in try_node.handlers:
        if h.type is None:
            return True
        if isinstance(h.type, ast.Name) and h.type.id in ("Exception", "BaseException"):
            return True
        if isinstance(h.type, ast.Tuple):
            for elt in h.type.elts:
                if isinstance(elt, ast.Name) and elt.id in ("Exception", "BaseException"):
                    return True
    return False


def _load_main():
    with open(SOURCE, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=SOURCE)
    return _main_func(tree)


def test_every_hard_reset_deadline_in_recovery_is_inside_a_try():
    """No _with_deadline(..., 'hard_reset') in main() may sit outside a try -- outside a try it
    is fatal, which is exactly the 2026-11-09 process death."""
    main = _load_main()
    enclosing = _tries_enclosing(main)

    hard_resets = [n for n in ast.walk(main) if _is_hard_reset_deadline_call(n)]
    assert hard_resets, (
        "expected at least one _with_deadline(..., 'hard_reset') call in main(); "
        "the recovery hard-reset step was not found -- did the loop change shape?"
    )

    for call in hard_resets:
        tries = enclosing.get(call, [])
        assert tries, (
            "a _with_deadline(..., 'hard_reset') call in main() is NOT inside any try: "
            "a blown hard-reset deadline there raises FleetRecoveryTimeout with no handler "
            "and kills the fleet process (the 2026-11-09 22:37:17 death). Line %d."
            % getattr(call, "lineno", -1)
        )


def test_a_hard_reset_deadline_is_caught_by_a_generic_exception_handler():
    """Being inside *some* try is not enough: one enclosing try must catch Exception, so
    FleetRecoveryTimeout (an Exception subclass) is actually handled and the leg is retried."""
    main = _load_main()
    enclosing = _tries_enclosing(main)
    hard_resets = [n for n in ast.walk(main) if _is_hard_reset_deadline_call(n)]
    assert hard_resets, "no _with_deadline(..., 'hard_reset') call found in main()"

    for call in hard_resets:
        tries = enclosing.get(call, [])
        assert any(_handler_catches_exception(t) for t in tries), (
            "the recovery hard-reset at line %d is inside a try, but no enclosing try catches "
            "Exception -- FleetRecoveryTimeout would escape uncaught and kill the process. "
            "It must land in the generic recover handler that counts a failed leg and backs "
            "off." % getattr(call, "lineno", -1)
        )


if __name__ == "__main__":
    test_every_hard_reset_deadline_in_recovery_is_inside_a_try()
    test_a_hard_reset_deadline_is_caught_by_a_generic_exception_handler()
    print("PASS: recovery hard-reset deadline is caught, not fatal")
