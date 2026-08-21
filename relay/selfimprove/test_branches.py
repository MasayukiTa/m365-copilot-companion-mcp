"""枝 = archive 行への名前付き可変 ref。git がすでに解いた形をそのまま借りる。

このモジュールが防がなければならない失敗は1つ:
「誰も覚えていない枝の上で走っている」。これは他の症状を持たない --
フリートは動き、走行は完了し、数字は数字に見える。
だから describe_active() が『名前なし』を明示的に返すことが、唯一の検出器になる。
"""
import json
import os

import pytest

from relay.selfimprove import archive as A
from relay.selfimprove import branches as B
from relay.selfimprove import manifest as M


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_SELFIMPROVE_BRANCHES", str(tmp_path / "branches.json"))
    monkeypatch.setattr(B, "_record", lambda *a, **k: None)
    return A.Archive(path=str(tmp_path / "entries.jsonl"))


def _code_of(obj):
    """Source with docstrings stripped.

    Written because the same mistake was made three times in one day: a test that greps the
    source for a forbidden name matches the COMMENT explaining why the name is forbidden.
    Assertions about what code does have to read code.
    """
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(obj).lstrip())
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr)                 and isinstance(body[0].value, ast.Constant)                 and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _add(arc, genome, verdict="KEEP"):
    return arc.add(dict(genome), slice_ids=["e1"], pass_at_1=0.5, ci=(0.4, 0.6),
                   gate_verdict=verdict)


def _v2(arc, verdict="KEEP"):
    return _add(arc, {"components": {"transport": "transport/v2"}}, verdict)


# ---- ref であって複製ではない --------------------------------------------------------------------

def test_a_branch_stores_a_pointer_not_a_copy_of_the_genome(env, tmp_path):
    """genome を複製した瞬間、『枝Xとは何か』への答えが2つになり、
    面白い問いが『どちらが古いか』に変わる。"""
    gid = _v2(env)
    B.create("fast", gid, archive=env)
    raw = json.loads((tmp_path / "branches.json").read_text(encoding="utf-8"))
    assert raw["fast"]["genome_id"] == gid
    assert "genome" not in raw["fast"] and "components" not in json.dumps(raw)


def test_resolving_a_branch_is_deterministic_because_the_archive_is_append_only(env):
    gid = _v2(env)
    B.create("fast", gid, archive=env)
    a = B.resolve("fast", archive=env)
    b = B.resolve("fast", archive=env)
    assert a["harness_id"] == b["harness_id"]
    assert a["manifest"]["components"]["transport"] == "transport/v2"


def test_delete_then_recreate_from_the_same_id_gives_the_same_harness(env):
    """ref の削除が安価で可逆だからこそ、上限を設けても運用が詰まらない。"""
    gid = _v2(env)
    B.create("fast", gid, archive=env)
    before = B.resolve("fast", archive=env)["harness_id"]
    assert B.delete("fast") is True
    B.create("fast", gid, archive=env)
    assert B.resolve("fast", archive=env)["harness_id"] == before


def test_deleting_a_branch_does_not_touch_the_archive(env):
    gid = _v2(env)
    B.create("fast", gid, archive=env)
    B.delete("fast")
    assert env.get(gid) is not None


# ---- main は ref にしない -----------------------------------------------------------------------

def test_main_cannot_be_a_branch(env):
    """ref にすると基底に第二の定義ができ、コードから乖離しうる。
    そうなると reset_to_base が『出荷時に帰る』ではなく
    『その ref が言う場所へ行く』に変わる。"""
    gid = _v2(env)
    for name in ("main", "base", "HEAD", "head"):
        with pytest.raises(B.BranchError) as exc:
            B.create(name, gid, archive=env)
        assert "reserved" in str(exc.value)


def test_base_stays_reachable_after_any_sequence_of_branch_operations(env, tmp_path,
                                                                     monkeypatch):
    """枝機能が壊しうる唯一の経路は、比較が ACTIVE_PATH に書くこと。
    このモジュールは write_active を呼ばない。"""
    from relay.selfimprove import runtime_config as RC
    monkeypatch.setenv(RC.OVERRIDE_ENV, str(tmp_path / "active_manifest.json"))
    monkeypatch.setattr(RC, "_note", lambda *a, **k: None)
    gid = _v2(env)
    B.create("a", gid, archive=env)
    B.materialize_to_file("a", archive=env)
    B.delete("a")
    B.create("b", gid, archive=env)
    RC.reset_to_base()
    assert RC.active_manifest(refresh=True) == M.base_manifest()


def test_no_function_here_writes_the_active_manifest():
    """『比較は子プロセスの向き先しか変えない』が全体の安全論拠。"""
    code = _code_of(B)
    assert "write_active" not in code
    assert "ACTIVE_PATH" not in code


# ---- 拒否 ---------------------------------------------------------------------------------------

def test_a_ref_that_cannot_resolve_is_refused_at_creation(env):
    """20分の比較の途中で気づくのではなく、画面を見ている今わかること。"""
    with pytest.raises(B.BranchError) as exc:
        B.create("ghost", "deadbeef1234", archive=env)
    assert "no archive row" in str(exc.value)


def test_a_rejected_genome_cannot_be_made_runnable_by_naming_it(env):
    """ループが拒否した genome が、名前を与えられて戻ってくる扉を作らない。"""
    gid = _v2(env, verdict="SECURITY_REJECTED")
    with pytest.raises(B.BranchError) as exc:
        B.create("sneaky", gid, archive=env)
    assert "not selectable" in str(exc.value)


def test_the_depth_rule_is_not_applied_to_a_hand_named_branch(env):
    """MAX_UNVALIDATED_DEPTH は『ループが証明済み祖先からどこまで離れてよいか』の規律で、
    自動親選択のためのもの。人が明示的に名前を付ける行為に当てるのは
    規則を書かれた問題の外へ持ち出すこと。"""
    code = _code_of(B.create)
    assert "_verdict_ok" in code
    assert "_selectable" not in code.replace("_verdict_ok", "")


def test_duplicate_labels_are_refused(env):
    gid = _v2(env)
    B.create("fast", gid, archive=env)
    with pytest.raises(B.BranchError):
        B.create("fast", gid, archive=env)


def test_the_ceiling_holds(env):
    gid = _v2(env)
    for i in range(B.MAX_BRANCHES):
        B.create("b%d" % i, gid, archive=env)
    with pytest.raises(B.BranchError) as exc:
        B.create("one-too-many", gid, archive=env)
    assert "limit" in str(exc.value)


@pytest.mark.parametrize("bad", ["", "   ", "has space", "slash/es", "x" * 41, "emoji🙂"])
def test_bad_labels_are_refused_before_anything_is_written(env, bad, tmp_path):
    with pytest.raises(B.BranchError):
        B.create(bad, _v2(env), archive=env)
    assert not (tmp_path / "branches.json").exists() or "\\u" not in \
        (tmp_path / "branches.json").read_text(encoding="utf-8")


# ---- 名前なしで走っている状態が見えること ---------------------------------------------------------

def test_the_active_harness_is_resolved_back_to_a_name(env, tmp_path, monkeypatch):
    from relay.selfimprove import runtime_config as RC
    monkeypatch.setenv(RC.OVERRIDE_ENV, str(tmp_path / "active_manifest.json"))
    monkeypatch.setattr(RC, "_note", lambda *a, **k: None)
    gid = _v2(env)
    B.create("fast", gid, archive=env)
    assert B.describe_active(archive=env)["kind"] == "base"
    RC.write_active(B.resolve("fast", archive=env)["manifest"])
    got = B.describe_active(archive=env)
    assert got["kind"] == "branch" and got["label"] == "fast"


def test_an_unnamed_active_harness_says_so_rather_than_claiming_base(env, tmp_path,
                                                                    monkeypatch):
    """これがこの失敗モードの唯一の検出器。base に丸めた瞬間、
    誰も覚えていない harness で走っている状態が見えなくなる。"""
    from relay.selfimprove import runtime_config as RC
    monkeypatch.setenv(RC.OVERRIDE_ENV, str(tmp_path / "active_manifest.json"))
    monkeypatch.setattr(RC, "_note", lambda *a, **k: None)
    RC.write_active(M.apply_genome(M.base_manifest(), {"parameters": {"max_retries": 3}}))
    got = B.describe_active(archive=env)
    assert got["kind"] == "unnamed", got
    assert got["label"] is None


def test_a_broken_ref_does_not_stop_the_active_harness_being_described(env, tmp_path,
                                                                      monkeypatch):
    """1本壊れた ref のせいで『今どこにいるか』が答えられなくなるのは、
    まさに答えが要る場面で答えを失うこと。"""
    from relay.selfimprove import runtime_config as RC
    monkeypatch.setenv(RC.OVERRIDE_ENV, str(tmp_path / "active_manifest.json"))
    monkeypatch.setattr(RC, "_note", lambda *a, **k: None)
    gid = _v2(env)
    B.create("good", gid, archive=env)
    refs = B.read()
    refs["broken"] = {"genome_id": "nope", "created_at": 0, "last_run_at": None, "note": ""}
    B._write(refs)
    RC.write_active(B.resolve("good", archive=env)["manifest"])
    assert B.describe_active(archive=env)["label"] == "good"


# ---- 鮮度 ---------------------------------------------------------------------------------------

def test_a_branch_records_when_it_was_last_run(env):
    """剪定ではなく表示で解く。古い枝が古いと分かることが対策。"""
    gid = _v2(env)
    B.create("fast", gid, archive=env, now=lambda: 100)
    assert B.read()["fast"]["last_run_at"] is None
    B.touch("fast", now=lambda: 500)
    assert B.read()["fast"]["last_run_at"] == 500


def test_touching_an_unknown_branch_is_not_an_error(env):
    B.touch("nobody")          # must not raise


# ---- 子プロセス用のファイル ----------------------------------------------------------------------

def test_materialize_writes_a_manifest_a_child_can_be_pointed_at(env):
    gid = _v2(env)
    B.create("fast", gid, archive=env)
    path, info = B.materialize_to_file("fast", archive=env)
    try:
        on_disk = json.loads(open(path, encoding="utf-8").read())
        assert on_disk == info["manifest"]
        assert M.harness_id(on_disk) == info["harness_id"]
    finally:
        os.remove(path)
