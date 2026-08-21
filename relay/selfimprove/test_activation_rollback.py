"""適用したものを本当に戻せるか。ダッシュボードのボタンではなく、ハーネスが読むファイルで。

この機構は「作ってある」状態が長く、一度も本番の経路で使われていなかった。
巻き戻しの実演を1周させたときに、2つの欠陥が同時に出た:

  * apply/revert と台帳記録が付いていたのは `active_genome.json` -- その docstring 自身が
    「実際に読む部分は先送り」と書いている、**誰も読まないファイル**。
    ハーネスが毎回解決する `active_manifest.json` には、退避も記録も revert も無かった。
  * 退避は既存ファイルがあるときだけ書かれていた。つまり**最初の適用**
    -- 基底から進化済みへ移す、まさにその1回 -- だけが取り消せなかった。
    そしてこのリポジトリはちょうどその状態にあった(active_manifest.json 不在)。
"""
import json
import os

import pytest

from relay.selfimprove import manifest as M
from relay.selfimprove import runtime_config as RC


@pytest.fixture
def active(tmp_path, monkeypatch):
    """本番の active_manifest には絶対に触らない。

    これは仮定ではなく事故の記録: activate=True のコントローラのテストが実際に
    本番の `.fleet/selfimprove/active_manifest.json` を書き、gitignore されているので
    diff にも出ず、以後すべてのフリート走行が別の retry 予算で回っていた。
    """
    path = tmp_path / "active_manifest.json"
    monkeypatch.setenv(RC.OVERRIDE_ENV, str(path))
    monkeypatch.setattr(RC, "_note", lambda *a, **k: None)
    return path


def _v2():
    return M.apply_genome(M.base_manifest(), {"components": {"transport": "transport/v2"}})


# ---- 往復 ---------------------------------------------------------------------------------------

def test_apply_then_rollback_returns_the_harness_to_base(active):
    assert RC.component("transport") == "transport/v1"
    RC.write_active(_v2())
    assert RC.component("transport") == "transport/v2"
    assert RC.revert_active() is True
    assert RC.component("transport") == "transport/v1"


def test_rolling_back_to_nothing_removes_the_file_rather_than_writing_the_base(active):
    """基底マニフェストを書き込むと『基底で活性化済み』という状態が残る。
    それは『一度も活性化していない』とは別の状態で、
    悪い夜を取り消した運用者が求めたものではない。"""
    RC.write_active(_v2())
    assert active.exists()
    RC.revert_active()
    assert not active.exists()


def test_a_second_activation_rolls_back_to_the_first_not_to_base(active):
    """2枠。深いスタックは『何回分か巻き戻す』を誘発する。"""
    RC.write_active(_v2())
    RC.write_active(M.apply_genome(M.base_manifest(),
                                   {"components": {"transport": "transport/v1"}}))
    assert RC.component("transport") == "transport/v1"
    assert RC.revert_active() is True
    assert RC.component("transport") == "transport/v2", "基底まで戻ってしまった"


# ---- 一方通行でないこと -------------------------------------------------------------------------

def test_the_rollback_itself_can_be_rolled_back(active):
    """最初の版は .prev から戻して .prev をそのまま残したので、
    いま撤回した genome はどこにも保持されず、やり直せなかった。
    良い変更を誤って戻した運用者は、それが何だったか思い出す以外に前へ進めない。"""
    RC.write_active(_v2())
    assert RC.component("transport") == "transport/v2"
    RC.revert_active()
    assert RC.component("transport") == "transport/v1"
    assert RC.revert_active() is True
    assert RC.component("transport") == "transport/v2", "巻き戻しが一方通行"


def test_two_swaps_return_exactly_where_you_started(active):
    """トグルが対称でなければ、往復するたびに状態がずれる。"""
    RC.write_active(_v2())
    before = active.read_text(encoding="utf-8")
    RC.revert_active()
    RC.revert_active()
    assert active.read_text(encoding="utf-8") == before


def test_the_next_move_can_be_named_so_a_control_does_not_lie(active):
    """すでに巻き戻した後で『ロールバック』と書いてあるボタンは、
    押すと逆のことをする。"""
    assert RC.pending_swap() is None
    RC.write_active(_v2())
    assert RC.pending_swap() == "", "適用前は『マニフェスト無し』へ戻るはず"
    RC.revert_active()
    assert RC.pending_swap() == M.harness_id(_v2())


def test_an_unreadable_other_state_is_still_offered(active):
    """現行コードが読めないマニフェストこそ戻したくなる対象。
    id が言えないだけで、状態としては生きている。"""
    RC.write_active(_v2())
    RC.revert_active()
    active.with_suffix(".json.prev").write_text("{not json", encoding="utf-8")
    assert RC.pending_swap() == "(unreadable)"
    assert RC.revert_active() is True


def test_nothing_applied_is_not_a_successful_rollback(active):
    """False を返すこと。True を返すと、何も起きていないのに
    『戻した』と報告される -- 障害対応で最悪の嘘。"""
    assert RC.revert_active() is False


# ---- 巻き戻しが読む先は、ハーネスが読む先と同じであること ----------------------------------------

def test_the_rollback_acts_on_the_file_the_harness_actually_reads(active):
    """機構が付いていたのは active_genome.json で、そこは誰も読んでいなかった。"""
    RC.write_active(_v2())
    on_disk = json.loads(active.read_text(encoding="utf-8"))
    assert (on_disk.get("components") or {}).get("transport") == "transport/v2"
    assert RC.active_manifest(refresh=True)["components"]["transport"] == "transport/v2"


def test_the_override_is_honoured_by_the_rollback_too(active, tmp_path):
    """書きは override を見て消しは本番を見る、では『取り消し』が2つの場所を意味する。"""
    RC.write_active(_v2())
    RC.revert_active()
    assert not active.exists()
    assert not (tmp_path / "somewhere_else.json").exists()


def test_the_undo_point_is_taken_before_the_write(active):
    """書いた後に退避を取ると、退避の中身は新しいほうになる。"""
    RC.write_active(_v2())
    prev = active.with_suffix(".json.prev")
    assert prev.exists()
    assert RC.NO_MANIFEST in prev.read_text(encoding="utf-8")


def test_the_other_slot_is_written_before_the_active_one(active):
    """途中で落ちたとき、古いマニフェストが両枠にある状態のほうが、
    現行のマニフェストがどちらにも無い状態よりましである。"""
    import inspect
    src = inspect.getsource(RC.revert_active)
    assert src.index("open(prev_path") < src.index("os.remove(path)")


def test_the_backup_keeps_what_was_on_disk_rather_than_what_parses(active):
    """現行コードが検証できないマニフェストこそ、戻したくなる対象。
    parse を通してから退避すると、その1件だけ戻せない。"""
    import inspect
    src = inspect.getsource(RC._read_raw)
    assert "json" not in src, "退避が parse を通っている"


# ---- 記録 ---------------------------------------------------------------------------------------

def test_both_directions_are_recorded_in_the_authority_ledger(tmp_path, monkeypatch):
    """戻せただけでは足りない。誰が何をいつ、が残らなければ
    人間が介入した痕跡が消える。"""
    from relay.selfimprove import authority_ledger as AL
    monkeypatch.setenv(RC.OVERRIDE_ENV, str(tmp_path / "active_manifest.json"))
    monkeypatch.setattr(AL, "_path", lambda path=None: str(tmp_path / "authority.jsonl"))
    monkeypatch.setattr(AL, "_notify", lambda record: None)
    RC.write_active(_v2())
    RC.revert_active()
    events = [r.get("event") for r in AL.read(str(tmp_path / "authority.jsonl"))]
    assert AL.GENOME_APPLY in events
    assert AL.GENOME_REVERT in events
    ok, why = AL.verify(str(tmp_path / "authority.jsonl"))
    assert ok, why


def test_a_ledger_failure_does_not_fail_the_write_but_is_not_silent(active, capsys,
                                                                   monkeypatch):
    """運用者はハーネスを変えろと言い、変わった。記録の失敗でそれを巻き戻すのは違う。
    だが黙って通してもいけない -- 帰属できない活性化こそ台帳の存在理由。"""
    monkeypatch.undo()
    monkeypatch.setenv(RC.OVERRIDE_ENV, str(active))

    def boom(*a, **k):
        raise RuntimeError("ledger down")

    monkeypatch.setattr("relay.selfimprove.authority_ledger.append", boom)
    RC.write_active(_v2())
    assert RC.component("transport") == "transport/v2", "記録の失敗で書き込みが落ちた"
    assert "could not record" in capsys.readouterr().out


# ---- 人間が巻き戻せること (運用者の恒久委任の条件) ------------------------------------------------

def test_rollback_needs_no_approval_machinery_to_run(active):
    """委任の条件は『人間の介入で巻き戻しができる』こと。
    巻き戻し自体がゲートの後ろにあると、その条件が満たせない。"""
    import inspect
    src = inspect.getsource(RC.revert_active)
    for gate in ("operator_approved", "autonomy", "permits", "unlock"):
        assert gate not in src, gate


# ---- main は常に到達可能 (枝は生やさない) --------------------------------------------------------

def _r(n):
    return M.apply_genome(M.base_manifest(), {"parameters": {"max_retries": n}})


def _retries():
    return RC.active_manifest(refresh=True)["parameters"]["max_retries"]


def test_two_activations_push_the_base_out_of_both_slots(active):
    """設計の穴を、直したことではなく症状で固定する。
    v2 を当ててから v3 を当てると2枠は v3/v2 になり、
    未活性の基底はどちらからも落ちる。"""
    RC.write_active(_r(7))
    RC.write_active(_r(9))
    RC.revert_active()
    assert _retries() == 7
    RC.revert_active()
    assert _retries() == 9, "2枠は直近2つ。基底は入っていない"


def test_base_is_reachable_no_matter_how_many_activations_happened(active):
    """『出荷時に戻す』は、履歴が失ってはいけない唯一の枝。
    基底は記憶ではなく構成されるものなので、枠を1つ増やす必要はない。"""
    RC.write_active(_r(7))
    RC.write_active(_r(9))
    assert RC.reset_to_base() is True
    assert _retries() == M.base_manifest()["parameters"]["max_retries"]
    assert not active.exists(), "『未活性』ではなく『基底で活性化済み』になっている"


def test_going_home_is_itself_undoable(active):
    """帰ることだけが取り消せない唯一の手、では困る。"""
    RC.write_active(_r(9))
    RC.reset_to_base()
    assert RC.revert_active() is True
    assert _retries() == 9


def test_resetting_when_already_at_base_is_not_reported_as_a_change(active):
    assert RC.reset_to_base() is False


def test_going_home_is_not_a_third_slot(active):
    """スタックを生やすと『何回分戻すか』という問いが生まれる。
    基底は構成できるので枠は要らない。"""
    import ast
    import inspect
    # docstring の散文ではなくコード本体を見る。以前 _policy_v2 で
    # docstring 中の "random" を拾って落としたのと同じ罠。
    tree = ast.parse(inspect.getsource(RC.reset_to_base).lstrip())
    fn = tree.body[0]
    if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant):
        fn.body = fn.body[1:]
    code = ast.unparse(fn)
    assert ".prev2" not in code and "history" not in code
    assert "base_manifest" not in code, "基底を書き込んでいる (未活性とは別の状態)"
