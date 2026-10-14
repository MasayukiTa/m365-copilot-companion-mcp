# -*- coding: utf-8 -*-
"""The cockpit's automatic retry command is accepted, and its retry flag reaches the runner.

ui/FleetCockpit.cs RetryEntry sends an add_goal item with ``retry: true``. The allowed-key set
``_GOAL_KEYS`` did not list ``retry``, so validate_command rejected the whole command with
"unknown key(s) ['retry']". goals_from_command also dropped the flag, so the runner's
duplicate-retry guard (which reads ``item.get("retry")``) never saw a retry.
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def test_a_cockpit_retry_item_is_accepted_and_its_flag_survives():
    from relay import fleet_runner as FR

    item = {"text": "retry this goal", "priority": True, "retry": True,
            "checks": None, "cwd": None}
    errs = FR.validate_command({"add_goal": [item]})
    assert not any("retry" in e for e in errs), errs

    out = FR.goals_from_command({"add_goal": [item]})
    assert out and out[0].get("retry") is True

    # A plain goal is not marked, and a non-boolean retry is refused, not silently kept.
    plain = FR.goals_from_command({"add_goal": [{"text": "x"}]})
    assert "retry" not in plain[0]
    assert "retry must be true/false" in FR._goal_item_error({"text": "x", "retry": "yes"})
