"""ラダーは「どのガードが適用されるか」を決める。ならばループ自身が動かせてはならない。

ブリーフは「無制限の自律的ソースコード改変へ直接跳ぶな」と書いている。実際にその跳躍が
起きる形は「跳ぼうと決める」ことではなく、レベル D に「チェックが緑なら自分の PR を
マージする」便利関数が1つ足されることである。だから merge と無審査の source 適用は
高いレベルで到達可能なのではなく、全レベルから欠けている。
"""
import pytest

from relay.selfimprove import autonomy as A
from relay.selfimprove import manifest as M
from relay.selfimprove import scheduler as S


# ---- what each rung grants -------------------------------------------------------------------

def test_the_four_rungs_are_the_ones_the_brief_names():
    assert A.LEVELS == ("A", "B", "C", "D")


def test_level_a_evaluates_but_does_not_start_or_activate():
    """A は「人が実験を開始し、人が勝者を承認する」。"""
    assert A.permits("A", A.EVALUATE)
    assert not A.permits("A", A.START_EXPERIMENT)
    assert not A.permits("A", A.ACTIVATE_CONFIG, change_kind="parameters",
                         gates_all_passed=True)


def test_level_b_starts_and_evaluates_but_a_human_approves_activation():
    assert A.permits("B", A.START_EXPERIMENT)
    assert not A.permits("B", A.ACTIVATE_CONFIG, change_kind="parameters",
                         gates_all_passed=True)


def test_level_c_activates_typed_changes_that_passed_every_gate():
    assert A.permits("C", A.ACTIVATE_CONFIG, change_kind="parameters",
                     gates_all_passed=True)


def test_level_d_opens_a_pull_request_and_grants_everything_below():
    assert A.permits("D", A.OPEN_SOURCE_PR)
    assert A.granted("D") == {A.EVALUATE, A.START_EXPERIMENT, A.ACTIVATE_CONFIG,
                              A.OPEN_SOURCE_PR}


# ---- what no rung grants -----------------------------------------------------------------------

def test_no_level_activates_source_without_review_or_merges():
    """D で到達可能かどうかが、誰かの設計記憶ではなくコードで答えられること。"""
    for level in A.LEVELS:
        assert not A.permits(level, A.ACTIVATE_SOURCE), level
        assert not A.permits(level, A.MERGE_SOURCE_PR), level
    with pytest.raises(A.AutonomyError) as exc:
        A.require("D", A.MERGE_SOURCE_PR)
    assert "unrestricted source-code mutation" in str(exc.value)


def test_an_unknown_level_is_the_floor_not_the_top():
    """設定ファイルの綴り間違いは、自律を1晩失わせてよいが、与えてはならない。"""
    assert A.normalise("Q") == "A"
    assert A.normalise(None) == "A"
    assert not A.permits("Q", A.START_EXPERIMENT)


# ---- level C is typed/config-only, and only on a real all-clear ------------------------------

def test_level_c_refuses_a_change_that_is_not_typed_config():
    with pytest.raises(A.AutonomyError) as exc:
        A.require("C", A.ACTIVATE_CONFIG, change_kind="source", gates_all_passed=True)
    assert "typed/config-only" in str(exc.value)


def test_nobody_said_is_not_everything_passed():
    """`if not gates_all_passed` だと None が False と同じになる。
    無人運転は、まさにその差が見えなくなる場所。"""
    assert not A.permits("C", A.ACTIVATE_CONFIG, change_kind="parameters",
                         gates_all_passed=None)
    with pytest.raises(A.AutonomyError) as exc:
        A.require("C", A.ACTIVATE_CONFIG, change_kind="parameters", gates_all_passed=None)
    assert "'nobody said' is not 'everything passed'" in str(exc.value)


def test_a_truthy_non_true_value_does_not_count_as_all_clear():
    for value in ("yes", 1, [1]):
        assert not A.permits("C", A.ACTIVATE_CONFIG, change_kind="parameters",
                             gates_all_passed=value), value


# ---- moving between rungs ----------------------------------------------------------------------

def test_a_raise_needs_an_operator():
    """レベルを自分で上げられるなら、そのレベルが支配する全ガードが助言になる。"""
    with pytest.raises(A.AutonomyError) as exc:
        A.raise_to("B", "C", evidence="two weeks of approved winners")
    assert "needs an operator" in str(exc.value)


def test_a_raise_cannot_skip_a_rung():
    """A から C への跳躍は、間の期間 -- システムが提案し人が個別に承認し、
    その提案が良かったか観察できる期間 -- を飛ばす。その期間こそが昇格の根拠。"""
    with pytest.raises(A.AutonomyError) as exc:
        A.raise_to("A", "C", operator_approved=True, evidence="looks fine")
    assert "skips B" in str(exc.value)


def test_a_raise_must_cite_something():
    with pytest.raises(A.AutonomyError) as exc:
        A.raise_to("B", "C", operator_approved=True)
    assert "cites no evidence" in str(exc.value)


def test_a_properly_evidenced_single_step_raise_is_allowed():
    assert A.raise_to("B", "C", operator_approved=True,
                      evidence="14 nights at B, every winner approved, no revert") == "C"


def test_lowering_needs_nobody():
    """午前2時に tripwire が鳴ったとき、誰かが起きるのを待たずに B へ戻せること。
    不在の人にしか締められないガードは、緩んだままのガード。"""
    assert A.lower_to("D", "B") == "B"
    with pytest.raises(A.AutonomyError):
        A.lower_to("B", "D")


def test_raise_to_refuses_to_be_used_for_lowering():
    with pytest.raises(A.AutonomyError) as exc:
        A.raise_to("C", "B", operator_approved=True, evidence="x")
    assert "not above" in str(exc.value)


# ---- the rung is not a coordinate the loop can tune ------------------------------------------

def test_autonomy_is_not_an_evolvable_component():
    """provenance と同じ理由。段を自分で動かせるなら、段が支配するガードは一手で助言になる。"""
    assert "autonomy" in M.FORBIDDEN_COMPONENTS
    with pytest.raises(M.ManifestError):
        M.apply_genome(M.base_manifest(), {"components": {"autonomy": "autonomy/level_d"}})


# ---- and it is checked at the sink, not merely described --------------------------------------

def test_a_scheduled_run_at_level_a_is_a_contradiction_not_a_stricter_setting():
    """A は「人が実験を開始する」。夜間キャンペーンはその定義に反する。"""
    reasons = S.preconditions(budget_candidates=5, level="A",
                              lock_path=str(_tmp_lock()))
    assert any("does not permit the system to start" in r for r in reasons)


def test_self_activation_begins_at_c_and_the_reason_says_so():
    lock = str(_tmp_lock())
    at_b = S.preconditions(budget_candidates=5, activate=True, level="B", lock_path=lock)
    assert any("self-activation begins at C" in r for r in at_b)
    assert S.preconditions(budget_candidates=5, activate=True, level="C",
                           lock_path=lock) == []


def test_level_b_with_an_operators_approval_is_still_allowed():
    """段は毎回の承認を置き換えない。B で人が承認するのが、ブリーフの言う B の姿。"""
    assert S.preconditions(budget_candidates=5, activate=True, level="B",
                           operator_approved_activation=True,
                           lock_path=str(_tmp_lock())) == []


def _tmp_lock():
    import os
    import tempfile
    return os.path.join(tempfile.mkdtemp(prefix="ladder_"), "campaign.lock")


# ---- reporting -----------------------------------------------------------------------------------

def test_a_status_line_names_what_is_never_allowed_too():
    """許可されたものだけを並べる状態表示は、D において『あとどれだけ先があるか』と読める。"""
    got = A.describe("D")
    assert got["level"] == "D" and got["next"] is None
    assert set(got["never"]) == {A.ACTIVATE_SOURCE, A.MERGE_SOURCE_PR}
    assert A.describe("A")["next"] == "B"


# ---- 呼ばれないガバナンス・コードは、不在より悪い -------------------------------------------------

def test_raise_to_has_no_production_caller():
    """`operator_approved` は呼び出し側が主張する真偽値で、誰でも True を渡せる。
    このまま配線すると「明らかなスタブ」が「効いているように見える統制」に変わり、
    後から読む人と監査がそれを本物だと受け取る。

    配線したくなったら、まずこのテストを消すことになる -- そのとき raise_to の
    docstring を読むことになる、というのがこのテストの仕事。
    """
    import os
    import re
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    hits = []
    for base, dirs, files in os.walk(root):
        # `.claude` を除くのは体裁のためではない。並行セッションはこの下の worktree に
        # リポジトリ全体を複製するので、走査が「別セッションの作業中コピー」を本番コードと
        # して数え、こちらが何も触っていないのに落ちる。実際そうなった。
        dirs[:] = [d for d in dirs
                   if d not in (".git", ".venv", "node_modules", "__pycache__",
                                ".fleet", ".claude")]
        for name in files:
            if not name.endswith(".py") or name.startswith("test_"):
                continue
            path = os.path.join(base, name)
            try:
                src = open(path, encoding="utf-8").read()
            except Exception:
                continue
            if os.path.abspath(path) == os.path.abspath(A.__file__):
                continue                      # 定義そのもの
            if re.search(r"\braise_to\s*\(", src):
                hits.append(os.path.relpath(path, root))
    assert not hits, (
        "raise_to に本番の呼び出し元ができている: %s。\n"
        "配線する前に docstring を読むこと -- 呼び出し側が主張する承認は承認ではない" % hits)


def test_raise_to_says_it_is_not_a_mechanism():
    doc = A.raise_to.__doc__ or ""
    assert "NOT A MECHANISM" in doc
    assert "governance_of_promotion" in doc
