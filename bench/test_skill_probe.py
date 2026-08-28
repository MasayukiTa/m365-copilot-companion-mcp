"""The grader for the skill-sentence experiment, written before the arms ran.

The first attempt at this experiment was judged by reading three answers and deciding they
looked alike. That is the shape of judgement that finds whatever it went looking for, which is
why the result could not be defended afterwards. These tests pin the grader's behaviour first,
so the number cannot be argued into a different one after the fact.
"""
from bench.skill_probe import compare, grade


FOLLOWED = """まず範囲を区切ります。
8/1〜8/10: 取得 4件 (hasMoreResults=false で終端確認)
8/11〜8/20: 受信なし(確認済) — 検索は成功、該当0件
8/21〜8/28: 取得 2件 (hasMoreResults=false で終端確認)
合計 6件。"""

IGNORED = "土日の予定は6件でした。"


def test_the_two_shapes_score_differently():
    """THE PROPERTY THE FIRST PROBE LACKED. Following the procedure and ignoring it must not
    lead to the same answer, or agreement between arms means nothing."""
    assert grade(FOLLOWED)["score"] == 3
    assert grade(IGNORED)["score"] == 0


def test_the_same_total_can_come_from_either_shape():
    """Both answers above say six. If the grader scored the COUNT it would call them equal --
    which is exactly the mistake that made the first experiment uninformative."""
    assert "6" in FOLLOWED and "6" in IGNORED
    assert grade(FOLLOWED)["score"] != grade(IGNORED)["score"]


def test_termination_evidence_is_recognised_in_either_form():
    """The worker answers in Japanese and may or may not quote the field name in Latin."""
    assert grade("1/1〜1/5: 3件 (hasMoreResults=false)")["cites_termination"]
    assert grade("1/1〜1/5: 3件 終端確認済")["cites_termination"]


def test_a_single_range_is_not_a_split():
    """One range is what a worker who never split would also produce, so it cannot count as
    evidence of splitting."""
    assert grade("8/1〜8/28: 6件 (hasMoreResults=false)")["split_into_dated_ranges"] is False


def test_the_forbidden_continuation_costs_a_point():
    """The skill forbids "continue from where I left off" because a parallel conversation
    makes it ambiguous. An answer that follows every other rule and uses it is not compliant."""
    good = grade(FOLLOWED)["score"]
    bad = grade(FOLLOWED + "\n残りを続けます。")["score"]
    assert bad == good - 1


def test_saying_it_used_a_skill_is_recorded_but_is_not_the_score():
    """"I used the skill" is a claim; the other signals are evidence. A worker that says it
    and does none of it must not score."""
    g = grade("skill_match を呼びました。土日の予定は6件です。")
    assert g["mentions_skill_lookup"] is True
    assert g["score"] == 0


def test_arms_that_do_not_separate_are_reported_as_a_null_not_a_negative():
    """The first attempt's actual result. It means the probe could not discriminate -- NOT
    that the sentence does nothing, and the difference has to survive into the report."""
    c = compare({"A": [IGNORED], "B": [IGNORED]})
    assert c["separated"] is False
    assert "not evidence that the sentence has no effect" in c["note"]


def test_arms_that_separate_cleanly_are_reported_as_separated():
    c = compare({"A": [FOLLOWED, FOLLOWED], "B": [IGNORED, IGNORED]})
    assert c["separated"] is True and c["note"] == ""
    assert c["arms"]["A"]["mean_score"] > c["arms"]["B"]["mean_score"]


def test_a_gap_smaller_than_an_arms_own_spread_is_not_a_separation():
    """MEASURED, NOT HYPOTHETICAL. Three runs per arm produced arm means of 0.67, 1.00 and
    1.00 while one arm's own scores ranged 0..2 -- and the first version of this comparator
    called that separated, because it asked only whether the gap was non-zero. It was reported
    upward as a finding before anyone looked at the spread."""
    noisy = compare({"A": [FOLLOWED, IGNORED], "B": [IGNORED, IGNORED]})
    assert noisy["widest_within_arm_spread"] >= noisy["gap"]
    assert noisy["separated"] is False


def test_a_small_sample_says_so_rather_than_leaving_it_to_be_inferred():
    c = compare({"A": [FOLLOWED], "B": [IGNORED]})
    assert c["underpowered"] is True


def test_an_empty_answer_scores_zero_rather_than_raising():
    """A worker can return nothing, and the grader must record that as a zero rather than
    taking the run down."""
    assert grade("")["score"] == 0
    assert grade(None)["score"] == 0
