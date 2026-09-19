from relay.review_resilience import (
    RecoveryAction,
    RefusalCause,
    TaskEnvelope,
    diagnose_after_fresh_replay,
    looks_like_policy_refusal,
    same_task_envelope,
)
from relay.relay_fleet import RelayWorker


def _env(**overrides):
    data = dict(
        task_id="review-1",
        parent_task_id=None,
        campaign_id="campaign-1",
        role="producer",
        goal_text="authorized review",
        cwd="C:/repo",
        metadata={
            "scope": ["a.py"],
            "output_contract": "FINDINGS",
            "authorization_preamble": "authorized",
        },
    )
    data.update(overrides)
    return TaskEnvelope(**data)


def test_policy_refusal_detection_is_specific():
    assert looks_like_policy_refusal("このリクエストには対応できません")
    assert looks_like_policy_refusal("I cannot assist with that request.")
    assert not looks_like_policy_refusal("Network error. Please try again later.")
    assert not looks_like_policy_refusal("No tools are assigned to this session.")
    assert not looks_like_policy_refusal("")


def test_task_envelope_hash_ignores_only_session_attempt():
    a = _env(session_attempt=0)
    b = _env(session_attempt=1)
    assert a.goal_hash == b.goal_hash
    assert same_task_envelope(a, b)
    c = _env(metadata={**a.metadata, "scope": ["b.py"]})
    assert c.goal_hash != a.goal_hash


def test_diagnosis_after_fresh_replay():
    recovered = diagnose_after_fresh_replay(True, False, True, False)
    assert recovered.cause == RefusalCause.SESSION_STATE
    assert recovered.action == RecoveryAction.FRESH_REPLAY

    refused = diagnose_after_fresh_replay(True, True, False, False)
    assert refused.cause == RefusalCause.TASK_CONTENT
    assert refused.action == RecoveryAction.DECOMPOSE


def test_worker_policy_refusal_replays_once_then_marks_content_refused(monkeypatch):
    goal = {
        "text": "authorized review",
        "cwd": "C:/repo",
        "task_id": "review-1",
        "campaign_id": "campaign-1",
        "role": "producer",
        "metadata": {"scope": ["a.py"], "output_contract": "FINDINGS"},
    }
    worker = RelayWorker(goal, "w0", resilience_profile="review", max_fresh_replays=1)
    called = []

    def fake_replay():
        called.append(True)
        worker.fresh_replay_count = 1
        return True

    monkeypatch.setattr(worker, "_start_fresh_replay", fake_replay)
    worker._decide("このリクエストには対応できません")
    assert called == [True]
    assert worker.outcome is None

    worker._decide("I cannot assist with that request")
    assert worker.status == "content_refused"
    assert worker.outcome == "CONTENT_REFUSED"
    assert worker.recovery_result == "needs_decomposition"


def test_worker_profile_off_keeps_legacy_decision_path(monkeypatch):
    worker = RelayWorker("ordinary goal", "w0", resilience_profile="off",
                         max_fresh_replays=0, max_no_progress=99)
    monkeypatch.setattr(worker, "_start_fresh_replay", lambda: (_ for _ in ()).throw(
        AssertionError("must not replay")))
    worker._decide("I cannot assist with that request")
    assert worker.status == "ready"
    assert worker.outcome is None


def test_resilient_transcript_is_attempt_scoped_and_replay_is_hard_capped(tmp_path):
    worker = RelayWorker(
        {"text": "goal", "task_id": "t", "campaign_id": "c"}, "w0",
        transcript_dir=str(tmp_path), run_id="run", resilience_profile="review",
        max_fresh_replays=99,
    )
    assert worker.transcript.endswith("run_w0_a0.jsonl")
    assert worker.attempt_transcripts == [worker.transcript]
    assert worker.max_fresh_replays == 1


# ---- 設計されて、作られていない半分 ---------------------------------------------------------

def _producible():
    """`diagnose_after_fresh_replay` が実際に返し得る (cause, action) の全体。

    **推論ではなく網羅。** 引数は真偽値4つなので組み合わせは16通りしかない。全部回す。
    """
    import itertools

    causes, actions = set(), set()
    for bits in itertools.product((False, True), repeat=4):
        d = diagnose_after_fresh_replay(*bits)
        causes.add(d.cause)
        actions.add(d.action)
    return causes, actions


#: 生成する経路が無いことが実測で確認済みの列挙子。**ここに足すのは設計判断であって
#: テストを通す作業ではない** — 何も設定しない状態は、設定されないまま増える。
#: relay_fleet.py:87 に同じ規律が先例として書かれている:「A STATUS BELONGS HERE ONLY IF
#: SOMETHING SETS IT。`unresolved_refusal` はこのタプル・下のラベル表・コックピットの
#: ピル表・outcome enum の**5箇所**にあって、何も設定していなかった」。
NOT_PRODUCIBLE_CAUSES = {RefusalCause.CAPABILITY, RefusalCause.OUTPUT_FILTER,
                         RefusalCause.CONTEXT_CONTAMINATION}
NOT_PRODUCIBLE_ACTIONS = {RecoveryAction.ALTERNATE_EXECUTOR, RecoveryAction.REDACT_OUTPUT}


def test_every_cause_is_either_producible_or_listed_as_not():
    """**7つのうち4つしか出ない。** 2026-09-20 に16通りを全部回して実測。

    残る3つは語彙としては存在し、`CAPABILITY_FAILURE_MARKERS` と `OUTPUT_FILTER_MARKERS`、
    そしてそれを読む `looks_like_capability_failure` / `looks_like_output_filter` まで
    書かれている — **が、`diagnose_after_fresh_replay` はテキストを受け取らない**ので、
    どの述語も呼ばれようがなく、どの原因も返りようがない。

    このテストは配線しない。**新しい列挙子が黙って増えることだけを止める。**
    """
    causes, _ = _producible()
    unlisted = set(RefusalCause) - causes - NOT_PRODUCIBLE_CAUSES
    assert not unlisted, (
        "no input to diagnose_after_fresh_replay produces these, and nothing says so: %s"
        % sorted(c.value for c in unlisted))


def test_every_action_is_either_producible_or_listed_as_not():
    """原因だけでなく行動も同じ。`ALTERNATE_EXECUTOR` と `REDACT_OUTPUT` は、
    ちょうど上の2つの原因と対になる行動で、両方とも出ない。"""
    _, actions = _producible()
    unlisted = set(RecoveryAction) - actions - NOT_PRODUCIBLE_ACTIONS
    assert not unlisted, (
        "no input produces these actions, and nothing says so: %s"
        % sorted(a.value for a in unlisted))


def test_the_not_producible_lists_do_not_outlive_the_gap():
    """**片方向のラチェットでは足りない。** 到達不能を記録した表は、到達可能になった
    瞬間から嘘になる — 今日1日で4回踏んだ形（払い終わった在庫項目、直った部分グラフ、
    旧設計を主張するテスト名、振る舞いより長生きした散文）。
    誰かがこの半分を配線したら、ここが落ちて表を消せと言う。"""
    causes, actions = _producible()
    assert not (causes & NOT_PRODUCIBLE_CAUSES), (
        "these are produced now -- remove them from NOT_PRODUCIBLE_CAUSES: %s"
        % sorted(c.value for c in causes & NOT_PRODUCIBLE_CAUSES))
    assert not (actions & NOT_PRODUCIBLE_ACTIONS), (
        "these are produced now -- remove them from NOT_PRODUCIBLE_ACTIONS: %s"
        % sorted(a.value for a in actions & NOT_PRODUCIBLE_ACTIONS))


def test_the_four_that_do_occur_are_all_four():
    """逆側。4つのうち1つでも出なくなったら、分岐が死んでいる。
    TRANSIENT と UNKNOWN は 2026-09-19 まで**本番では**出せなかった（呼び出し元が
    `fresh_was_transient_error=False` をリテラルで渡していた）が、関数自体は返せた —
    その差がこのテストでは見えないことを承知で置いている。本番側は
    relay/test_a_worker_that_dies_after_a_fresh_replay_says_why.py が見る。"""
    causes, actions = _producible()
    assert causes == set(RefusalCause) - NOT_PRODUCIBLE_CAUSES
    assert actions == set(RecoveryAction) - NOT_PRODUCIBLE_ACTIONS
