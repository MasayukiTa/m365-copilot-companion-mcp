# -*- coding: utf-8 -*-
"""`auto` was selectable, documented, and the contract gate could not reach it.

`tools/approval_policy.VALID_APPROVAL_MODES` has defined three modes for as long as the module
has existed, and `relay/task_router.job_gate` implements all three:

    bypass   ALLOW everything
    auto     the deterministic classifier decides -- stop -> DENY, ask -> CONFIRM, else ALLOW
    default  every first-seen job class asks a human, and keeps asking

`tools/contract_gate.check_op` read only `bypass`. An operator who chose `auto` got the manual
gate anyway. That is this repository's signature defect -- a capability with no caller -- sitting
inside the safety machinery, where it is invisible from the outside: the gate still appears, so
nothing looks broken, and the setting silently does not mean what it says.

AND THE DISCOURAGED MODE WAS THE DEFAULT in both readers, with the cockpit labelling it
"確認（推奨）". The owner's reason for inverting that is the one that decides it: a design that
asks every time is a design nobody reads, and an approval that is always there is not an
approval.

`auto` IS NOT A RELAXATION AT THE DANGEROUS END, which is what makes it defensible as the
default rather than merely convenient. Under `default` a STOP-pattern operation is put to a
human, who CAN approve it. Under `auto` it is refused outright and cannot be approved at all.
The modes differ only on operations the deterministic classifier finds clean.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import approval_policy, contract_gate  # noqa: E402


@pytest.fixture
def gate(monkeypatch, tmp_path):
    """A contract with one op in ask_before and one in stop_when, and gates in tmp_path."""
    monkeypatch.setattr(contract_gate, "GATE_DIR", tmp_path / "gates", raising=False)
    monkeypatch.setattr(contract_gate, "load_contract",
                        lambda *a, **k: {"active": True, "ask_before": ["outbound"],
                                         "stop_when": ["delete"]}, raising=False)
    monkeypatch.setattr(contract_gate, "policy_state_is_suspect", lambda: "", raising=False)
    return contract_gate


def _mode(monkeypatch, value):
    monkeypatch.setattr("tools.approval_policy.current_approval_mode",
                        lambda default=None: value)


# ── the classifier the mode uses ──────────────────────────────────────────────────────────

def test_the_classifier_reuses_the_live_vocabulary():
    """Two lists that are meant to mean the same thing and are maintained separately is how a
    gate refuses here and allows there. This borrows relay.autonomy_gate's patterns, which
    task_router._static_risk already classifies local payloads with."""
    assert contract_gate._auto_verdict("rm -rf /data") == "stop"
    assert contract_gate._auto_verdict("please summarise the notes") == "clean"


def test_the_classifier_fails_closed(monkeypatch):
    """No classifier means no automatic decision. A mode that quietly became "allow
    everything" would be `bypass` wearing another name."""
    import builtins
    real = builtins.__import__

    def boom(name, *a, **k):
        if name == "relay.autonomy_gate":
            raise ImportError("gone")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", boom)
    assert contract_gate._auto_verdict("anything at all") == "ask"


# ── the mode, through the gate ────────────────────────────────────────────────────────────

def test_auto_refuses_a_prohibited_operation_without_asking(gate, monkeypatch):
    """STRICTER THAN CONFIRM, not looser. Under `default` this same operation is put to a
    person who can approve it; under `auto` it cannot be approved."""
    _mode(monkeypatch, "auto")
    out = gate.check_op("outbound", "rm -rf /data")
    assert out is not None, "a prohibited pattern was allowed through"
    assert "拒否" in out or "Refused" in out


def test_auto_lets_a_clean_operation_through(gate, monkeypatch):
    _mode(monkeypatch, "auto")
    assert gate.check_op("outbound", "read the report and summarise it") is None


def test_auto_still_asks_about_a_flagged_operation(gate, monkeypatch):
    """The classifier's middle answer is "a human decides" -- `auto` does not collapse ask into
    allow, or the ask_before list would mean nothing."""
    _mode(monkeypatch, "auto")
    flagged = None
    from relay.autonomy_gate import _ASK_PATTERNS
    for pat in _ASK_PATTERNS:
        sample = pat.replace(r"\b", "").replace("\\s+", " ").replace("\\", "")
        if contract_gate._auto_verdict(sample) == "ask":
            flagged = sample
            break
    if flagged is None:
        pytest.skip("no ASK pattern reduced to a literal sample here")
    out = gate.check_op("outbound", flagged)
    assert out is not None and ("承認待ち" in out or "Awaiting approval" in out)


def test_bypass_is_unchanged(gate, monkeypatch):
    _mode(monkeypatch, "bypass")
    assert gate.check_op("outbound", "rm -rf /data") is None


def test_no_mode_relaxes_stop_when(gate, monkeypatch):
    """The hard stop is the thing none of this touches. It is what makes the other two modes
    safe to choose at all."""
    for mode in ("auto", "bypass", "default"):
        _mode(monkeypatch, mode)
        out = gate.check_op("delete", "wipe the folder")
        assert out is not None, "stop_when was relaxed under %r" % mode


def test_a_broken_policy_reader_does_not_become_bypass(gate, monkeypatch):
    """If the mode cannot be read the gate must behave as the strictest thing a human can
    still override -- asking -- never as bypass."""
    def boom(default=None):
        raise OSError("settings unreadable")
    monkeypatch.setattr("tools.approval_policy.current_approval_mode", boom)
    out = gate.check_op("outbound", "anything")
    assert out is not None


# ── which mode an installation gets with nothing set ──────────────────────────────────────

def test_the_recommended_mode_is_the_default(monkeypatch, tmp_path):
    monkeypatch.delenv("TASK_JOB_APPROVAL_MODE", raising=False)
    monkeypatch.setattr(approval_policy, "settings_path", lambda: tmp_path / "none.txt")
    assert approval_policy.current_approval_mode() == "auto"
    assert approval_policy.FALLBACK_APPROVAL_MODE == "auto"


def test_an_explicit_choice_still_wins(monkeypatch, tmp_path):
    """Changing a default must not take the setting away from an operator who made one."""
    s = tmp_path / "settings.txt"
    s.write_text("job_approval_mode=default\n", encoding="utf-8")
    monkeypatch.setattr(approval_policy, "settings_path", lambda: s)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert approval_policy.current_approval_mode() == "default"


def test_the_manual_mode_is_still_available():
    """Kept, not removed. It is discouraged because it is unread, not because it is wrong."""
    assert "default" in approval_policy.VALID_APPROVAL_MODES


# ── and the cockpit agrees with the relay ─────────────────────────────────────────────────

def _cockpit():
    p = os.path.join(REPO, "ui", "FleetCockpit.cs")
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def test_the_cockpit_no_longer_calls_the_unread_mode_recommended():
    """THERE ARE TWO SELECTORS. This caught the settings panel still calling the manual mode
    recommended after the prompt window's copy was fixed -- and the settings panel is the
    surface an operator actually opens to change the setting. Asserting over the whole file
    rather than one block is what made the second copy visible."""
    src = _cockpit()
    assert "確認（推奨）" not in src, "the manual mode is still labelled recommended"
    assert src.count("自動（推奨）") >= 2, (
        "only one of the two mode selectors was updated")


def test_the_cockpit_and_the_relay_share_one_default():
    """Two readers of one setting that disagree about its default would show the operator a
    mode the relay is not using."""
    src = _cockpit()
    assert '"TASK_JOB_APPROVAL_MODE") ?? "auto"' in src
    assert 'item == null ? "auto"' in src
