"""L3 の policy が、スケジュール実行のどこで効くか。

`preconditions` が答えるのは「開始してよいか」だけ。走行が正当に始まっても、
終わった先に人が見るべき状態はありうる — 走行中に判定器が変わった、pass 率が
実在の変更では届かない幅で跳ねた、環境が3割を落とした、sentinel が退行した。
tripwire はその後段で、**ゲートとは別の問い**に答える（ゲートは候補について、
tripwire は走行そのものが信用できるかについて）。

もう一つは plateau。環境が完全に健全なまま、直近K件の候補が全部ゲートに落ちている
状態はありうる。`harness_feedback` は abort を見ているが、こちらは**正直な却下の連続**
を見る — 1件ずつ見ると何も問題が無いように見える形。
"""
import pytest

from relay.selfimprove import policy as POL
from relay.selfimprove import scheduler as S


def _ok_frozen(monkeypatch):
    from relay.selfimprove import frozen as F
    monkeypatch.setattr(F, "frozen_intact", lambda *a, **k: (True, []))


def _lock(tmp_path):
    return str(tmp_path / "campaign.lock")


# ---- plateau: 開始しない理由として ---------------------------------------------------------------

def test_a_run_of_rejections_stops_the_schedule(monkeypatch, tmp_path):
    """5件連続で KEEP が無いなら、次の一手は人が方向を変えること。"""
    _ok_frozen(monkeypatch)
    decisions = [{"state": "REJECT"} for _ in range(5)]
    reasons = S.preconditions(recent_decisions=decisions, lock_path=_lock(tmp_path),
                              budget_candidates=5)
    assert any("plateau" in r for r in reasons), reasons


def test_one_keep_anywhere_in_the_window_resets_it(monkeypatch, tmp_path):
    """窓の中に1件でも通ったものがあれば plateau ではない -- 停滞と不運は別。"""
    _ok_frozen(monkeypatch)
    decisions = [{"state": "REJECT"}] * 3 + [{"state": "KEEP"}] + [{"state": "REJECT"}]
    reasons = S.preconditions(recent_decisions=decisions, lock_path=_lock(tmp_path),
                              budget_candidates=5)
    assert not any("plateau" in r for r in reasons), reasons


def test_a_short_history_does_not_plateau(monkeypatch, tmp_path):
    """2件の却下は停滞ではない。K に満たない履歴で止めると、始まる前に止まる。"""
    _ok_frozen(monkeypatch)
    reasons = S.preconditions(recent_decisions=[{"state": "REJECT"}] * 2,
                              lock_path=_lock(tmp_path), budget_candidates=5)
    assert not any("plateau" in r for r in reasons), reasons


def test_inconclusive_is_not_a_pass(monkeypatch, tmp_path):
    """INCONCLUSIVE は「通った」ではない。通ったことにすると停滞が永久に隠れる。"""
    _ok_frozen(monkeypatch)
    reasons = S.preconditions(recent_decisions=[{"state": "INCONCLUSIVE"}] * 5,
                              lock_path=_lock(tmp_path), budget_candidates=5)
    assert any("plateau" in r for r in reasons), reasons


# ---- tripwire: 走行の後で -----------------------------------------------------------------------

def test_a_clean_result_fires_nothing_and_says_what_it_could_check(monkeypatch):
    """「何も鳴らなかった」と「何も評価できなかった」は別。安心してよいのは前者だけ。"""
    _ok_frozen(monkeypatch)
    got = S.tripwires_after({"pass_at_1": 0.7, "slice_ids": ["a", "b"],
                             "on": {"infra_ids": []}, "sentinel": {"regressed": False}})
    assert got["fired"] == [] and got["halt"] is False
    # トリップワイヤ名であって状態キーではない。`len(state)` で数えると `prev_pass` を
    # 1本と数え、「何を検査したか」を言うための唯一のフィールドが3本と報告していた。
    assert set(got["evaluated"]) == {"frozen_changed", "implausible_jump",
                                     "infra_spike", "sentinel_regressed"}


def test_a_judge_that_changed_during_the_run_fires(monkeypatch):
    """ゲートは候補について答える。走行中に判定器が変われば、
    候補が正しく却下されていても、その走行は信用できない。"""
    from relay.selfimprove import frozen as F
    monkeypatch.setattr(F, "frozen_intact", lambda *a, **k: (False, ["guards.py"]))
    got = S.tripwires_after({})
    assert "frozen_changed" in got["fired"] and got["halt"] is True


def test_an_infra_spike_fires(monkeypatch):
    _ok_frozen(monkeypatch)
    got = S.tripwires_after({"slice_ids": ["a", "b", "c", "d"],
                             "on": {"infra_ids": ["a", "b"]}})
    assert "infra_spike" in got["fired"]


def test_a_total_abort_is_a_total_loss_not_an_unknown_rate(monkeypatch):
    """割る対象が無い abort を「率は不明」にすると、最悪の走行が一番静かになる。"""
    _ok_frozen(monkeypatch)
    got = S.tripwires_after({"infra": {"aborted": True}})
    assert got["state"]["infra_rate"] == 1.0
    assert "infra_spike" in got["fired"]


def test_a_sentinel_regression_fires(monkeypatch):
    _ok_frozen(monkeypatch)
    got = S.tripwires_after({"sentinel": {"regressed": True}})
    assert "sentinel_regressed" in got["fired"]


def test_an_absent_measurement_never_becomes_a_fired_alarm(monkeypatch):
    """部分的な状態で鳴らせると、測っていないことが警報になる。"""
    _ok_frozen(monkeypatch)
    got = S.tripwires_after({})
    assert got["fired"] == []
    assert got["evaluated"] == ["frozen_changed"], "測れないものまで評価している"
    assert got["unreadable_inputs"], "何が読めなかったかを言っていない"


def test_the_scheduled_run_reports_tripwires_rather_than_acting_on_them(monkeypatch,
                                                                       tmp_path):
    """止めて言うのが仕事。何をするかは人が決める。"""
    _ok_frozen(monkeypatch)
    out = S.nightly(archive_path=str(tmp_path / "e.jsonl"), budget_candidates=1,
                    lock_path=_lock(tmp_path))
    assert "tripwires" in out
    assert set(out["tripwires"]) == {"fired", "halt", "state", "evaluated",
                                     "unreadable_inputs", "note"}
    for word in ("rollback", "revert", "disabled", "activated"):
        assert word not in str(out["tripwires"]).lower()


def test_five_passes_are_not_a_plateau(monkeypatch, tmp_path):
    """逆方向。最初のアダプタは `keep` を書き、policy は `kept` を読んでいたので、
    全判定が「落ちた」と読まれ、**5連続 KEEP でも plateau が鳴った** --
    スケジュールを恒久的に止める前提条件が、所見の顔をして出る形。
    却下方向のテストだけでは通り抜ける。"""
    _ok_frozen(monkeypatch)
    reasons = S.preconditions(recent_decisions=[{"state": "KEEP"}] * 5,
                              lock_path=_lock(tmp_path), budget_candidates=5)
    assert not any("plateau" in r for r in reasons), reasons


def test_the_adapter_speaks_the_flag_policy_reads():
    """綴りの取り違えは、片方向のテストでは見えない。"""
    assert POL._keep_flag({"kept": True}) is True
    assert POL._keep_flag({"keep": True}) is False, (
        "policy が `keep` を読むようになったなら、scheduler のアダプタも直すこと")


# ---- レビューが見つけた「配線したつもり」の欠陥 -------------------------------------------------

def test_the_archive_key_the_scheduler_reads_is_the_one_the_archive_writes():
    """`verdict` を読んでいたが、アーカイブが書くのは `gate_verdict`。
    全判定が "" になり、plateau は5行以上のアーカイブで（5連続 KEEP でも）鳴り、
    同時に `HF.observe` も全状態ゼロで死ぬ。**直したと書いた欠陥の1つ隣**。"""
    import inspect

    from relay.selfimprove import archive as A
    from relay.selfimprove import scheduler as S
    assert 'e.get("gate_verdict")' in inspect.getsource(S.nightly)
    assert '"gate_verdict"' in inspect.getsource(A.Archive.add)


def test_a_real_archive_of_keeps_does_not_block_the_schedule(monkeypatch, tmp_path):
    """実アーカイブで確かめる -- キー名の取り違えは、合成辞書では見えない。"""
    from relay.selfimprove import archive as A
    from relay.selfimprove import scheduler as S
    _ok_frozen(monkeypatch)
    arc = A.Archive(path=str(tmp_path / "entries.jsonl"))
    for i in range(6):
        arc.add({"parameters": {"max_retries": 10 + i}},
                slice_ids=["e1"], pass_at_1=0.5, ci=(0.4, 0.6), gate_verdict="KEEP")
    out = S.nightly(archive_path=arc.path, budget_candidates=1,
                    lock_path=_lock(tmp_path))
    assert not any("plateau" in r for r in (out.get("blocked_by") or [])), out.get("blocked_by")


def test_an_uncleared_tripwire_blocks_the_following_night(tmp_path, monkeypatch):
    """halt が返り値の dict にしか無いなら halt ではない。次の夜こそ誰も見ていない。"""
    from relay.selfimprove import scheduler as S
    _ok_frozen(monkeypatch)
    lock = _lock(tmp_path)
    S._record_halt({"fired": ["sentinel_regressed"], "state": {}}, lock_path=lock)
    reasons = S.preconditions(lock_path=lock, budget_candidates=5)
    assert any("tripwire fired on an earlier run" in r for r in reasons), reasons


def test_a_malformed_result_section_does_not_destroy_the_report(monkeypatch):
    """真値の文字列は `or {}` を通り、`"regressed" in "nothing regressed"` が
    部分一致し、次の行で str に .get して AttributeError -- キャンペーンの**後**に
    投げられ、その夜の報告ごと壊す。"""
    from relay.selfimprove import scheduler as S
    _ok_frozen(monkeypatch)
    got = S.tripwires_after({"sentinel": "nothing regressed", "infra": "fine"})
    assert got["fired"] == [] and "sentinel_regressed" not in got["evaluated"]


def test_both_arms_infra_failures_count(monkeypatch):
    """候補側だけ数えると率が半分になる。baseline 側の abort も同じ病んだハーネス。"""
    from relay.selfimprove import scheduler as S
    _ok_frozen(monkeypatch)
    got = S.tripwires_after({"slice_ids": ["a", "b", "c", "d"],
                             "on": {"infra_ids": ["a"]}, "off": {"infra_ids": ["b"]}})
    assert got["state"]["infra_rate"] == 0.5


def test_an_abort_after_the_slice_was_chosen_is_still_a_total_loss(monkeypatch):
    from relay.selfimprove import scheduler as S
    _ok_frozen(monkeypatch)
    got = S.tripwires_after({"slice_ids": ["a", "b"], "on": {"infra_ids": []},
                             "infra": {"aborted": True}})
    assert got["state"]["infra_rate"] == 1.0


def test_the_replay_coreset_reads_where_the_controller_writes(tmp_path):
    """`entry["results"]["episodes"]` はどの行にも無く、replay 集合は常に空だった。
    `.entries()` の AttributeError と同じ種類で、静かに返る分だけ悪い。"""
    import inspect

    from relay.selfimprove import scheduler as S
    body = " ".join(l for l in inspect.getsource(S._recent_failures).splitlines()
                    if not l.strip().startswith("#"))
    assert "episode_results" in body
    assert 'get("results")' not in body, "コメントではなくコードが古いキーを読んでいる"
