"""Soak / chaos harness framework for the M365 hardening program.

Turns the empirical failure catalog (bench/M365_HARDENING_AND_UX.md, F1-F8) into
regression tests: each known failure mode is a Scenario that injects the fault and
asserts the system auto-recovers. "Fixed" is only claimed once soak is green.

SAFETY POSTURE
==============
This module performs NO real chaos by default. A live headless measurement may be
running; touching the live Edge / killing processes / filling disk would corrupt it.

  * RealInjector  -> every method raises NotImplementedError("deferred: ...").
                     The real PowerShell / edge_recover wiring is a POST-MEASUREMENT
                     task. Each method documents its intended real action in a comment.
  * NoopInjector  -> does nothing; exercises the harness control flow safely (dry-run).
  * MockInjector  -> records which methods were called (tests only).

Probes are read-only health checks. Only MockProbe is implemented now; LiveProbe is a
docstring stub that raises NotImplementedError (post-measurement wiring).

The CLI `--live` flag REFUSES to run and exits nonzero. stdlib only, deterministic,
no network, no real subprocess, no time.sleep.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional


# --------------------------------------------------------------------------- #
# 1. Scenario representation
# --------------------------------------------------------------------------- #
@dataclass
class Scenario:
    """One failure mode from the catalog.

    name        human-readable label, e.g. "Edge-memory-wedge"
    failure_id  catalog id, e.g. "F1"
    description short explanation of the incident / what is asserted
    inject      callable(injector) -> performs the fault injection
    recovered   callable(probe) -> bool; True iff the system auto-recovered
    """
    name: str
    failure_id: str
    description: str
    inject: Callable[["Injector"], None]
    recovered: Callable[["Probe"], bool]


# --------------------------------------------------------------------------- #
# 2. Injector interface + three implementations
# --------------------------------------------------------------------------- #
class Injector:
    """Abstract chaos-injection interface (the real-chaos actions).

    Subclasses implement (or stub) each fault. The harness only ever calls these
    through a Scenario.inject, so swapping implementations swaps the danger level.
    """

    def edge_memory_pressure(self) -> None:        # F1
        raise NotImplementedError

    def kill_companion_edge(self) -> None:         # F1 / F7
        raise NotImplementedError

    def fill_disk_near_floor(self) -> None:        # F2
        raise NotImplementedError

    def expire_sso_redirect(self) -> None:         # F4
        raise NotImplementedError

    def wedge_fleet(self) -> None:                 # F7
        raise NotImplementedError

    def spawn_shim_pid_confusion(self) -> None:    # F5
        raise NotImplementedError

    def orphan_detached_child(self) -> None:       # F6
        raise NotImplementedError


class RealInjector(Injector):
    """REAL chaos. DEFERRED — every method raises NotImplementedError.

    Wiring the real actions is a post-measurement task; doing it now risks
    corrupting the live measurement. Each method records its intended real action.
    """

    _DEFERRED = "deferred: do not run real chaos during a live measurement"

    def edge_memory_pressure(self) -> None:
        # REAL: open ~20 heavy django tabs with effort=auto side-pages under load
        # until Edge RSS climbs past the recycle cap (~1.5 GB) on the 16 GB box,
        # so the Edge memory governor must recycle / suppress side-pages.
        raise NotImplementedError(self._DEFERRED)

    def kill_companion_edge(self) -> None:
        # REAL: Stop-Process the copilot-companion-edge instance (own instance only;
        # NEVER Get-Process EXCEL|Stop-Process style broad kills) to simulate a hard
        # crash and assert edge_recover relaunches + re-auths.
        raise NotImplementedError(self._DEFERRED)

    def fill_disk_near_floor(self) -> None:
        # REAL: balloon .fleet/swe/work with blobless clones + worktrees until C: free
        # approaches the 7 GB floor, so disk admission LRU-evicts reconstructable
        # clones and fails soft (skip+retry) instead of aborting the chunk.
        raise NotImplementedError(self._DEFERRED)

    def expire_sso_redirect(self) -> None:
        # REAL: navigate a tab to ?redirfrom=CsrToSSR&auth=2 (the redirect variant that
        # was misread as "SSO expired") to assert the auth-state classifier distinguishes
        # mid-redirect from needs-signin and auto-renavs.
        raise NotImplementedError(self._DEFERRED)

    def wedge_fleet(self) -> None:
        # REAL: freeze fleet status (no-progress > watchdog threshold) to assert
        # watchdog v2 fires the correct escalation rung (renav -> tab-reset ->
        # Edge hard-reset -> clean restart) rather than the single blunt hard-reset.
        raise NotImplementedError(self._DEFERRED)

    def spawn_shim_pid_confusion(self) -> None:
        # REAL: launch via the venv python.exe shim (shim pid != real pid) so a naive
        # tasklist /FI "PID eq" reports false "process died"; assert guards.proc_alive
        # (CIM/psutil cmdline match) is used everywhere liveness is checked.
        raise NotImplementedError(self._DEFERRED)

    def orphan_detached_child(self) -> None:
        # REAL: spawn a nested detached child of an already-detached driver to reproduce
        # the mid-run reap; assert guards.launch_detached for top-level + blocking
        # children otherwise keeps the child alive.
        raise NotImplementedError(self._DEFERRED)


class NoopInjector(Injector):
    """Does nothing. Safe dry-run that still exercises the harness control flow."""

    def edge_memory_pressure(self) -> None:
        pass

    def kill_companion_edge(self) -> None:
        pass

    def fill_disk_near_floor(self) -> None:
        pass

    def expire_sso_redirect(self) -> None:
        pass

    def wedge_fleet(self) -> None:
        pass

    def spawn_shim_pid_confusion(self) -> None:
        pass

    def orphan_detached_child(self) -> None:
        pass


class MockInjector(Injector):
    """Records which methods were called (for tests). No side effects."""

    def __init__(self) -> None:
        self.calls: List[str] = []

    def edge_memory_pressure(self) -> None:
        self.calls.append("edge_memory_pressure")

    def kill_companion_edge(self) -> None:
        self.calls.append("kill_companion_edge")

    def fill_disk_near_floor(self) -> None:
        self.calls.append("fill_disk_near_floor")

    def expire_sso_redirect(self) -> None:
        self.calls.append("expire_sso_redirect")

    def wedge_fleet(self) -> None:
        self.calls.append("wedge_fleet")

    def spawn_shim_pid_confusion(self) -> None:
        self.calls.append("spawn_shim_pid_confusion")

    def orphan_detached_child(self) -> None:
        self.calls.append("orphan_detached_child")


# --------------------------------------------------------------------------- #
# 3. Probe interface + MockProbe (+ LiveProbe stub)
# --------------------------------------------------------------------------- #
class Probe:
    """Abstract read-only health-check interface used to assert recovery.

    Predicates return True when the corresponding subsystem is healthy.
    """

    def edge_mem_ok(self) -> bool:         # Edge RSS back under the recycle cap (F1)
        raise NotImplementedError

    def cdp_alive(self) -> bool:           # CDP / Edge endpoint responding (F1/F7)
        raise NotImplementedError

    def auth_ready(self) -> bool:          # authed-chat-ready, not mid-redirect (F4)
        raise NotImplementedError

    def disk_ok(self) -> bool:             # C: free back above the floor (F2)
        raise NotImplementedError

    def fleet_progressing(self) -> bool:   # worker status advancing, not frozen (F7)
        raise NotImplementedError


class MockProbe(Probe):
    """Returns configured booleans. Defaults to all-healthy.

    Usage: MockProbe()                       -> everything healthy
           MockProbe(auth_ready=False)       -> auth still broken
    """

    def __init__(
        self,
        edge_mem_ok: bool = True,
        cdp_alive: bool = True,
        auth_ready: bool = True,
        disk_ok: bool = True,
        fleet_progressing: bool = True,
    ) -> None:
        self.edge_mem = edge_mem_ok
        self.cdp = cdp_alive
        self.auth = auth_ready
        self.disk = disk_ok
        self.fleet = fleet_progressing

    def edge_mem_ok(self) -> bool:
        return self.edge_mem

    def cdp_alive(self) -> bool:
        return self.cdp

    def auth_ready(self) -> bool:
        return self.auth

    def disk_ok(self) -> bool:
        return self.disk

    def fleet_progressing(self) -> bool:
        return self.fleet


class LiveProbe(Probe):
    """DEFERRED stub. Post-measurement, this will back the predicates with real
    health signals: edge_auth.classify_live for auth_ready, the cockpit /
    .fleet/status.json for fleet_progressing, psutil RSS for edge_mem_ok, a CDP
    ping for cdp_alive, and a free-space check for disk_ok. Not implemented now."""

    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError(
            "deferred: LiveProbe wiring is a post-measurement task"
        )


# --------------------------------------------------------------------------- #
# 4. SCENARIOS registry
# --------------------------------------------------------------------------- #
SCENARIOS: Dict[str, Scenario] = {
    "F1": Scenario(
        name="Edge-memory-wedge",
        failure_id="F1",
        description="Edge balloons under load -> no-progress -> watchdog re-wedge loop; "
                    "governor must recycle and throughput must hold.",
        inject=lambda inj: inj.edge_memory_pressure(),
        recovered=lambda p: p.edge_mem_ok() and p.fleet_progressing(),
    ),
    "F2": Scenario(
        name="disk-floor",
        failure_id="F2",
        description="C: free hits the floor from unbounded work/ clones; disk admission "
                    "must LRU-evict and the run must continue (fail-soft).",
        inject=lambda inj: inj.fill_disk_near_floor(),
        recovered=lambda p: p.disk_ok(),
    ),
    "F4": Scenario(
        name="SSO-redirect-misread",
        failure_id="F4",
        description="?redirfrom=CsrToSSR&auth=2 tab misread as 'SSO expired'; classifier "
                    "must distinguish mid-redirect and auto-renav to authed-chat-ready.",
        inject=lambda inj: inj.expire_sso_redirect(),
        recovered=lambda p: p.auth_ready(),
    ),
    "F5": Scenario(
        name="shim-pid-false-death",
        failure_id="F5",
        description="venv python.exe shim pid != real pid; PID-filter liveness reports "
                    "false death. guards.proc_alive must report the process alive.",
        inject=lambda inj: inj.spawn_shim_pid_confusion(),
        recovered=lambda p: p.cdp_alive(),
    ),
    "F6": Scenario(
        name="nested-detach-orphan",
        failure_id="F6",
        description="A detached child of an already-detached driver gets reaped mid-run; "
                    "launch_detached / blocking-children must keep the worker progressing.",
        inject=lambda inj: inj.orphan_detached_child(),
        recovered=lambda p: p.fleet_progressing(),
    ),
    "F7": Scenario(
        name="watchdog-wedge-loop",
        failure_id="F7",
        description="True wedge (status frozen); watchdog v2 escalation ladder must "
                    "recover Edge + restore progress rather than blunt re-wedge.",
        inject=lambda inj: inj.wedge_fleet(),
        recovered=lambda p: p.cdp_alive() and p.fleet_progressing(),
    ),
}


# --------------------------------------------------------------------------- #
# 5. run_scenario / run_suite
# --------------------------------------------------------------------------- #
def run_scenario(
    scenario: Scenario,
    injector: Injector,
    probe: Probe,
    settle_fn: Optional[Callable[[], None]] = None,
) -> Dict[str, object]:
    """Inject one fault and check recovery. Pure control flow — no real time.sleep.

    Returns {"name","failure_id","injected": bool, "recovered": bool|None, "error": str|None}.

    If injection raises NotImplementedError (RealInjector / deferred chaos), the error
    is recorded, injected=False, and recovery is skipped (recovered=None) — never a
    real action. An optional settle_fn (a no-op in tests) is called between inject and
    the recovery check for cases where a settle delay is conceptually needed.
    """
    result: Dict[str, object] = {
        "name": scenario.name,
        "failure_id": scenario.failure_id,
        "injected": False,
        "recovered": None,
        "error": None,
    }
    try:
        scenario.inject(injector)
        result["injected"] = True
    except NotImplementedError as exc:
        result["error"] = str(exc)
        return result

    if settle_fn is not None:
        settle_fn()

    result["recovered"] = bool(scenario.recovered(probe))
    return result


def run_suite(
    scenarios,
    injector: Injector,
    probe: Probe,
    settle_fn: Optional[Callable[[], None]] = None,
) -> Dict[str, object]:
    """Run a collection of scenarios. `scenarios` may be a dict (values used) or a list.

    Returns {"total","recovered","failed","results":[...]}. A scenario counts as
    recovered only when result["recovered"] is True; everything else (False or None)
    counts as failed.
    """
    items = list(scenarios.values()) if isinstance(scenarios, dict) else list(scenarios)
    results = [run_scenario(s, injector, probe, settle_fn=settle_fn) for s in items]
    recovered = sum(1 for r in results if r["recovered"] is True)
    return {
        "total": len(results),
        "recovered": recovered,
        "failed": len(results) - recovered,
        "results": results,
    }


# --------------------------------------------------------------------------- #
# 6. CLI
# --------------------------------------------------------------------------- #
_LIVE_REFUSAL = (
    "refusing: real chaos is deferred and must never run during a live measurement; "
    "RealInjector methods are not implemented"
)


def _print_list() -> None:
    for fid in sorted(SCENARIOS):
        s = SCENARIOS[fid]
        print("%-3s %-22s %s" % (s.failure_id, s.name, s.description))


def _print_summary(summary: Dict[str, object]) -> None:
    print("soak suite: %d total, %d recovered, %d failed"
          % (summary["total"], summary["recovered"], summary["failed"]))
    for r in summary["results"]:
        if r["recovered"] is True:
            status = "RECOVERED"
        elif r["recovered"] is False:
            status = "NOT-RECOVERED"
        else:
            status = "SKIPPED(%s)" % (r["error"] or "no-inject")
        print("  %-3s %-22s %s" % (r["failure_id"], r["name"], status))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m relay.soak",
        description="Soak/chaos harness for the M365 hardening failure catalog (F1-F8).",
    )
    parser.add_argument("--list", action="store_true",
                        help="print the scenario registry and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="run the suite with NoopInjector + all-healthy MockProbe (default)")
    parser.add_argument("--live", action="store_true",
                        help="REFUSED: real chaos is deferred; this never runs real chaos")
    args = parser.parse_args(argv)

    if args.list:
        _print_list()
        return 0

    if args.live:
        print(_LIVE_REFUSAL, file=sys.stderr)
        return 2

    # default and --dry-run: safe harness-wiring exercise, no real chaos.
    summary = run_suite(SCENARIOS, NoopInjector(), MockProbe())
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
