from pathlib import Path

src = (Path(__file__).with_name('fleet_runner.py')).read_text(encoding='utf-8', errors='replace')


def test_live_capacity_is_not_capped_by_the_number_of_goals_at_launch():
    # A run may start with one goal and receive many add_goal commands later.  The admission
    # budget must describe machine/operator capacity, not the size of the queue at t=0.
    assert 'min(settings_maxtabs(), len(goals))' not in src
    assert 'auto_concurrency(len(goals))' not in src
    assert 'min(asc_ceiling, len(goals))' not in src
