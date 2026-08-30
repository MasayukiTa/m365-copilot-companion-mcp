"""The dangerous-command gate must not go quiet because a switch is on somewhere else.

A demotion was added here on the argument that once execution is routed into a container,
holding a command for human approval asks a person to approve something that cannot happen --
which is how approval queues become noise people click through. The argument is sound. The
implementation checked whether ROUTING WAS ENABLED, which is a different question.

Routing being on does not mean THIS call was routed. An operator's call, or any call naming a
path no container owns, passes through the gateway and executes on this machine. And a call
that genuinely was routed returns from the gateway before local dispatch, so it never reaches
check_op at all. The condition therefore fired for exactly the commands still about to run
here.
"""
import json

import tools.contract_gate as CG


def _armed(tmp_path, monkeypatch):
    """A contract that gates shell_destructive, with EVERY path this module writes to in tmp.

    Redirecting the contract file alone is not enough. A firing gate also queues the operation
    for a human under ALLOWED_BASE/.companion_gates, and a test that leaves that pointing at
    the real tree posts its fixture into the operator's actual approval queue -- which has
    happened here, with `rm -rf /` as the fixture.

    The op class is "ask_before"; an earlier version of this fixture invented a key name, and
    the gate then read as inert for a reason that had nothing to do with what was being tested.
    """
    import tools.file_ops as FO
    monkeypatch.setattr(FO, "ALLOWED_BASE", tmp_path)
    monkeypatch.setattr(CG, "_FLEET_DIR", str(tmp_path))
    monkeypatch.setattr(CG, "_CONTRACT_FILE", tmp_path / "active_contract.json")
    monkeypatch.setattr(CG, "_SEEN", {"active_contract": False, "retired_via_api": False})
    (tmp_path / "active_contract.json").write_text(
        json.dumps({"active": True, "ask_before": ["shell_destructive"]}), encoding="utf-8")


def test_the_gate_still_fires_while_routing_is_switched_on(tmp_path, monkeypatch):
    _armed(tmp_path, monkeypatch)
    from relay import broker_client as BC
    marker = tmp_path / "BROKER_ON"
    marker.write_text("on", encoding="utf-8")
    monkeypatch.setattr(BC, "MARKER", str(marker))
    monkeypatch.delenv("SWE_BROKER", raising=False)
    assert BC.enabled() is True, "the premise of this test is that the switch is on"

    out = CG.check_op("shell_destructive", "rm -rf /var/data")
    assert out is not None, (
        "the gate went inert because routing was switched on, but reaching check_op means "
        "the gateway did NOT route this call -- it is about to run on this machine")


def test_the_gate_fires_with_routing_off_too(tmp_path, monkeypatch):
    _armed(tmp_path, monkeypatch)
    from relay import broker_client as BC
    monkeypatch.setattr(BC, "MARKER", str(tmp_path / "absent"))
    monkeypatch.delenv("SWE_BROKER", raising=False)
    assert CG.check_op("shell_destructive", "rm -rf /var/data") is not None


def test_an_ungated_op_is_still_inert(tmp_path, monkeypatch):
    """The gate must not become a blanket refusal in the course of not being demoted."""
    _armed(tmp_path, monkeypatch)
    assert CG.check_op("delete", "some/file.txt") is None
