"""Phase 6 has a caller now: run_episode can ask the solver a follow-up question.

`relay/selfimprove/solver_feedback.py` was fully implemented and tested and nothing called
it -- a phase whose module nothing calls is not a phase. These tests are about the SEAM, not
the module: they pin the three things the wiring must not get wrong.

  * off by default, and off means off: no extra turn, no new key on the result row;
  * on means the solver is actually asked, and the answer is tagged with the episode it
    came from;
  * whatever the follow-up produces cannot change the grade, and cannot reach a gate --
    because it is asked only after grade_final_state has already run.
"""
import json
import tempfile

import bench.companionbench.agents as A
from bench.companionbench import runner as R
from bench.companionbench.episode import Episode, GradeResult
from relay.selfimprove import experiment as EX
from relay.selfimprove import solver_feedback as SF


def _tmp():
    return tempfile.mkdtemp(prefix="cbfeedback_")


class _Ep(Episode):
    """A tiny always-passing episode, so any grade difference is the wiring's fault."""

    def __init__(self, episode_id="e1", category="filesystem"):
        self.episode_id = episode_id
        self.category = category

    def setup(self, workdir):
        return "do the thing"

    def grade_final_state(self, workdir, *, reply=""):
        return GradeResult(functional_score=1.0, security_score=1.0, side_effect_score=1.0)


def _agent(calls, feedback_reply="", boom_on_feedback=False):
    """Records every prompt it is asked. Answers the solver-feedback question distinctly
    from the task prompt, so a test can tell which turn produced which reply."""
    def fn(prompt, workdir):
        calls.append(prompt)
        if prompt == SF.prompt():
            if boom_on_feedback:
                raise RuntimeError("adapter choked on the follow-up")
            return feedback_reply
        return "done"
    return A.in_process(fn)


def _clear_flag(monkeypatch):
    monkeypatch.delenv("MCP_SOLVER_FEEDBACK", raising=False)


# ---- off by default, and byte-identical -----------------------------------------------

def test_flag_defaults_off():
    assert SF.enabled() is False


def test_flag_off_sends_no_extra_turn(monkeypatch):
    """フラグ既定(OFF)では、エピソードの本来のプロンプト以外は送信されない。"""
    _clear_flag(monkeypatch)
    calls = []
    r = R.run_episode(_Ep(), _agent(calls), root=_tmp())
    assert calls == ["do the thing"], "追加ターンが送信された: %r" % calls
    assert r["success"] is True


def test_flag_explicitly_off_also_sends_no_extra_turn(monkeypatch):
    monkeypatch.setenv("MCP_SOLVER_FEEDBACK", "0")
    calls = []
    R.run_episode(_Ep(), _agent(calls), root=_tmp())
    assert len(calls) == 1


def test_flag_off_row_carries_no_solver_feedback_key(monkeypatch):
    """フラグ OFF の行は、このワイヤリングが存在しなかった頃と鍵の形まで同じでなければ
    ならない -- 行全体を比較する既存の呼び出し元を壊さないため。"""
    _clear_flag(monkeypatch)
    r = R.run_episode(_Ep(), _agent([]), root=_tmp())
    assert "solver_feedback" not in r


def test_flag_off_grade_matches_flag_on_grade(monkeypatch):
    """追加ターンの有無で、同じエピソードの採点結果が変わってはならない。
    grade は追加ターンより前に確定しているので、これは構造的に保証されるはずのもの。"""
    grade_keys = ("success", "functional_score", "security_score", "side_effect_score",
                  "infra_failure")

    _clear_flag(monkeypatch)
    off = R.run_episode(_Ep(), _agent([], feedback_reply="ignored"), root=_tmp())

    monkeypatch.setenv("MCP_SOLVER_FEEDBACK", "1")
    on = R.run_episode(_Ep(), _agent([], feedback_reply=json.dumps(
        {"tool_friction": ["read_file pages awkwardly"]})), root=_tmp())

    for key in grade_keys:
        assert off[key] == on[key], key


# ---- on: the solver is actually asked, and tagged with its episode -------------------

def test_flag_on_asks_a_second_turn_after_the_task_turn(monkeypatch):
    monkeypatch.setenv("MCP_SOLVER_FEEDBACK", "1")
    calls = []
    R.run_episode(_Ep(), _agent(calls, feedback_reply="{}"), root=_tmp())
    assert len(calls) == 2, "追加ターンが送信されていない: %r" % calls
    assert calls[0] == "do the thing"
    assert calls[1] == SF.prompt()


def test_flag_on_collects_feedback_tagged_with_the_episode_id(monkeypatch):
    monkeypatch.setenv("MCP_SOLVER_FEEDBACK", "1")
    calls = []
    reply = json.dumps({"tool_friction": ["read_file pages awkwardly"],
                        "suggested_harness_change": "raise the read chunk size"})
    r = R.run_episode(_Ep("ep_xyz"), _agent(calls, feedback_reply=reply), root=_tmp())
    entry = r["solver_feedback"]
    assert entry["episode_id"] == "ep_xyz", "エピソードに紐づかない項目は検証できない"
    assert entry["tool_friction"] == ["read_file pages awkwardly"]
    assert entry["suggested_harness_change"] == "raise the read chunk size"
    assert "parse_error" not in entry


def test_flag_on_an_unreadable_follow_up_reply_is_recorded_not_invented(monkeypatch):
    monkeypatch.setenv("MCP_SOLVER_FEEDBACK", "1")
    r = R.run_episode(_Ep("e2"), _agent([], feedback_reply="not json"), root=_tmp())
    assert r["solver_feedback"]["episode_id"] == "e2"
    assert r["solver_feedback"]["parse_error"]


def test_flag_on_a_broken_follow_up_does_not_lose_the_episode_result(monkeypatch):
    """追加ターンでアダプタが例外を投げても、本来のエピソード結果は失われない。"""
    monkeypatch.setenv("MCP_SOLVER_FEEDBACK", "1")
    r = R.run_episode(_Ep("e3"), _agent([], boom_on_feedback=True), root=_tmp())
    assert r["success"] is True
    assert r["solver_feedback"]["episode_id"] == "e3"
    assert "solver_feedback_error" in r["solver_feedback"]


def test_the_follow_up_does_not_get_the_episodes_own_workdir(monkeypatch):
    """delivery evidence は workdir でエピソードの本来のターンに紐づけている。追加ターンに
    同じ workdir を渡すと、その紐づけが追加ターンの行を拾ってしまう -- 別ディレクトリで
    呼ぶことでそれを避ける。"""
    monkeypatch.setenv("MCP_SOLVER_FEEDBACK", "1")
    seen = []

    def fn(prompt, workdir):
        seen.append((prompt, workdir))
        return "{}" if prompt == SF.prompt() else "done"

    R.run_episode(_Ep("e4"), A.in_process(fn), root=_tmp())

    assert len(seen) == 2
    task_workdir, feedback_workdir = seen[0][1], seen[1][1]
    assert feedback_workdir != task_workdir
    assert feedback_workdir.startswith(task_workdir)


# ---- accumulation and the gate boundary -----------------------------------------------

def test_solver_feedback_entries_extracts_only_present_rows():
    rows = [{"episode_id": "e1", "solver_feedback": {"episode_id": "e1", "tool_friction": []}},
            {"episode_id": "e2"},
            {"episode_id": "e3", "solver_feedback": None}]
    assert R.solver_feedback_entries(rows) == [rows[0]["solver_feedback"]]


def test_accumulated_entries_feed_tally_and_to_hypotheses(monkeypatch):
    """収集した entries が、そのまま tally/to_hypotheses に渡せることを確認する --
    プールを終えたあとで仮説を作る、という配線の目的そのもの。"""
    monkeypatch.setenv("MCP_SOLVER_FEEDBACK", "1")
    rows = []
    for i in range(3):
        calls = []
        reply = json.dumps({"tool_friction": ["slow reads"]})
        rows.append(R.run_episode(_Ep("e%d" % i), _agent(calls, feedback_reply=reply),
                                  root=_tmp()))
    entries = R.solver_feedback_entries(rows)
    assert len(entries) == 3
    tallied = SF.tally(entries)
    hyps = SF.to_hypotheses(tallied)
    assert len(hyps) == 1
    assert hyps[0]["raised_by"] == 3
    assert sorted(hyps[0]["evidence_episodes"]) == ["e0", "e1", "e2"]


def test_a_single_complaint_across_a_pool_is_still_only_an_anecdote(monkeypatch):
    """min_episodes=2 の既定は、この配線を通しても効いたままでなければならない。"""
    monkeypatch.setenv("MCP_SOLVER_FEEDBACK", "1")
    reply = json.dumps({"tool_friction": ["one-off complaint"]})
    row = R.run_episode(_Ep("solo"), _agent([], feedback_reply=reply), root=_tmp())
    entries = R.solver_feedback_entries([row])
    assert SF.to_hypotheses(SF.tally(entries)) == []


def test_nothing_the_feedback_produces_reaches_a_gate(monkeypatch):
    """tally/to_hypotheses の出力に、判定として読める語が混じっていないこと --
    そしてこれは success/gate 系のキーとは別の場所にしか現れないことも確認する。"""
    monkeypatch.setenv("MCP_SOLVER_FEEDBACK", "1")
    rows = []
    for i in range(3):
        reply = json.dumps({"tool_friction": ["slow reads"]})
        rows.append(R.run_episode(_Ep("g%d" % i), _agent([], feedback_reply=reply),
                                  root=_tmp()))
        assert rows[-1]["success"] is True, "採点はフィードバック収集と無関係でなければならない"

    entries = R.solver_feedback_entries(rows)
    tallied = SF.tally(entries)
    hyps = SF.to_hypotheses(tallied)
    blob = json.dumps({"tally": tallied, "hypotheses": hyps})
    for word in ("keep", "reject", "verdict", "accept", "p_value", "significant"):
        assert word not in blob.lower(), "判定に読める語 %r が出力に含まれている" % word


def test_the_flag_is_recorded_in_the_experiment_fingerprint():
    """未記録のトグルは未記録の交絡変数 -- フラグは fingerprint の対象に入っていなければ
    ならない。"""
    assert "MCP_SOLVER_FEEDBACK" in EX.FINGERPRINT_ENV_KEYS
