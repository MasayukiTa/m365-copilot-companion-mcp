"""ダッシュボードの枝ブロック。この画面が防ぐ失敗は1つに絞られる。

「誰も名前を付けていないハーネスの上で走っている」は他の症状を持たない --
フリートは動き、走行は完了し、数字は数字に見える。だから
『いま何が走っているか』を名前に解決して出すことが、唯一の検出器になる。

もう1つは p-hacking。最良の1回だけを『もう一度走らせる』の隣に出すと、
運用者が欲しい答えを製造する装置になる。全試行が出ていて初めて歯止めになる。
"""
import pytest

from relay.selfimprove import branches as BR
from relay.selfimprove import dashboard as D


@pytest.fixture
def env(tmp_path, monkeypatch):
    from relay.selfimprove import archive as A
    from relay.selfimprove import compare as C
    monkeypatch.setenv("MCP_SELFIMPROVE_BRANCHES", str(tmp_path / "branches.json"))
    monkeypatch.setenv("MCP_SELFIMPROVE_COMPARE_QUEUE", str(tmp_path / "queue.jsonl"))
    monkeypatch.setenv("MCP_SELFIMPROVE_COMPARISONS", str(tmp_path / "comparisons.jsonl"))
    monkeypatch.setattr(BR, "_record", lambda *a, **k: None)
    arc = A.Archive(path=str(tmp_path / "entries.jsonl"))
    monkeypatch.setattr(A, "Archive", lambda *a, **k: arc)
    from relay.selfimprove import runtime_config as RC
    monkeypatch.setenv(RC.OVERRIDE_ENV, str(tmp_path / "active_manifest.json"))
    monkeypatch.setattr(RC, "_note", lambda *a, **k: None)
    return arc, C


def _mk(arc, label, genome):
    gid = arc.add(dict(genome), slice_ids=["e1"], pass_at_1=0.5, ci=(0.4, 0.6),
                  gate_verdict="KEEP")
    BR.create(label, gid, archive=arc)
    return gid


# ---- 走っているものが名前で出ること ---------------------------------------------------------------

def test_the_running_harness_is_named(env):
    arc, _ = env
    from relay.selfimprove import runtime_config as RC
    _mk(arc, "fast", {"components": {"transport": "transport/v2"}})
    section = D._branch_section()
    assert section["active"]["kind"] == "base"

    RC.write_active(BR.resolve("fast", archive=arc)["manifest"])
    section = D._branch_section()
    assert section["active"]["kind"] == "branch"
    assert section["active"]["label"] == "fast"


def test_an_unnamed_harness_is_reported_loudly_not_rounded_to_base(env):
    """base に丸めた瞬間、この失敗モードは検出不能になる。"""
    arc, _ = env
    from relay.selfimprove import manifest as M
    from relay.selfimprove import runtime_config as RC
    RC.write_active(M.apply_genome(M.base_manifest(), {"parameters": {"max_retries": 3}}))
    section = D._branch_section()
    assert section["active"]["kind"] == "unnamed"
    text = "\n".join(D._render_branches({"branches": section}))
    assert "UNNAMED HARNESS" in text
    assert "nobody named" in text


def test_the_running_line_comes_first_in_the_block(env):
    """脚注に置くと、探しに行った人にしか届かない。"""
    arc, _ = env
    _mk(arc, "fast", {"components": {"transport": "transport/v2"}})
    lines = D._render_branches({"branches": D._branch_section()})
    body = [l for l in lines if l.strip() and not l.startswith("-") and l != "BRANCHES"]
    assert body[0].startswith("running now")


# ---- 全試行が出ること ---------------------------------------------------------------------------

def test_every_comparison_attempt_is_listed(env):
    arc, C = env
    _mk(arc, "fast", {"components": {"transport": "transport/v2"}})
    _mk(arc, "slow", {"components": {"transport": "transport/v1"}})
    req = {"id": "cmp-1", "a": {"label": "fast"}, "b": {"label": "slow"}}
    good = {"memory_gain_mb": 400, "control": {"done": 4}, "candidate": {"done": 4}}
    small = {"memory_gain_mb": 10, "control": {"done": 4}, "candidate": {"done": 4}}
    C.record(req, small, small, C.decide(small, small))
    C.record(req, good, good, C.decide(good, good))
    section = D._branch_section()
    assert [c["verdict"] for c in section["comparisons"]] == ["INCONCLUSIVE", "A"]


def test_an_inconclusive_does_not_look_like_a_win(env):
    """勝ちと同じ見た目で出すと、閾値未満の差が発見として読まれる。"""
    arc, C = env
    _mk(arc, "fast", {"components": {"transport": "transport/v2"}})
    _mk(arc, "slow", {"components": {"transport": "transport/v1"}})
    req = {"id": "cmp-1", "a": {"label": "fast"}, "b": {"label": "slow"}}
    small = {"memory_gain_mb": 10, "control": {"done": 4}, "candidate": {"done": 4}}
    good = {"memory_gain_mb": 400, "control": {"done": 4}, "candidate": {"done": 4}}
    C.record(req, small, small, C.decide(small, small))
    C.record(req, good, good, C.decide(good, good))
    text = "\n".join(D._render_branches({"branches": D._branch_section()}))
    win = [l for l in text.split("\n") if " A " in l or l.rstrip().endswith("A")]
    assert "==" in text and "->" in text, text
    assert text.index("==") < text.index("->"), "INCONCLUSIVE と勝ちが同じ印になっている"


def test_a_withdrawn_verdict_is_shown_as_withdrawn(env):
    arc, C = env
    _mk(arc, "fast", {"components": {"transport": "transport/v2"}})
    _mk(arc, "slow", {"components": {"transport": "transport/v1"}})
    req = {"id": "cmp-1", "a": {"label": "fast"}, "b": {"label": "slow"}}
    good = {"memory_gain_mb": 400, "control": {"done": 4}, "candidate": {"done": 4}}
    C.record(req, good, good, C.decide(good, good))
    C.withdraw("cmp-1", "one ordering never ran")
    section = D._branch_section()
    assert section["comparisons"][0]["verdict"] == "WITHDRAWN"
    assert section["comparisons"][0]["original_verdict"] == "A"


def test_the_withdrawal_row_itself_is_not_listed_as_an_attempt(env):
    """撤回は試行ではない。試行として数えると回数が水増しされる。"""
    arc, C = env
    _mk(arc, "fast", {"components": {"transport": "transport/v2"}})
    _mk(arc, "slow", {"components": {"transport": "transport/v1"}})
    req = {"id": "cmp-1", "a": {"label": "fast"}, "b": {"label": "slow"}}
    good = {"memory_gain_mb": 400, "control": {"done": 4}, "candidate": {"done": 4}}
    C.record(req, good, good, C.decide(good, good))
    C.withdraw("cmp-1", "because")
    assert len(D._branch_section()["comparisons"]) == 1


# ---- 計器の範囲が画面に出ること -------------------------------------------------------------------

def test_the_instrument_scope_is_on_the_screen(env):
    """20分待ってから『測れませんでした』ではなく、押す前に読めること。"""
    arc, _ = env
    section = D._branch_section()
    assert section["instrument"]["measures"] == ["transport"]
    assert section["instrument"]["note"]


# ---- 壊れた状態でも描けること ---------------------------------------------------------------------

def test_a_broken_ref_is_flagged_rather_than_hidden(env):
    arc, _ = env
    _mk(arc, "fast", {"components": {"transport": "transport/v2"}})
    refs = BR.read()
    refs["ghost"] = {"genome_id": "nope", "created_at": 0, "last_run_at": None, "note": ""}
    BR._write(refs)
    section = D._branch_section()
    broken = [b for b in section["branches"] if not b["resolves"]]
    assert [b["label"] for b in broken] == ["ghost"]
    assert "BROKEN REF" in "\n".join(D._render_branches({"branches": section}))


def test_a_missing_ledger_produces_an_empty_section_not_an_exception(tmp_path, monkeypatch):
    """インシデント中に1つの台帳が無いだけで描画できないダッシュボードは、誰も信用しない。"""
    monkeypatch.setenv("MCP_SELFIMPROVE_BRANCHES", str(tmp_path / "nope.json"))
    monkeypatch.setenv("MCP_SELFIMPROVE_COMPARISONS", str(tmp_path / "nope.jsonl"))
    section = D._branch_section()
    assert section["branches"] == []
    assert isinstance(D._render_branches({"branches": section}), list)


def test_the_section_reaches_dashboard_state():
    assert "branches" in D.dashboard_state()


def test_the_dashboard_never_runs_a_comparison():
    """20分かかるジョブを HTTP リクエストや描画から同期で起動する経路を作らない。"""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(D).lstrip())
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
    code = ast.unparse(tree)
    for forbidden in ("compare.run", "C.run(", "enqueue(", "take_lock", "write_active"):
        assert forbidden not in code, forbidden
