"""theme_from_goal must not cut a goal that starts with a date at the slash in the date."""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.project_memory import theme_from_goal  # noqa: E402


def test_a_leading_month_day_is_not_split_at_its_slash():
    theme = theme_from_goal("10/2〜10/4の金沢から立山への行程を作って")
    assert theme != "10"
    assert theme.startswith("10/2")


def test_a_leading_year_month_day_is_not_split_at_its_slash():
    theme = theme_from_goal("2026/10/2の予定を整理して")
    assert theme != "2026"
    assert theme.startswith("2026/10/2")


def test_a_real_slash_separator_still_ends_the_first_clause():
    assert theme_from_goal("ログ整理/古いファイルを消す") == "ログ整理"


def test_a_comma_still_ends_the_first_clause():
    assert theme_from_goal("在庫を確認、明日までに") == "在庫を確認"
