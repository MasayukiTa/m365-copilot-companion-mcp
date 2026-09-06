# -*- coding: utf-8 -*-
"""Every shared .fleet record is either redirected for tests, or someone decided it need not be.

FIVE TIMES, THE SAME WAY. A new shared file appears under .fleet; the tests around the code
that writes it have no reason to know it is the operator's; they fill it. The routing record
got route_closed events for a route that never closed. The refusal log got 117 lines from
203.0.113.7, a documentation address no backend has ever called from. Then the pending queue,
then the summary cache. Each was fixed afterwards by adding a line to conftest.py.

The fifth reached a screen. A test driving capture_floor with a stub context wrote

    {"ok": false, "kind": "other",
     "reason": "AttributeError: 'FakeContext' object has no attribute 'new_page'"}

into .fleet/capture_status.json -- the only file the cockpit's sign-in dot reads -- so a test
run moved a live health indicator while nothing was wrong with sign-in. A false alarm from a
test is worse than a missing signal: it spends the trust the indicator exists to earn.

A HAND-MAINTAINED ALLOWLIST FAILS OPEN, which is the shape of all five: the sixth file will be
protected only if somebody remembers. This test removes the remembering. It walks the source
for module-level constants that build a path under .fleet and requires each to appear in
conftest's LIVE_RECORD_REDIRECTS or in DELIBERATELY_NOT_REDIRECTED with a reason. A new one
fails here until it is classified, which turns the silence into a decision.

It cannot prove a redirected constant is the only way a module writes -- a function that builds
its own path is out of reach of any static check. What it removes is the failure that actually
happened five times: a shared record nobody thought about.
"""
import ast
import io
import os

import pytest

import conftest as C

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGES = ("relay", "tools", "bridge")


def _module_path(file_path):
    rel = os.path.relpath(file_path, REPO).replace("\\", "/")
    return rel[:-3].replace("/", ".")


#: The directories a test must not write into: places the OPERATOR's records live.
#:
#: NOT "anything outside the repo". That generalisation was considered and rejected: it would
#: sweep in executables, read-only resources and configuration, and a guard that flags those
#: gets exemptions written for it until nobody believes its failures any more. What earns a
#: name here is that a live record accumulates in it -- something a later analysis reads back
#: and draws a conclusion from.
#:
#: `.companion_runs` was added after tests were found writing into it (a file there changed
#: during a run on 2026-09-06). It is under HOME, not the repo, which is exactly why the
#: .fleet-only walk never saw it. relay/selfimprove/trace_to_eval reads corrections_*.jsonl
#: from that directory and PROMOTES what qualifies into an evaluation ledger, so a corrections
#: file written by a test could be promoted as though a person had written it. There were zero
#: such files at the time, which is not a reason for an exemption: it is the reason there was
#: no damage yet.
RECORD_DIR_MARKERS = (".fleet", ".companion_runs")


def fleet_constants():
    """(module, CONSTANT) for every module-level constant naming an operator-record directory.

    A TRIPWIRE, NOT A PROOF. It reads the source for a constant whose text mentions one of the
    marker directories; a path assembled at call time, or built from a name this does not know,
    passes straight through. What it removes is the failure that actually happened five times
    here: a shared record nobody thought about. Its own docstring said as much when it only
    knew .fleet, and knowing one more name does not make it complete.
    """
    found = set()
    for package in PACKAGES:
        root = os.path.join(REPO, package)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            if "__pycache__" in dirpath:
                continue
            for name in files:
                if not name.endswith(".py") or name.startswith("test_"):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    src = io.open(path, encoding="utf-8", errors="replace").read()
                    tree = ast.parse(src)
                except Exception:
                    continue
                for node in tree.body:
                    if not isinstance(node, ast.Assign):
                        continue
                    segment = ast.get_source_segment(src, node) or ""
                    if not any('"%s"' % m in segment or "'%s'" % m in segment
                               for m in RECORD_DIR_MARKERS):
                        continue
                    for target in node.targets:
                        # Uppercase or _UPPERCASE: the module-level-constant convention. A
                        # lowercase module-level name is a computed value, not a declared path.
                        if isinstance(target, ast.Name) and target.id.lstrip("_").isupper():
                            found.add((_module_path(path), target.id))
    return found


def test_the_walker_finds_something():
    """A detector that silently finds nothing would pass every assertion below. This
    repository has dozens; if it ever returns an empty set, the walk is broken, not the code."""
    assert len(fleet_constants()) >= 20


def test_every_shared_record_is_classified():
    known = {(m, c) for m, consts in C.LIVE_RECORD_REDIRECTS.items() for c in consts}
    known |= set(C.DELIBERATELY_NOT_REDIRECTED)
    unclassified = sorted(fleet_constants() - known)
    assert not unclassified, (
        "these name an operator-record directory and are neither redirected for tests nor "
        "listed as "
        "deliberately unredirected -- add each to conftest.LIVE_RECORD_REDIRECTS, or to "
        "DELIBERATELY_NOT_REDIRECTED with the reason it is safe:\n  "
        + "\n  ".join("%s.%s" % pair for pair in unclassified))


def test_the_lists_do_not_name_anything_that_no_longer_exists():
    """A stale entry is a redirect protecting a file that moved, which reads as protection and
    is not."""
    found = fleet_constants()
    listed = {(m, c) for m, consts in C.LIVE_RECORD_REDIRECTS.items() for c in consts}
    listed |= set(C.DELIBERATELY_NOT_REDIRECTED)
    stale = sorted(listed - found)
    assert not stale, "listed but no longer a module-level .fleet constant: %s" % (stale,)


def test_every_exemption_carries_a_reason():
    for pair, reason in C.DELIBERATELY_NOT_REDIRECTED.items():
        assert isinstance(reason, str) and len(reason.strip()) >= 20, (
            "%s.%s is exempt with no real reason; an unexplained exemption is the allowlist "
            "failing open again, one entry at a time" % pair)


@pytest.mark.parametrize("module,const", sorted(
    {(m, c) for m, consts in C.LIVE_RECORD_REDIRECTS.items() for c in consts}))
def test_the_redirect_actually_lands(module, const):
    """The fixture is autouse, so by the time this test body runs the constant must already
    point somewhere under pytest's tmp -- not under the repository."""
    import importlib
    import sys

    lazy = module in C.ONLY_IF_ALREADY_IMPORTED
    if lazy and module not in sys.modules:
        # By design. Importing the bridge costs ~4 seconds and this fixture is autouse, so a
        # module nothing has imported is left alone -- a test that never imports it cannot
        # write through it either. The patching itself is checked below, on a module that IS
        # imported.
        importlib.import_module(module)
        assert getattr(importlib.import_module(module), const, None) is not None, \
            "%s.%s is gone; the redirect names a constant that no longer exists" % (
                module, const)
        return

    mod = importlib.import_module(module)
    value = str(getattr(mod, const, "") or "")
    assert value, "%s.%s is gone; the redirect names a constant that no longer exists" % (
        module, const)
    assert os.path.join(REPO, ".fleet") not in value, (
        "%s.%s still points into the live .fleet during a test: %s" % (module, const, value))


def test_a_lazily_imported_module_is_patched_once_it_is_present(tmp_path, monkeypatch):
    """The deferred half of the fixture, exercised rather than assumed.

    The bridge is imported only when a test has already pulled it in. This reproduces the
    fixture's own loop against an already-imported module and checks the constant moves --
    otherwise "we patch it when it is present" is a sentence nobody has run.
    """
    import importlib
    module = "bridge.copilot_bridge"
    if module not in C.ONLY_IF_ALREADY_IMPORTED:
        pytest.skip("the bridge is no longer deferred; this test's premise is gone")
    mod = importlib.import_module(module)          # now it IS present
    consts = C.LIVE_RECORD_REDIRECTS[module]

    for const, filename in consts.items():
        original = getattr(mod, const, None)
        assert original is not None, "%s.%s vanished" % (module, const)
        from pathlib import Path as _P
        target = tmp_path / filename
        value = _P(str(target)) if isinstance(original, _P) else str(target)
        monkeypatch.setattr(mod, const, value, raising=False)
        assert os.path.join(REPO, ".fleet") not in str(getattr(mod, const))


def test_the_walk_reaches_records_outside_the_repo():
    """The gap this guard had, pinned so it cannot come back quietly.

    It looked only for `.fleet`, and the operator's trace directory is `~/.companion_runs` --
    outside the repo, under a different name. Tests were writing there unnoticed. Both modules
    that declare it must be found; there are two, which is itself why a name-based walk is
    worth having.
    """
    found = fleet_constants()
    assert ("tools.trace_ops", "RUNS_DIR") in found
    assert ("tools.runlog_ops", "RUNS_DIR") in found


def test_a_test_run_leaves_the_operators_trace_directory_alone():
    """The property the classification is FOR, checked directly rather than inferred from the
    table. Uses the same redirect the autouse fixture applies, then writes through the module's
    own API and asserts the real directory did not gain a file."""
    from pathlib import Path
    from tools import trace_ops as TO

    live = Path.home() / ".companion_runs"
    before = set(p.name for p in live.glob("*")) if live.is_dir() else set()
    TO.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    (TO.RUNS_DIR / "isolation_probe.jsonl").write_text("{}\n", encoding="utf-8")

    assert str(live) not in str(TO.RUNS_DIR), (
        "RUNS_DIR still points at the operator's directory during tests: %s" % TO.RUNS_DIR)
    after = set(p.name for p in live.glob("*")) if live.is_dir() else set()
    assert after == before, "a test write reached the live trace directory: %s" % (after - before)
