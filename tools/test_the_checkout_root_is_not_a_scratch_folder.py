# -*- coding: utf-8 -*-
"""The checkout root is not a scratch folder: a NEW untracked top-level name is refused.

These tests pin the three things the placement guard promises and the two it must NOT do:

  PROMISES
    1. A new untracked top-level name under the checkout root is a 'violation'.
    2. Writing INTO a tracked top-level entry (tools/..., docs/...) is allowed.
    3. A product-managed name (.fleet, .jobs ...) and an operator allow-list entry are allowed.
  MUST NOT
    4. A path OUTSIDE the checkout (the per-run temp work area, anywhere else) is never judged.
    5. In the default 'warn' mode nothing is raised -- the write proceeds and is only recorded;
       only 'enforce' raises.

The guard reads its mode from settings.txt like every other each_gate control, so these drive
it through the MCP_WORKAREA_GUARD override (which the module documents as beating the file)
rather than writing a settings file, keeping the test hermetic.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import workarea_guard as WG  # noqa: E402


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("MCP_WORKAREA_GUARD", raising=False)
    WG._reset_for_test()
    yield
    WG._reset_for_test()


def _root_child(name: str) -> str:
    return str(WG.CHECKOUT_ROOT / name)


# ---------------------------------------------------------------- classify: the verdicts
def test_a_new_untracked_top_level_name_is_a_violation():
    verdict, reason, seg = WG.classify(_root_child("secure_desktop"))
    assert verdict == "violation", (verdict, reason)
    assert seg == "secure_desktop"


def test_a_new_top_level_FILE_is_also_a_violation():
    verdict, _, seg = WG.classify(_root_child("_pr_body.md"))
    assert verdict == "violation"
    assert seg == "_pr_body.md"


def test_writing_into_a_tracked_top_is_allowed():
    # tools/ and docs/ are tracked; a new file DEEP inside them is fine -- the rule is only
    # about new names AT the root.
    for p in ("tools/auto/new_helper.py", "docs/private/ledger.d/entry.md", "relay/x/y.py"):
        verdict, reason, _ = WG.classify(_root_child(p))
        assert verdict == "allow", (p, verdict, reason)


def test_a_product_managed_root_name_is_allowed():
    for p in (".fleet/workarea_refusals.jsonl", ".jobs/j1.json", "generated/out.md"):
        verdict, reason, _ = WG.classify(_root_child(p))
        assert verdict == "allow", (p, verdict, reason)


def test_a_path_outside_the_checkout_is_never_judged(tmp_path):
    verdict, _, seg = WG.classify(str(tmp_path / "anything" / "deep.txt"))
    assert verdict == "outside"
    assert seg is None


def test_a_dotdot_escape_is_resolved_before_judging():
    # tools/../evil would resolve to <root>/evil -- a new top-level name, not a tools/ write.
    verdict, _, seg = WG.classify(_root_child(os.path.join("tools", "..", "evil_scratch")))
    assert verdict == "violation"
    assert seg == "evil_scratch"


# ---------------------------------------------------------------- the operator allow-list
def test_an_operator_allow_entry_is_allowed(monkeypatch, tmp_path):
    settings = tmp_path / "settings.txt"
    settings.write_text("workarea_allow=audit_fixes_20261022\n", encoding="utf-8")
    import tools.settings_path as SP
    monkeypatch.setattr(SP, "NEW_PATH", str(settings))
    WG._reset_for_test()
    verdict, reason, _ = WG.classify(_root_child("audit_fixes_20261022/report.md"))
    assert verdict == "allow", (verdict, reason)
    assert "allow-list" in reason


# ---------------------------------------------------------------- warn vs enforce vs off
def test_warn_mode_records_but_does_not_raise(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_WORKAREA_GUARD", "warn")
    monkeypatch.setattr(WG, "LEDGER_PATH", tmp_path / "refusals.jsonl")
    WG._reset_for_test()
    WG.check_write(_root_child("reviews"))         # must NOT raise
    assert (tmp_path / "refusals.jsonl").exists()
    body = (tmp_path / "refusals.jsonl").read_text(encoding="utf-8")
    assert "reviews" in body and "warned" in body


def test_enforce_mode_raises_on_a_violation(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_WORKAREA_GUARD", "enforce")
    monkeypatch.setattr(WG, "LEDGER_PATH", tmp_path / "refusals.jsonl")
    WG._reset_for_test()
    with pytest.raises(PermissionError):
        WG.check_write(_root_child("audit_scratch"))
    assert "refused" in (tmp_path / "refusals.jsonl").read_text(encoding="utf-8")


def test_enforce_mode_lets_an_allowed_write_through(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_WORKAREA_GUARD", "enforce")
    monkeypatch.setattr(WG, "LEDGER_PATH", tmp_path / "refusals.jsonl")
    WG._reset_for_test()
    WG.check_write(_root_child("tools/auto/x.py"))  # tracked top -> no raise
    WG.check_write(str(tmp_path / "outside.txt"))   # outside -> no raise


def test_off_mode_does_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_WORKAREA_GUARD", "off")
    monkeypatch.setattr(WG, "LEDGER_PATH", tmp_path / "refusals.jsonl")
    WG._reset_for_test()
    WG.check_write(_root_child("whatever_new"))      # no raise, no record
    assert not (tmp_path / "refusals.jsonl").exists()


def test_default_mode_is_warn(monkeypatch, tmp_path):
    # No env, no settings file with the key -> the shipped default is warn, not enforce.
    settings = tmp_path / "settings.txt"
    settings.write_text("", encoding="utf-8")
    import tools.settings_path as SP
    monkeypatch.setattr(SP, "NEW_PATH", str(settings))
    WG._reset_for_test()
    assert WG.mode() == "warn"


# ---------------------------------------------------------------- the file-tools integration
def test_write_file_is_refused_at_the_root_under_enforce(monkeypatch, tmp_path):
    """The guard is reached through file_ops._validate_path, so write_file itself must refuse."""
    monkeypatch.setenv("MCP_WORKAREA_GUARD", "enforce")
    monkeypatch.setattr(WG, "LEDGER_PATH", tmp_path / "refusals.jsonl")
    WG._reset_for_test()
    # Unlock is required by write_file; skip cleanly if the harness is locked rather than
    # asserting on an unrelated subsystem.
    file_ops = pytest.importorskip("tools.file_ops")
    from tools import security
    if security.require_unlocked():
        pytest.skip("harness locked; the _validate_path/classify units cover the logic")
    out = file_ops.write_file(_root_child("brandnew_scratch_dir/x.txt"), "hi")
    assert "error" in out.lower() or "PermissionError" in out


def test_reads_are_never_blocked(monkeypatch):
    """for_write defaults False, so a read of a root-level path is untouched even in enforce."""
    monkeypatch.setenv("MCP_WORKAREA_GUARD", "enforce")
    WG._reset_for_test()
    file_ops = pytest.importorskip("tools.file_ops")
    # Resolving AGENTS.md (a tracked root file) for read must not raise.
    p = file_ops._validate_path(str(WG.CHECKOUT_ROOT / "AGENTS.md"))
    assert p.name == "AGENTS.md"
