"""The shadow comparison has to be able to REFUTE the explanation it was built to test.

A replay that can only ever agree with the mechanism its author likes is not evidence. So the
tests below cover both directions explicitly: a log where retrying rescues turns, and a log
where it rescues none -- and the second must produce a verdict that says the explanation is
unsupported, not one that stays quiet about it.
"""
from bench.companionbench import shadow_rules as S


def _log(*attempts):
    """attempts: (ok, found) pairs, in order."""
    return [{"ok": ok, "found": found, "truncated": False, "users": 1, "at_s": i * 2.0}
            for i, (ok, found) in enumerate(attempts)]


# ---- the two rules ------------------------------------------------------------------------

def test_the_old_rule_is_decided_by_the_first_ok_response():
    """それが旧実装の挙動そのもの -- 中身に関わらず最初の ok で打ち切っていた。"""
    assert S.old_verdict(_log((False, False), (True, False), (True, True))) is False
    assert S.old_verdict(_log((True, True))) is True
    assert S.old_verdict(_log((False, False), (False, False))) is None


def test_the_new_rule_takes_a_marker_found_on_any_attempt():
    assert S.new_verdict(_log((True, False), (True, True))) is True
    assert S.new_verdict(_log((True, False), (True, False))) is False
    assert S.new_verdict(_log((False, False))) is None


# ---- what the comparison licenses ----------------------------------------------------------

def test_a_late_marker_is_counted_as_rescued_and_reported_as_current_evidence():
    """最初の ok では無く、後の試行で現れた -- 再送はしないので、遅れて描画されたということ。"""
    rows = [{"episode_id": "a", "attempt_log": _log((True, False), (True, True))},
            {"episode_id": "b", "attempt_log": _log((True, True))}]
    got = S.compare(rows)
    assert got["rescued"] == 1 and got["agreed_found"] == 1
    assert got["rescue_rate"] == 0.5
    assert "rendered late" in got["verdict"]
    assert "does not establish that the earlier run" in got["verdict"], "過去へ外挿している"


def test_no_rescues_says_the_explanation_is_unsupported_rather_than_staying_quiet():
    """これが本命。ハイドレーション説を『支持されない』と言えなければ、この道具は
    著者の気に入った説明に同意するだけの装置になる。"""
    rows = [{"episode_id": "a", "attempt_log": _log((True, True))},
            {"episode_id": "b", "attempt_log": _log((True, False), (True, False))}]
    got = S.compare(rows)
    assert got["rescued"] == 0
    assert "not operating here" in got["verdict"]
    assert "unsupported by this sample" in got["verdict"]


def test_rows_without_an_attempt_log_are_not_silently_counted_as_agreement():
    """記録の無い行を『一致』に数えると、計器が動いていない run が最も綺麗に見える。"""
    got = S.compare([{"episode_id": "a"}, {"episode_id": "b", "attempt_log": []}])
    assert got["scored"] == 0
    assert "says nothing about either" in got["verdict"]


def test_a_marker_the_new_rule_lost_is_flagged_as_a_broken_replay():
    """新ルールが旧ルールの見つけたマーカーを失うことは原理上ありえない。
    起きたなら再生側の欠陥なので、そう言う。"""
    got = S.compare([{"episode_id": "a",
                      "attempt_log": [{"ok": True, "found": True, "at_s": 0}]}])
    assert got["reversed"] == 0
    # construct the impossible case directly against the summariser
    assert "fix the replay" in S.verdict({"scored": 3, "rescued": 0, "reversed": 1})


def test_it_reads_result_rows_as_well_as_transcript_entries():
    """保存済みの run を整形し直さずに指せること -- 整形の途中で落ちるのが一番怖い。"""
    got = S.compare([{"episode_id": "a",
                      "delivery_attempt_log": _log((True, False), (True, True))}])
    assert got["scored"] == 1 and got["rescued"] == 1


def test_rescue_latency_is_reported_because_confirmed_alone_is_not_weighable():
    rows = [{"episode_id": "a", "attempt_log": _log((True, False), (True, False), (True, True))}]
    got = S.compare(rows)
    assert got["rescue_latency_s"]["max"] == 4.0
