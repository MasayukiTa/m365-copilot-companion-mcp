"""Tests for ram_target_cap -- the RAM-aware dynamic concurrency target. Monkeypatches the
free-RAM reading so the up-ramp / soft-drain / clamp behavior is deterministic.

Run:  .venv\\Scripts\\python.exe relay\\test_autoscale.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import relay.relay_fleet as rf

results = []


def check(name, cond):
    results.append(bool(cond))
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


def with_ram(mb):
    rf.avail_phys_mb = lambda: float(mb)


def main():
    cap = rf.ram_target_cap

    # 1. plenty of RAM, but ramp up only ONE per call (never jump to the ceiling)
    with_ram(6000)
    check("ramp_up_by_one", cap(1, 1, 4) == 2)
    check("ramp_up_next", cap(2, 2, 4) == 3)
    check("ramp_up_to_ceiling", cap(3, 3, 4) == 4)
    check("never_exceed_ceiling", cap(4, 4, 4) == 4)

    # 2. steady state: enough RAM to hold current, not enough to add -> stays put
    with_ram(1700)            # (1700-1400)//700 = 0 -> raw == open_now
    check("hold_when_tight", cap(3, 3, 4) == 3)

    # 3. RAM deficit -> target drops below open_now (soft drain target)
    with_ram(1000)            # (1000-1400)//700 = -1 -> raw = open_now-1
    check("drain_small_deficit", cap(3, 3, 4) == 2)
    with_ram(300)             # (300-1400)//700 = -2 -> raw = open_now-2
    check("drain_larger_deficit", cap(3, 3, 4) == 1)

    # 4. floor is respected (never below 1)
    with_ram(100)
    check("floor_one", cap(1, 1, 4) == 1)
    check("floor_from_high_open", cap(2, 2, 4) >= 1 and cap(2, 2, 4) <= 2)

    # 5. ceiling clamps even with huge RAM and a single ramp step
    with_ram(64000)
    check("single_step_despite_huge_ram", cap(1, 1, 8) == 2)
    check("ceiling_caps_ramp", cap(7, 7, 8) == 8 and cap(8, 8, 8) == 8)

    # 6. down is immediate (not rate-limited like up): big deficit drops multiple at once
    with_ram(0)               # (0-1400)//700 = -2
    check("down_is_immediate", cap(4, 4, 8) == 2)

    print("\n=== %d/%d autoscale checks passed ===" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
