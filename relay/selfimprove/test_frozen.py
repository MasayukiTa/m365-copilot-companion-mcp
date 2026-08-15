"""Unit tests for the frozen-constitution guard. Run: python -m relay.selfimprove.test_frozen"""
import json
import os
import tempfile

from relay.selfimprove import frozen as F

# A small fake manifest used inside the temp repo (independent of the real FROZEN_MANIFEST).
FAKE_MANIFEST = ["a/grader.py", "b/guards.py", "c/constitution.md"]


def _make_repo(d: str) -> None:
    for rel in FAKE_MANIFEST:
        full = os.path.join(d, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as f:
            f.write("original content of %s\n" % rel)


def test_no_baseline():
    with tempfile.TemporaryDirectory() as d:
        bp = os.path.join(d, "frozen_baseline.json")
        ok, changed = F.frozen_intact(d, bp, FAKE_MANIFEST)
        assert ok is False and changed == ["NO_BASELINE"]
    print("ok test_no_baseline")


def test_snapshot_and_intact():
    with tempfile.TemporaryDirectory() as d:
        _make_repo(d)
        bp = os.path.join(d, "frozen_baseline.json")
        # snapshot using the fake manifest, then verify intact
        sums = F.compute_checksums(d, FAKE_MANIFEST)
        data = {"repo_root": d, "checksums": sums}
        with open(bp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f)
        assert all(v != F.MISSING for v in sums.values())
        ok, changed = F.frozen_intact(d, bp, FAKE_MANIFEST)
        assert ok is True and changed == []
    print("ok test_snapshot_and_intact")


def test_modified_file_detected():
    with tempfile.TemporaryDirectory() as d:
        _make_repo(d)
        bp = os.path.join(d, "frozen_baseline.json")
        import json
        with open(bp, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"repo_root": d, "checksums": F.compute_checksums(d, FAKE_MANIFEST)}, f)
        # tamper with the guards file -- a reward-hack attempt
        with open(os.path.join(d, "b/guards.py"), "a", encoding="utf-8", newline="\n") as f:
            f.write("# always keep\n")
        ok, changed = F.frozen_intact(d, bp, FAKE_MANIFEST)
        assert ok is False
        assert "b/guards.py" in changed and "a/grader.py" not in changed
    print("ok test_modified_file_detected")


def test_deleted_file_detected():
    with tempfile.TemporaryDirectory() as d:
        _make_repo(d)
        bp = os.path.join(d, "frozen_baseline.json")
        import json
        with open(bp, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"repo_root": d, "checksums": F.compute_checksums(d, FAKE_MANIFEST)}, f)
        os.remove(os.path.join(d, "c/constitution.md"))
        ok, changed = F.frozen_intact(d, bp, FAKE_MANIFEST)
        assert ok is False and "c/constitution.md" in changed
        # the now-missing file reads as MISSING
        assert F.compute_checksums(d, ["c/constitution.md"])["c/constitution.md"] == F.MISSING
    print("ok test_deleted_file_detected")


def test_burned_append_only():
    old = ['{"instance_id": "a__a-1"}', '{"instance_id": "b__b-2"}']
    extended = old + ['{"instance_id": "c__c-3"}']
    rewritten = ['{"instance_id": "x__x-9"}', '{"instance_id": "b__b-2"}']
    shrunk = old[:1]
    assert F.burned_append_only(old, extended) is True
    assert F.burned_append_only(old, old) is True          # no change is fine
    assert F.burned_append_only(old, rewritten) is False    # first line changed
    assert F.burned_append_only(old, shrunk) is False       # ledger shrank
    print("ok test_burned_append_only")


def test_real_manifest_snapshot_roundtrip():
    # exercise snapshot_baseline / load_baseline against the real repo (read-only, temp baseline)
    with tempfile.TemporaryDirectory() as d:
        bp = os.path.join(d, "frozen_baseline.json")
        data = F.snapshot_baseline(F.REPO, bp)
        assert set(data["checksums"].keys()) == set(F.FROZEN_MANIFEST)
        loaded = F.load_baseline(bp)
        assert loaded == data
        ok, changed = F.frozen_intact(F.REPO, bp)
        assert ok is True and changed == []
    print("ok test_real_manifest_snapshot_roundtrip")


def test_a_baseline_is_not_silently_re_blessed():
    """再スナップショットは改ざん洗浄の経路。上書きは人間の明示行為に限る。"""
    with tempfile.TemporaryDirectory() as d:
        bp = os.path.join(d, "b.json")
        first = F.snapshot_baseline(F.REPO, bp)
        try:
            F.snapshot_baseline(F.REPO, bp)
            assert False, "既存 baseline を黙って上書きした"
        except F.BaselineRefused:
            pass
        assert F.load_baseline(bp) == first
        # --force は通る。禁止ではなく、事故で起きないことが要件。
        assert F.snapshot_baseline(F.REPO, bp, force=True) == first
    print("ok test_a_baseline_is_not_silently_re_blessed")


def test_a_frozen_path_that_does_not_exist_is_refused():
    """存在しないパスは何も pin しない。MISSING を正当な baseline 値として受け入れていたため、
    数か月前に untrack された2つの憲章ドキュメントが黙って無効化されていた。"""
    with tempfile.TemporaryDirectory() as d:
        bp = os.path.join(d, "b.json")
        orig = list(F.FROZEN_MANIFEST)
        F.FROZEN_MANIFEST.append("bench/this_file_does_not_exist.md")
        try:
            F.snapshot_baseline(F.REPO, bp)
            assert False, "存在しないファイルを baseline に入れた"
        except F.BaselineRefused as exc:
            assert "this_file_does_not_exist" in str(exc)
        finally:
            F.FROZEN_MANIFEST[:] = orig
        assert not os.path.exists(bp)
    print("ok test_a_frozen_path_that_does_not_exist_is_refused")


def test_the_shipped_baseline_covers_the_current_manifest():
    """本番の baseline が実際に存在し、現在の凍結集合と一致していること。
    独立レビューまで baseline は存在せず、frozen_intact は常に NO_BASELINE を返していた。
    フェイルセーフではあったが、この検査は一度も稼働していなかった。"""
    base = F.load_baseline()
    assert base is not None, "frozen_baseline.json が無い -- 検査は一度も走らない"
    assert set(base["checksums"]) == set(F.FROZEN_MANIFEST)
    assert F.MISSING not in base["checksums"].values()
    # Deliberately NOT asserting the contents still match. The frozen check exists to catch
    # a change made BETWEEN ITERATIONS OF THE LOOP, and a human editing a grader in a working
    # tree trips it every time -- a test that fails on ordinary development is one everybody
    # learns to ignore, which is worse than not having it. `--verify` is the operational
    # check; this test guards the structure the operational check depends on.


if __name__ == "__main__":
    test_no_baseline()
    test_snapshot_and_intact()
    test_modified_file_detected()
    test_deleted_file_detected()
    test_burned_append_only()
    test_real_manifest_snapshot_roundtrip()
    print("ALL FROZEN TESTS PASSED")


def test_the_security_boundary_is_part_of_the_frozen_set():
    """独立レビューの指摘: 「judge が無事か」の検査が、許可を決める側を見ていなかった。

    測定する側だけを凍結し、許可する側を外していた。進化ループが最も不都合に感じるのは
    後者であり、そこが検査対象外なら「憲法は無傷」という報告に意味がない。
    """
    from relay.selfimprove.frozen import FROZEN_MANIFEST
    for path in ("tools/security.py", "tools/_untrusted.py", "tools/folder_policy.py"):
        assert path in FROZEN_MANIFEST, "%s が凍結対象に入っていない" % path


def test_the_evolution_boundary_is_frozen_too():
    """許可リストを自分で広げられるなら、許可リストは存在しないのと同じ。"""
    from relay.selfimprove.frozen import FROZEN_MANIFEST
    assert "relay/selfimprove/manifest.py" in FROZEN_MANIFEST
    assert "relay/selfimprove/decision.py" in FROZEN_MANIFEST


def test_a_missing_baseline_is_a_violation_not_a_pass():
    """ベースライン不在で「無傷」と答えたら、検査は存在しないのと同じ。"""
    import tempfile, os
    from relay.selfimprove.frozen import frozen_intact
    ok, changed = frozen_intact(baseline_path=os.path.join(tempfile.mkdtemp(), "none.json"))
    assert ok is False and "NO_BASELINE" in changed


def test_the_baseline_does_not_get_to_say_what_is_protected():
    """独立レビューの実証コード: {"checksums": {}} が INTACT を返していた。
    比較ループが baseline 自身のキーを回っていたので、空にすれば0回で通る。"""
    with tempfile.TemporaryDirectory() as d:
        bp = os.path.join(d, "empty.json")
        with open(bp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"checksums": {}}, fh)
        ok, changed = F.frozen_intact(F.REPO, bp)
    assert ok is False
    assert all(c.startswith("UNPINNED:") for c in changed)
    assert len(changed) == len(F.FROZEN_MANIFEST)
    print("ok test_the_baseline_does_not_get_to_say_what_is_protected")


def test_dropping_one_entry_from_the_baseline_is_a_violation():
    """1件だけ外して『検査していない』状態を作る手口も同じ穴。"""
    with tempfile.TemporaryDirectory() as d:
        bp = os.path.join(d, "b.json")
        F.snapshot_baseline(F.REPO, bp)
        data = F.load_baseline(bp)
        data["checksums"].pop("tools/security.py")
        with open(bp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh)
        ok, changed = F.frozen_intact(F.REPO, bp)
    assert ok is False and "UNPINNED:tools/security.py" in changed
    print("ok test_dropping_one_entry_from_the_baseline_is_a_violation")


def test_the_companionbench_judge_is_frozen():
    """候補の採否を実際に決めているのは今や CompanionBench。
    swebench グレーダだけを凍結していたのは、当時それしか無かったからにすぎない。"""
    for rel in ("bench/companionbench/runner.py", "bench/companionbench/episode.py",
                "bench/companionbench/pools.py", "bench/companionbench/episodes/core.py",
                "bench/companionbench/episodes/office.py",
                "bench/companionbench/episodes/runtime.py",
                "bench/companionbench/episodes/sealed.py"):
        assert rel in F.FROZEN_MANIFEST, rel
    print("ok test_the_companionbench_judge_is_frozen")


def test_rewriting_the_baseline_is_caught_by_the_out_of_tree_anchor():
    """作業ツリー内のファイルだけを信じている限り、グレーダとその記録済み
    チェックサムは同じ手で同時に書き換えられる。"""
    if not F._read_anchor():
        return                              # anchored only where an operator has snapshotted
    orig = open(F.DEFAULT_BASELINE, encoding="utf-8").read()
    data = json.loads(orig)
    data["checksums"]["tools/security.py"] = "0" * 64
    try:
        with open(F.DEFAULT_BASELINE, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        ok, changed = F.frozen_intact()
        assert ok is False
        assert "BASELINE_REWRITTEN" in changed, changed
    finally:
        with open(F.DEFAULT_BASELINE, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(orig)
    # Only the anchor verdict is asserted on restore. Full intactness would also require the
    # working tree to match the last snapshot, which it does not while a human is editing a
    # frozen grader -- the same reason the shipped-baseline test checks structure only.
    assert "BASELINE_REWRITTEN" not in F.frozen_intact()[1]
    print("ok test_rewriting_the_baseline_is_caught_by_the_out_of_tree_anchor")


def test_pointing_the_anchor_at_nothing_is_a_violation_not_a_skip():
    """anchor が無ければ検査を飛ばす仕様だったので、同一プロセスの候補が
    環境変数を存在しないパスへ向けるだけで検査を無効化できた。
    fresh clone に anchor が無いのは避けられない(=absent)。
    実在するのに別を指すのは選択(=redirected)であり、区別する。"""
    import os as _os
    if not _os.path.isfile(F.DEFAULT_ANCHOR):
        return                       # anchored only where an operator has snapshotted
    prev = _os.environ.get(F.ANCHOR_ENV)
    _os.environ[F.ANCHOR_ENV] = _os.path.join(tempfile.mkdtemp(), "not_here")
    try:
        assert F.anchor_state() == "redirected"
        ok, changed = F.frozen_intact()
        assert ok is False
        assert "ANCHOR_REDIRECTED_AWAY_FROM_EXISTING" in changed
    finally:
        if prev is None:
            _os.environ.pop(F.ANCHOR_ENV, None)
        else:
            _os.environ[F.ANCHOR_ENV] = prev
    # Only assert the redirect CLEARS when the ambient environment is not itself redirected.
    # A CI-shaped run exports the override at a path that does not exist, which is a redirect
    # by definition -- restoring it must not be read as the check failing to reset.
    if F.anchor_state() != "redirected":
        assert "ANCHOR_REDIRECTED_AWAY_FROM_EXISTING" not in F.frozen_intact()[1]
    print("ok test_pointing_the_anchor_at_nothing_is_a_violation_not_a_skip")
