"""Tests for the Edge auto-recycle decision (reliability under memory pressure).

Run:  .venv\\Scripts\\python.exe relay\\test_recycle.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from relay.edge_recover import companion_edge_mb, should_recycle

results = []


def check(name, cond):
    results.append(bool(cond))
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


def main():
    # bloated Edge -> recycle
    r, why = should_recycle(2000, 4000)
    check("bloat_recycles", r and "Edge" in why)
    # low free RAM -> recycle
    r, why = should_recycle(500, 800)
    check("low_ram_recycles", r and "free RAM" in why)
    # both healthy -> no
    check("healthy_no_recycle", should_recycle(500, 4000)[0] is False)
    # unknown edge size (0) + healthy RAM -> no (don't recycle on no signal)
    check("unknown_edge_no_recycle", should_recycle(0, 4000)[0] is False)
    # custom thresholds honoured
    check("custom_cap", should_recycle(900, 4000, edge_cap_mb=800)[0] is True)
    check("custom_floor", should_recycle(100, 1200, free_floor_mb=1500)[0] is True)

    # companion_edge_mb is env-dependent: must not raise and returns a non-negative float
    mb = companion_edge_mb()
    check("edge_mb_nonneg_float", isinstance(mb, float) and mb >= 0.0)

    print("\n=== %d/%d recycle checks passed ===" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
