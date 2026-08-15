"""Proactive feedback: does it stay quiet, and does it say something actionable when it does not.

Both halves are the design. A monitor that speaks every cycle stops being read, and one that
only ever brings bad news gets muted -- so silence has to be the common case and each
observation has to name a decision somebody can make.
"""
from __future__ import annotations

from relay.selfimprove import harness_feedback as HF


def _states(**counts):
    out = []
    for state, n in counts.items():
        out += [{"state": state}] * n
    return out


def test_a_healthy_loop_produces_nothing():
    """閾値を超えるものが無ければ無言。それが正常であって、監視の故障ではない。"""
    assert HF.observe(decisions=_states(KEEP=2, INCONCLUSIVE=4, REJECT=4)) == []
    assert "healthy outcome" in HF.report([])


def test_a_short_campaign_says_nothing_about_anything():
    """3件から形状を語ると、自信のあるノイズが出る。無言のほうがまだ良い。"""
    assert HF.observe(decisions=_states(INFRA_ABORT=3)) == []


def test_an_unwell_environment_is_the_first_thing_to_fix():
    obs = HF.observe(decisions=_states(INFRA_ABORT=4, KEEP=1, REJECT=5))
    assert obs and "unwell" in obs[0]["finding"]
    assert "fix the environment" in obs[0]["do"]


def test_mostly_inconclusive_means_the_slices_are_too_small():
    """『効果がなかった』ではなく『検出力がなかった』。区別が付かないと
    有望な方向を捨て続ける。"""
    obs = HF.observe(decisions=_states(INCONCLUSIVE=9, REJECT=1))
    assert any("too small to decide" in o["finding"] for o in obs)
    assert any("raise N" in o["do"] for o in obs)


def test_a_proposer_whose_predictions_never_hold_is_named_as_the_problem():
    """候補を疑う前に、候補を作っている仕組みを疑う。"""
    obs = HF.observe(decisions=_states(REJECT=10),
                     prediction_accuracy={"decided": 20, "keep_rate": 0.0})
    assert any("not surviving contact" in o["finding"] for o in obs)


def test_a_reasonable_prediction_rate_is_not_flagged():
    """ほとんどの案は落ちる。低いこと自体は異常ではない。"""
    obs = HF.observe(decisions=_states(REJECT=8, KEEP=2),
                     prediction_accuracy={"decided": 20, "keep_rate": 0.2})
    assert not any("surviving contact" in o["finding"] for o in obs)


def test_a_search_stuck_in_one_behaviour_is_surfaced():
    obs = HF.observe(decisions=_states(REJECT=10),
                     qd_coverage={"cells_occupied": 1, "described": 10, "total": 10})
    assert any("one behaviour" in o["finding"] for o in obs)
    assert any("vary a component" in o["do"] for o in obs)


def test_an_unrun_holdout_is_reported_as_unrun():
    obs = HF.observe(decisions=_states(KEEP=8), sealed_unevaluable=8)
    assert any("holdout has not been run" in o["finding"] for o in obs)
    assert any("unrun canary is not a passed one" in o["do"] for o in obs)


def test_incomplete_security_evidence_is_reported_as_such():
    obs = HF.observe(decisions=_states(KEEP=8), security_incomplete=8)
    assert any("cannot carry it" in o["finding"] for o in obs)


def test_every_observation_names_an_action():
    """『通過率73%』は feedback ではない。何をするかが書いていなければ読まれない。"""
    obs = HF.observe(decisions=_states(INFRA_ABORT=5, INCONCLUSIVE=8,
                                       NEEDS_HUMAN_REVIEW=5),
                     sealed_unevaluable=3, security_incomplete=2)
    assert obs
    for o in obs:
        assert o["do"].strip() and o["evidence"].strip() and o["finding"].strip()
    assert "do:" in HF.report(obs)


def test_it_recommends_and_never_acts():
    """気づいて直す部品は、判定者のいないオプティマイザ。"""
    import inspect
    src = inspect.getsource(HF)
    for forbidden in ("write_active", "apply_genome", "subprocess", "os.remove"):
        assert forbidden not in src, "feedback が行動している: %s" % forbidden
