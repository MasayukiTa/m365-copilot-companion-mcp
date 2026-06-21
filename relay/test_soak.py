"""Hermetic unit tests for the soak/chaos harness. NO real chaos.

Run: python -m relay.test_soak

Mirrors relay/selfimprove/test_guards.py style. Uses only MockInjector + MockProbe
(and RealInjector to prove deferred chaos is caught, never executed). No live Edge,
no process kills, no disk, no network, no sleep.
"""
from relay import soak as S


def test_run_scenario_recovered():
    inj = S.MockInjector()
    probe = S.MockProbe()                                  # all healthy
    res = S.run_scenario(S.SCENARIOS["F1"], inj, probe)
    assert res["injected"] is True
    assert res["recovered"] is True
    assert res["error"] is None
    assert res["failure_id"] == "F1"
    assert inj.calls == ["edge_memory_pressure"]           # the matching method was recorded
    print("ok test_run_scenario_recovered")


def test_run_scenario_not_recovered():
    inj = S.MockInjector()
    # F4 recovery == probe.auth_ready(); report auth still broken
    probe = S.MockProbe(auth_ready=False)
    res = S.run_scenario(S.SCENARIOS["F4"], inj, probe)
    assert res["injected"] is True
    assert res["recovered"] is False
    assert res["error"] is None
    assert inj.calls == ["expire_sso_redirect"]
    print("ok test_run_scenario_not_recovered")


def test_run_scenario_real_injector_deferred():
    real = S.RealInjector()
    probe = S.MockProbe()
    res = S.run_scenario(S.SCENARIOS["F1"], real, probe)
    assert res["injected"] is False                        # NotImplementedError caught
    assert res["recovered"] is None                        # recovery check skipped
    assert res["error"] is not None and "deferred" in res["error"]
    print("ok test_run_scenario_real_injector_deferred")


def test_settle_fn_called_on_inject():
    inj = S.MockInjector()
    probe = S.MockProbe()
    ticks = []
    res = S.run_scenario(S.SCENARIOS["F2"], inj, probe, settle_fn=lambda: ticks.append(1))
    assert res["recovered"] is True
    assert ticks == [1]                                    # settle_fn ran once after inject
    # but NOT when injection is deferred (no real action -> no settle)
    real = S.RealInjector()
    ticks2 = []
    S.run_scenario(S.SCENARIOS["F2"], real, probe, settle_fn=lambda: ticks2.append(1))
    assert ticks2 == []
    print("ok test_settle_fn_called_on_inject")


def test_run_suite_mixed():
    inj = S.MockInjector()
    # F2 recovers (disk_ok default True); F4 does NOT (auth broken). Others healthy.
    probe = S.MockProbe(auth_ready=False)
    summary = S.run_suite(S.SCENARIOS, inj, probe)
    assert summary["total"] == len(S.SCENARIOS)
    assert summary["failed"] == 1                          # only F4 fails
    assert summary["recovered"] == summary["total"] - 1
    failed_ids = [r["failure_id"] for r in summary["results"] if r["recovered"] is not True]
    assert failed_ids == ["F4"]
    print("ok test_run_suite_mixed")


def test_run_suite_real_injector_all_skipped():
    real = S.RealInjector()
    probe = S.MockProbe()
    summary = S.run_suite(S.SCENARIOS, real, probe)
    assert summary["recovered"] == 0                       # nothing injected -> nothing recovered
    assert summary["failed"] == summary["total"]
    assert all(r["injected"] is False for r in summary["results"])
    assert all("deferred" in r["error"] for r in summary["results"])
    print("ok test_run_suite_real_injector_all_skipped")


def test_registry_has_catalog():
    for fid in ("F1", "F2", "F4", "F5", "F6", "F7"):
        assert fid in S.SCENARIOS
        assert S.SCENARIOS[fid].failure_id == fid
        assert callable(S.SCENARIOS[fid].inject)
        assert callable(S.SCENARIOS[fid].recovered)
    print("ok test_registry_has_catalog")


def test_noop_injector_safe():
    # NoopInjector does nothing yet the suite runs and reports all-healthy recovery.
    summary = S.run_suite(S.SCENARIOS, S.NoopInjector(), S.MockProbe())
    assert summary["failed"] == 0 and summary["recovered"] == summary["total"]
    print("ok test_noop_injector_safe")


def test_cli_live_refused():
    # --live must refuse and exit nonzero, never invoking real chaos.
    assert S.main(["--live"]) != 0
    assert S.main(["--dry-run"]) == 0
    assert S.main(["--list"]) == 0
    print("ok test_cli_live_refused")


if __name__ == "__main__":
    test_run_scenario_recovered()
    test_run_scenario_not_recovered()
    test_run_scenario_real_injector_deferred()
    test_settle_fn_called_on_inject()
    test_run_suite_mixed()
    test_run_suite_real_injector_all_skipped()
    test_registry_has_catalog()
    test_noop_injector_safe()
    test_cli_live_refused()
    print("ALL SOAK TESTS PASSED")
