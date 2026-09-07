# -*- coding: utf-8 -*-
"""What survives a move between machines.

The failure these pin: a copied .env that looks configured and is not. Nothing here starts a
process or reads the real .env -- the classification is the thing under test.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import env_portability as EP  # noqa: E402


def test_the_dpapi_value_is_not_carried_to_another_machine():
    """THE DEFECT THIS FILE OPENS WITH. Copied, it decrypts on neither the new account nor the
    new machine, and the failure surfaces as "not configured" at the first mutating tool --
    three days after the install looked successful."""
    assert EP.classify("MCP_UNLOCK_PASSWORD_PROTECTED") == "machine_bound"
    out = EP.merge_for_new_machine("MCP_UNLOCK_PASSWORD_PROTECTED=dpapi:AAA\nMCP_TOOL_MAP=1\n")
    assert "MCP_UNLOCK_PASSWORD_PROTECTED" not in "\n".join(out["lines"])
    assert "MCP_TOOL_MAP=1" in out["lines"]
    assert [k for k, _why in out["dropped"]] == ["MCP_UNLOCK_PASSWORD_PROTECTED"]


def test_the_tunnel_identity_does_not_travel():
    """A dev tunnel belongs to the account hosting it and cannot be renamed. Two machines
    carrying one name fight over the host, which reads as an intermittent outage."""
    for key in ("MCP_TUNNEL_NAME", "MCP_TUNNEL_URL"):
        assert EP.classify(key) == "machine_bound", key


def test_ordinary_settings_do_travel():
    """The classification has to be narrow. Dropping a working setting is the opposite failure
    and would be blamed on the transfer for a long time."""
    for key in ("MCP_TOOL_MAP", "MCP_API_KEY", "MCP_IMPL_AGENT_URL", "TASK_JOB_APPROVAL_MODE"):
        assert EP.classify(key) == "portable", key


def test_what_this_machine_already_established_wins():
    """The new machine's own values were made here, so they are valid here. Overwriting them
    with the foreign file is the failure being prevented, not a merge strategy."""
    out = EP.merge_for_new_machine("MCP_API_KEY=from-the-old-pc\n",
                                   "MCP_API_KEY=made-here\n")
    assert "MCP_API_KEY=made-here" in out["lines"]
    assert "MCP_API_KEY=from-the-old-pc" not in out["lines"]
    assert out["kept_local"] == ["MCP_API_KEY"]


def test_a_machine_bound_value_belonging_to_this_machine_is_kept():
    """Dropping is about the INCOMING file. A value this machine created for itself must
    survive the merge, or every transfer would delete the new host's own tunnel."""
    out = EP.merge_for_new_machine("MCP_TUNNEL_NAME=old-host\n", "MCP_TUNNEL_NAME=this-host\n")
    assert "MCP_TUNNEL_NAME=this-host" in out["lines"]


def test_the_flags_whose_absence_reads_as_a_different_fault_are_filled_in():
    """FLEET_INTAKE_AUTOSTART missing does not look like a config gap; it looks like the fleet
    ignoring goals. Eleven were lost that way."""
    out = EP.merge_for_new_machine("MCP_TOOL_MAP=1\n")
    joined = "\n".join(out["lines"])
    assert "FLEET_INTAKE_AUTOSTART=1" in joined
    assert "MCP_REQUIRE_UNLOCK_TOKEN=1" in joined
    assert set(out["added_defaults"]) == {"FLEET_INTAKE_AUTOSTART", "MCP_REQUIRE_UNLOCK_TOKEN"}


def test_a_default_does_not_override_a_deliberate_setting():
    """An operator who turned autostart off meant it."""
    out = EP.merge_for_new_machine("FLEET_INTAKE_AUTOSTART=0\n")
    assert "FLEET_INTAKE_AUTOSTART=0" in out["lines"]
    assert out["added_defaults"] == ["MCP_REQUIRE_UNLOCK_TOKEN"]


def test_a_repeated_key_keeps_the_last_one_the_way_dotenv_does():
    """Parsing to a dict would silently pick the first and change what the file means."""
    pairs = EP.parse_env("A=1\nA=2\n")
    assert pairs == [("A", "1"), ("A", "2")]
    assert "A=2" in EP.merge_for_new_machine("A=1\nA=2\n")["lines"]


def test_comments_and_blanks_are_not_mistaken_for_settings():
    assert EP.parse_env("# a note\n\nA=1\n") == [("A", "1")]


def test_a_present_but_undecryptable_value_is_reported_as_a_problem():
    """The class of fault a health check misses: not absent, but unusable. A missing value is
    caught at the door; this one is caught nowhere until the first mutating tool."""
    found = EP.problems({"MCP_UNLOCK_PASSWORD_PROTECTED": "dpapi:AAAAnotarealblob=="})
    assert found, "an undecryptable unlock password was not reported"
    assert found[0]["key"] == "MCP_UNLOCK_PASSWORD_PROTECTED"
    assert found[0]["problem"] == "undecryptable"


def test_a_healthy_configuration_reports_nothing():
    """The regression guard: a detector that always fires is not a detector."""
    assert EP.problems({"MCP_UNLOCK_PASSWORD": "plain-and-readable"}) == []


# ── repairing the cause, not describing it ────────────────────────────────────────────────────

def _write_env(tmp_path, text):
    p = tmp_path / ".env"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_an_undecryptable_password_is_replaced_with_one_this_machine_can_use(tmp_path,
                                                                             monkeypatch):
    """THE CAUSE, NOT THE MESSAGE. Improving unlock()'s wording would leave the install broken
    and put a person in the loop for something no person chose: the password is generated by
    setup.ps1, not remembered by anyone. setup.ps1 only writes .env when there is none, so a
    copied file keeps its unusable value forever.

    This one takes a REAL round trip through DPAPI -- protect here, read back here -- because
    that is the property under test: the replacement must be openable by this account. That
    needs Windows, so it is skipped elsewhere. The other repair tests stub protect_secret and
    run everywhere; the narrowing, the backup and the stale-plain-value rule are all covered
    there, so the CI runner still exercises the logic, just not the crypto.
    """
    from tools import env_portability as EP
    from tools import secret_store as SS

    if os.name != "nt":
        pytest.skip("DPAPI protection exists only on Windows")

    protected_here = SS.protect_secret("whatever-this-machine-can-open")
    monkeypatch.setattr(SS, "protect_secret", lambda v: protected_here)

    env_path = _write_env(tmp_path, "MCP_API_KEY=keep-me\n"
                                    "MCP_UNLOCK_PASSWORD_PROTECTED=dpapi:AAAAfromanotherpc==\n")
    env = {"MCP_UNLOCK_PASSWORD_PROTECTED": "dpapi:AAAAfromanotherpc=="}
    got = EP.repair_unlock_password(env_path, env)

    assert got["acted"] is True, got
    text = open(env_path, encoding="utf-8").read()
    assert "dpapi:AAAAfromanotherpc==" not in text, "the unusable value was left in place"
    assert "MCP_API_KEY=keep-me" in text, "an unrelated secret was lost"
    written = dict(EP.parse_env(text))
    assert SS.unlock_password_from_env(written) == "whatever-this-machine-can-open", (
        "the replacement cannot be read back on this machine")


def test_a_working_password_is_never_touched(tmp_path):
    """The narrowing that matters. This edits the file holding every other secret."""
    from tools import env_portability as EP
    env_path = _write_env(tmp_path, "MCP_UNLOCK_PASSWORD=works\n")
    got = EP.repair_unlock_password(env_path, {"MCP_UNLOCK_PASSWORD": "works"})
    assert got["acted"] is False
    assert open(env_path, encoding="utf-8").read() == "MCP_UNLOCK_PASSWORD=works\n"


def test_an_absent_password_is_left_to_first_time_setup(tmp_path):
    """Nothing to repair is not the same as something broken. Generating one here would race
    setup.ps1 and hide a genuinely unconfigured install."""
    from tools import env_portability as EP
    env_path = _write_env(tmp_path, "MCP_API_KEY=x\n")
    got = EP.repair_unlock_password(env_path, {})
    assert got["acted"] is False
    assert "setup" in got["reason"]


def test_the_previous_env_is_kept_before_anything_is_written(tmp_path, monkeypatch):
    """A backup, because the file being edited holds every other secret on the machine."""
    from tools import env_portability as EP
    from tools import secret_store as SS
    monkeypatch.setattr(SS, "protect_secret", lambda v: "dpapi:BBBBlocal==")
    env_path = _write_env(tmp_path, "MCP_UNLOCK_PASSWORD_PROTECTED=dpapi:AAAAforeign==\n")
    EP.repair_unlock_password(env_path, {"MCP_UNLOCK_PASSWORD_PROTECTED": "dpapi:AAAAforeign=="})
    backup = env_path + ".before-unlock-repair"
    assert os.path.isfile(backup)
    assert "dpapi:AAAAforeign==" in open(backup, encoding="utf-8").read()


def test_a_stale_plain_value_does_not_survive_the_repair(tmp_path, monkeypatch):
    """MCP_UNLOCK_PASSWORD is read FIRST. Leaving an old empty-or-wrong plain line in place
    would let it win over the value just established."""
    from tools import env_portability as EP
    from tools import secret_store as SS
    monkeypatch.setattr(SS, "protect_secret", lambda v: "dpapi:BBBBlocal==")
    env_path = _write_env(tmp_path, "MCP_UNLOCK_PASSWORD=\n"
                                    "MCP_UNLOCK_PASSWORD_PROTECTED=dpapi:AAAAforeign==\n")
    EP.repair_unlock_password(env_path, {"MCP_UNLOCK_PASSWORD_PROTECTED": "dpapi:AAAAforeign=="})
    text = open(env_path, encoding="utf-8").read()
    assert "MCP_UNLOCK_PASSWORD=" not in text.replace("MCP_UNLOCK_PASSWORD_PROTECTED=", "")
