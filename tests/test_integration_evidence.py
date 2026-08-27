"""The ratchet that asks whether a new capability is connected to anything.

A unit test proves a function does what it says; it does not prove anything calls it. Three
capabilities in one day were complete, tested, green and wired to nothing -- an approval gate
that never ran, a reaper nothing invoked, a resume decision nobody consulted.

The check itself has to be exercised the same way, so these tests drive it over synthetic
trees rather than trusting that it would fire.
"""
import ast
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import check_integration_evidence as chk


def test_a_use_inside_the_defining_module_counts():
    """The question is whether the capability is REACHABLE, not whether it is exported. A
    helper that only main() calls is wired up; demanding an external caller would flag every
    private-by-convention helper in every script."""
    hits = chk.references("route_faulted_since", "scripts/win/tab_audit.py")
    assert any(h[0].endswith("tab_audit.py") for h in hits)


def test_a_name_used_in_a_module_that_never_imports_the_definition_is_not_evidence():
    """THE FALSE PASS THE FIRST VERSION HAD. `git grep -w claim` finds a memory template's
    JSON and half a dozen unrelated modules, and reported the ownership ledger as wired on
    the strength of them. A short, ordinary name always finds a textual hit somewhere, so a
    grep-only check passes hardest exactly where it is needed most."""
    hits = dict(chk.references("claim", "relay/ownership.py"))
    for path in hits:
        if not path.endswith(".py"):
            continue
        src = open(os.path.join(ROOT, path), encoding="utf-8", errors="replace").read()
        assert "ownership" in src, "%s counted without importing the module" % path


def test_conventional_entry_points_are_not_asked_for_evidence():
    """`main` is called by the runtime, `test_*` by pytest, `_x` is private. Each is a
    registration point of a kind no grep can see."""
    for name in ("main", "test_something", "_helper", "handle_get", "cmd_run", "on_close"):
        assert chk.CONVENTIONAL.match(name), name
    for name in ("classify_fallback", "report_status", "reconcile"):
        assert not chk.CONVENTIONAL.match(name), name


def test_the_exceptions_file_is_valid_and_every_entry_carries_a_reason():
    data = json.load(open(os.path.join(ROOT, ".integration_exceptions.json"), encoding="utf-8"))
    for name, reason in data.items():
        assert isinstance(reason, str) and len(reason) > 20, (
            "%s is excused without a reason a reader can check" % name)


def test_it_reports_success_when_a_range_adds_nothing(monkeypatch):
    """An empty diff must not be reported as a problem, or the check becomes noise on every
    commit that only edits existing code.

    The diff is STUBBED rather than taken from the real repository. The first version shelled
    out to git and hung: under pytest the call took over two minutes and `.stdout` came back
    None. What is being tested is the empty-diff branch, not whether git is fast today."""
    monkeypatch.setattr(chk, "_git", lambda *a, **k: "")
    assert chk.added_definitions("HEAD") == {}
    assert chk.main(["--base", "HEAD"]) == 0


def test_git_output_is_always_a_string():
    """`.stdout` came back None under pytest, and `diff.strip()` took the checker down with an
    AttributeError. A check that crashes is a check somebody removes from CI."""
    assert chk._git("rev-parse", "--git-dir") is not None
    assert isinstance(chk._git("no-such-subcommand-zzz"), str)


def test_the_watched_paths_cover_where_capabilities_live():
    """A definition in .fleet/ or a one-off analysis script is not a capability anybody wires
    up; relay, tools and the operational scripts are."""
    assert "relay/" in chk.WATCHED
    assert not any(w.startswith(".fleet") for w in chk.WATCHED)


def test_it_actually_goes_red_on_an_unreferenced_definition(tmp_path, monkeypatch):
    """PROVEN, NOT ASSUMED. A checker that has never failed is a checker nobody has tested;
    this drives the real decision path with a name that exists nowhere in the repository."""
    monkeypatch.setattr(chk, "added_definitions",
                        lambda base: {"a_capability_nobody_wired_up_zzz": "relay/ownership.py"})
    assert chk.main(["--base", "HEAD~1"]) == 1


def test_an_excused_definition_passes(monkeypatch):
    monkeypatch.setattr(chk, "added_definitions",
                        lambda base: {"a_capability_nobody_wired_up_zzz": "relay/ownership.py"})
    monkeypatch.setattr(chk, "load_exceptions",
                        lambda: {"a_capability_nobody_wired_up_zzz": "a reason long enough to read"})
    assert chk.main(["--base", "HEAD~1"]) == 0


def test_an_unresolvable_base_is_announced_not_passed_over_in_silence(capsys):
    """A shallow clone has no HEAD~1. Reporting "no new definitions" then would make the check
    a green tick that means nothing on every CI run -- the exact failure mode it exists to
    prevent, wearing the check's own name."""
    assert chk.main(["--base", "nonexistent-ref-zzz"]) == 0
    out = capsys.readouterr().out
    assert "SKIPPED" in out and "fetch-depth" in out


def test_ci_checks_out_deep_enough_to_have_a_base(capsys):
    """The check reads HEAD~1, and actions/checkout defaults to a depth of one."""
    ci = open(os.path.join(ROOT, ".github", "workflows", "ci.yml"), encoding="utf-8").read()
    i = ci.index("actions/checkout@")
    assert "fetch-depth: 2" in ci[i:i + 400], "the check would skip on every run"


def test_ci_actually_runs_it():
    """A check that is not invoked is the very thing it was built to detect."""
    ci = open(os.path.join(ROOT, ".github", "workflows", "ci.yml"), encoding="utf-8").read()
    assert "scripts/check_integration_evidence.py" in ci
