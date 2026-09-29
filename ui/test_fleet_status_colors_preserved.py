from pathlib import Path

THEME = Path(__file__).with_name("Theme.cs").read_text(encoding="utf-8")
COCKPIT = Path(__file__).with_name("FleetCockpit.cs").read_text(encoding="utf-8-sig")


def test_started_and_running_states_stay_blue_while_done_stays_green():
    # This is an operator-facing invariant inherited from the earlier cockpit: Starting/Running
    # states are blue/info; completed work is green/success.  UI refactors may change layout but
    # must not flatten or swap these semantic colours.
    for status in ("ready", "waiting", "researching", "refuting", "verifying"):
        assert f'{{ "{status}"' in THEME
        line = next(l for l in THEME.splitlines() if f'{{ "{status}"' in l)
        assert '"info"' in line, (status, line)
    done = next(l for l in THEME.splitlines() if '{ "done"' in l)
    assert '"success"' in done


def test_blue_and_green_tokens_keep_the_historical_hues():
    assert 'Info(bool d)' in THEME
    assert '"#60A5FA" : "#2563EB"' in THEME
    assert 'Success(bool d)' in THEME
    assert '"#22C55E" : "#15803D"' in THEME


def test_done_card_explicitly_uses_success_and_live_card_uses_status_kind():
    assert 'isDone ? "success"' in COCKPIT
    assert 'Theme.StatusKind(status)' in COCKPIT


def test_queued_before_start_remains_neutral_not_fake_running_blue():
    pending = next(l for l in THEME.splitlines() if '{ "pending"' in l)
    assert '"neutral"' in pending
