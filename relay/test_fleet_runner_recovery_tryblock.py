# Regression test for the 2026-11-09 22:37 fleet crash.
#
# WHAT BROKE: in run_fleet's `while pending:` recovery loop, the pre-connect steps
# -- the cdp_alive probe and the edge-unreachable `hard_reset` -- sat OUTSIDE the
# try: that wraps the reconnect. Both run under _with_deadline(), which raises
# FleetRecoveryTimeout when a step blows its 60s ceiling. Because they were outside
# the try, that timeout was caught by NO handler: it propagated out of the loop and
# killed main(). Observed live: hard_reset(port) timed out, FleetRecoveryTimeout
# escaped, the coordinator process died mid-recovery, status.json froze, the attempt
# was never counted, and attempt 3/3 was never issued. A sibling process had to pick
# the goals back up.
#
# THE CONTRACT (see FleetRecoveryTimeout and _with_deadline docstrings in
# fleet_runner.py): a blown recovery-step deadline must be answered by the recovery
# loop's generic `except Exception` -- attempt counted, conditional hard reset,
# retried through the ordinary path -- NOT allowed to escape.
#
# This test asserts that structurally, with no browser: the _with_deadline(...,
# "hard_reset") call in the recovery loop must live inside a try whose handlers
# include `except Exception` (and FleetContextLost). If someone moves it back out,
# COUNT drops to 0 and this fails.

import ast
import os

results = []


def check(name, cond):
    results.append(bool(cond))
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


def _handler_names(try_node):
    names = set()
    for h in try_node.handlers:
        if h.type is None:
            names.add("<bare>")
        elif isinstance(h.type, ast.Name):
            names.add(h.type.id)
    return names


def _body_calls_hard_reset_deadline(try_node):
    # Walk ONLY the try body (not its handlers/else/finally): we want the hard_reset
    # to be a guarded statement, not something a handler happens to call.
    for stmt in try_node.body:
        for n in ast.walk(stmt):
            if (isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name)
                    and n.func.id == "_with_deadline"
                    and len(n.args) >= 3
                    and isinstance(n.args[2], ast.Constant)
                    and n.args[2].value == "hard_reset"):
                return True
    return False


def _find_guarded_recovery_tries(src):
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            hn = _handler_names(node)
            if "Exception" in hn and _body_calls_hard_reset_deadline(node):
                out.append((node.lineno, sorted(hn)))
    return out


def test_hard_reset_is_inside_recovery_try():
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "fleet_runner.py"), encoding="utf-8").read()
    guarded = _find_guarded_recovery_tries(src)
    check(
        "edge-unreachable hard_reset runs inside a try caught by `except Exception`",
        len(guarded) >= 1,
    )
    if guarded:
        _lineno, handlers = guarded[0]
        check(
            "that guarding try also handles FleetContextLost (ordinary recovery path)",
            "FleetContextLost" in handlers,
        )


def test_fleet_recovery_timeout_is_an_exception_subclass():
    # The whole fix relies on FleetRecoveryTimeout being catchable by `except
    # Exception`. If it is ever reparented to BaseException, the generic handler
    # stops catching it and the crash returns by a different door.
    from relay.fleet_runner import FleetRecoveryTimeout
    check(
        "FleetRecoveryTimeout is a subclass of Exception",
        issubclass(FleetRecoveryTimeout, Exception),
    )


def main():
    test_hard_reset_is_inside_recovery_try()
    test_fleet_recovery_timeout_is_an_exception_subclass()
    passed = sum(results)
    total = len(results)
    print("\n=== %d/%d recovery-tryblock checks passed ===" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
