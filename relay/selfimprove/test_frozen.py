"""Unit tests for the frozen-constitution guard. Run: python -m relay.selfimprove.test_frozen"""
import pytest
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


def test_the_baseline_never_records_an_absolute_path(tmp_path):
    """このファイルは追跡され push される -- 今回は公開リポジトリへ。
    repo_root をそのまま書いていたので、スナップショットのたびにユーザ名と
    ディレクトリ名が履歴に入っていた。しかも読み返されてもいなかった。"""
    import json
    import re

    path = str(tmp_path / "baseline.json")
    F.snapshot_baseline(baseline_path=path, force=True)
    text = open(path, encoding="utf-8").read()
    assert not re.search(r"[A-Za-z]:\\|[A-Za-z]:/", text), "絶対パスが記録されている"
    assert json.loads(text)["checksums"], "checksums が空"


def test_a_checkout_with_windows_line_endings_is_not_tampering(tmp_path):
    """生バイトで比較していたため、`core.autocrlf` の Windows チェックアウトでは
    1文字も触れていない木で「判定器が改竄された」と恒久的に報告していた。
    実例は manifest.py の 233 個の CRLF。

    痛いのは2つ目のほう。整合性チェックが清潔な木で狼少年になると、
    直るのではなく「飛ばすフラグ」が足され、検査は静かに存在しなくなる。"""
    lf = tmp_path / "lf.py"
    crlf = tmp_path / "crlf.py"
    body = "def judge():\n    return 1\n"
    lf.write_bytes(body.encode())
    crlf.write_bytes(body.replace("\n", "\r\n").encode())
    assert F._sha256(str(lf)) == F._sha256(str(crlf))


def test_a_real_content_change_is_still_caught(tmp_path):
    """正規化が検査そのものを無力化していないこと。"""
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_bytes(b"def judge():\r\n    return 1\r\n")
    b.write_bytes(b"def judge():\r\n    return 2\r\n")
    assert F._sha256(str(a)) != F._sha256(str(b))


def test_the_shipped_frozen_set_matches_its_baseline():
    """このテストが赤いとき、意味は2つに1つ -- 誰かが判定器を触ったか、
    再ベースラインを伴う正当な変更が入ったか。どちらも人が読むべき事象。"""
    ok, changed = F.frozen_intact()
    assert ok, "frozen set adrift: %s" % changed


def test_the_anchor_survives_a_checkout_that_rewrites_line_endings(tmp_path, monkeypatch):
    """アンカーも生バイト digest だった -- チェックサム側で今朝直したのと同じ欠陥。

    git は Windows で baseline.json を CRLF に展開するので、スナップショット時に書いた
    アンカーは、そのファイルが checkout / stash / ブランチ切替を通った瞬間に一致しなく
    なり、誰も触っていない baseline に対して BASELINE_REWRITTEN が出ていた。実測 22 個。

    しかも frozen intact はスケジュール実行の前提条件なので、**アンカーだけで
    `nightly()` を恒久的にブロックできる**。"""
    baseline = tmp_path / "frozen_baseline.json"
    baseline.write_bytes(b'{\n  "checksums": {}\n}\n')
    digest_lf = F._baseline_digest(str(baseline))

    baseline.write_bytes(b'{\r\n  "checksums": {}\r\n}\r\n')
    assert F._baseline_digest(str(baseline)) == digest_lf, (
        "改行の違いだけで別のファイルと判定されている")


def test_a_real_edit_to_the_baseline_is_still_detected(tmp_path):
    """正規化が検査そのものを無力化していないこと -- 書き換えは依然として検出する。"""
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_bytes(b'{"checksums": {"x": "1"}}\n')
    b.write_bytes(b'{"checksums": {"x": "2"}}\n')
    assert F._baseline_digest(str(a)) != F._baseline_digest(str(b))


# ---- 基準値の取り直しは、特定された判断であること ---------------------------------------------------

def test_force_without_a_reason_is_refused(tmp_path, capsys):
    bp = str(tmp_path / "b.json")
    F.snapshot_baseline(F.REPO, bp)
    assert F._main(["--snapshot", "--force", "--repo", F.REPO, "--baseline", bp]) == 2
    assert "--reason" in capsys.readouterr().out


def test_a_reblessing_is_recorded_with_the_operators_words(tmp_path, monkeypatch, capsys):
    """台帳は許可しないし止めもしない。残すのは『誰が・なぜ・どの指示で』だけ。
    原文で残すのは、要約が行為者による自分の権限の解釈になるから。"""
    from relay.selfimprove import authority_ledger as led
    ledger_path = str(tmp_path / "ledger.jsonl")
    monkeypatch.setenv(led.ENV_PATH, ledger_path)
    bp = str(tmp_path / "b.json")
    F.snapshot_baseline(F.REPO, bp)
    words = "OK問題なし。進めてよい。"
    rc = F._main(["--snapshot", "--force", "--repo", F.REPO, "--baseline", bp,
                  "--reason", "promoting quality_cards", "--authorization", words])
    assert rc == 0
    rows = [r for r in led.read(ledger_path) if r.get("event") == led.REBLESS]
    assert len(rows) == 1
    assert rows[0]["authorization"] == words
    assert rows[0]["reason"] == "promoting quality_cards"
    assert "ledger: seq=" in capsys.readouterr().out, "台帳の末尾が出力されていない"


def test_the_docstring_no_longer_claims_something_nothing_enforces():
    """以前は『コードが自分でやることでは決してない』と書いてあったが、それを
    強制するものは無かった。強制できない規則を残すと、読む人はいずれ
    規則全体を飾りとして扱う -- 実際 operator はこの操作が自動だと認識していた。"""
    doc = F.snapshot_baseline.__doc__ or ""
    assert "SPECIFIED decision" in doc
    assert "not claimed" in doc.lower()
    # 旧文は残っているが、それは**引用**としてであり主張としてではない。
    # 単に「文字列が無いこと」を見ると、引退させた規則を歴史として書き残す
    # 正しいやり方まで落としてしまう。
    retired = "never something code does"
    if retired in doc:
        before = doc[max(0, doc.index(retired) - 200):doc.index(retired)]
        assert "It said" in before or "previous wording" in before, (
            "旧い主張が、引退したものとしてではなく現役の規則として残っている")


def test_a_mismatch_is_recorded_once_not_once_per_check(tmp_path, monkeypatch):
    """照合は毎回走る。未解決の不一致を毎回積むと、記録すべき『行為』が埋もれる。"""
    from relay.selfimprove import authority_ledger as led
    ledger_path = str(tmp_path / "ledger.jsonl")
    monkeypatch.setenv(led.ENV_PATH, ledger_path)
    for _ in range(3):
        led.record_mismatch_once(["a.py"], reason="drift", actor_claimed="t")
    rows = [r for r in led.read(ledger_path) if r.get("event") == led.BASELINE_MISMATCH]
    assert len(rows) == 1
    led.record_mismatch_once(["a.py", "b.py"], reason="more drift", actor_claimed="t")
    rows = [r for r in led.read(ledger_path) if r.get("event") == led.BASELINE_MISMATCH]
    assert len(rows) == 2, "差分が変わったら新しい記録が要る"


# ---- 恒久委任と、その外側に置いたもの -------------------------------------------------------------

def test_the_standing_delegation_does_not_cover_the_machinery_that_enforces_it(tmp_path,
                                                                               capsys):
    """operator は再署名を委任し、Skills の承認だけ人間に残した。だがその
    「Skills は人間」というルールは憲法の中にあり、憲法の書き換えは委任済み。
    線を守るルールが線の内側にあると、委任された1手で線ごと消せる。
    取り消し経路とこの除外リスト自身も同じ。"""
    bp = str(tmp_path / "b.json")
    F.snapshot_baseline(F.REPO, bp)
    data = json.loads(open(bp, encoding="utf-8").read())
    data["checksums"]["relay/selfimprove/manifest.py"] = "0" * 64   # 触ったことにする
    open(bp, "w", encoding="utf-8", newline="\n").write(
        json.dumps(data, indent=2, sort_keys=True) + "\n")

    rc = F._main(["--snapshot", "--force", "--repo", F.REPO, "--baseline", bp,
                  "--reason", "routine", "--authorization", F.STANDING_DELEGATION])
    assert rc == 2
    out = capsys.readouterr().out
    assert "standing delegation does not cover" in out
    assert "manifest.py" in out


def test_an_ordinary_change_stays_delegated(tmp_path):
    """除外は狭くなければ意味がない。全部を除外すれば委任は無効化される。"""
    bp = str(tmp_path / "b.json")
    F.snapshot_baseline(F.REPO, bp)
    data = json.loads(open(bp, encoding="utf-8").read())
    data["checksums"]["bench/companionbench/runner.py"] = "0" * 64
    open(bp, "w", encoding="utf-8", newline="\n").write(
        json.dumps(data, indent=2, sort_keys=True) + "\n")
    rc = F._main(["--snapshot", "--force", "--repo", F.REPO, "--baseline", bp,
                  "--reason", "routine", "--authorization", F.STANDING_DELEGATION])
    assert rc == 0


def test_the_excluded_list_holds_what_defines_the_boundary():
    for rel in ("relay/selfimprove/frozen.py", "relay/selfimprove/manifest.py",
                "tools/security.py"):
        assert rel in F.DELEGATION_EXCLUDED, rel


# ---- 巻き戻し -----------------------------------------------------------------------------------

def test_revoke_restores_the_previous_baseline_from_the_ledger(tmp_path, monkeypatch):
    """`.prev` はキャッシュ。ベースラインを上書きできる者は隣のコピーも消せるので、
    正本は追記専用・連鎖済みの台帳側に置く。"""
    from relay.selfimprove import authority_ledger as led
    monkeypatch.setenv(led.ENV_PATH, str(tmp_path / "l.jsonl"))
    bp = str(tmp_path / "b.json")
    F.snapshot_baseline(F.REPO, bp)
    original = open(bp, encoding="utf-8").read()
    data = json.loads(original)
    data["checksums"]["bench/companionbench/runner.py"] = "0" * 64
    open(bp, "w", encoding="utf-8", newline="\n").write(
        json.dumps(data, indent=2, sort_keys=True) + "\n")
    F._main(["--snapshot", "--force", "--repo", F.REPO, "--baseline", bp,
             "--reason", "accepting", "--authorization", F.STANDING_DELEGATION])
    os.remove(bp + ".prev")                     # キャッシュを消しても復元できること
    F.revoke_baseline(bp, reason="undo")
    assert json.loads(open(bp, encoding="utf-8").read())["checksums"][
        "bench/companionbench/runner.py"] == "0" * 64


def test_revoke_is_its_own_event_not_a_rebless(tmp_path, monkeypatch):
    """事後の不変条件が正反対（rebless の後は intact、revoke の後は破れているのが正常）。
    同じ型にすると、台帳から状態を再構成する側が必ず条件分岐で腐る。"""
    from relay.selfimprove import authority_ledger as led
    monkeypatch.setenv(led.ENV_PATH, str(tmp_path / "l.jsonl"))
    bp = str(tmp_path / "b.json")
    F.snapshot_baseline(F.REPO, bp)
    F._main(["--snapshot", "--force", "--repo", F.REPO, "--baseline", bp,
             "--reason", "r", "--authorization", F.STANDING_DELEGATION])
    F.revoke_baseline(bp, reason="undo")
    assert [r["event"] for r in led.read()][-2:] == [led.REBLESS, led.REVOKE]


def test_revoke_refuses_when_there_is_nothing_to_go_back_to(tmp_path, monkeypatch):
    from relay.selfimprove import authority_ledger as led
    monkeypatch.setenv(led.ENV_PATH, str(tmp_path / "l.jsonl"))
    bp = str(tmp_path / "b.json")
    F.snapshot_baseline(F.REPO, bp)
    with pytest.raises(F.BaselineRefused) as exc:
        F.revoke_baseline(bp, reason="undo")
    assert "nothing to revoke to" in str(exc.value)


def test_revoke_undoes_the_approval_and_says_the_code_is_still_changed(tmp_path, monkeypatch,
                                                                       capsys):
    """承認の取り消しであって版管理ではない。取り消した後に frozen が破れるのは
    副作用ではなく狙い。それを出力で言わないと、壊れたと読まれる。"""
    from relay.selfimprove import authority_ledger as led
    monkeypatch.setenv(led.ENV_PATH, str(tmp_path / "l.jsonl"))
    bp = str(tmp_path / "b.json")
    F.snapshot_baseline(F.REPO, bp)
    data = json.loads(open(bp, encoding="utf-8").read())
    data["checksums"]["bench/companionbench/runner.py"] = "0" * 64
    open(bp, "w", encoding="utf-8", newline="\n").write(
        json.dumps(data, indent=2, sort_keys=True) + "\n")
    F._main(["--snapshot", "--force", "--repo", F.REPO, "--baseline", bp,
             "--reason", "r", "--authorization", F.STANDING_DELEGATION])
    assert F._main(["--revoke", "--repo", F.REPO, "--baseline", bp, "--reason", "undo"]) == 0
    out = capsys.readouterr().out
    assert "frozen set intact: False" in out
    assert "expected" in out
    assert "git revert" in out, "コードを戻す手順が出ていない"


def test_the_revoke_docstring_does_not_promise_to_undo_the_code():
    doc = F.revoke_baseline.__doc__ or ""
    assert "NOT A CHANGE" in doc.upper()
    assert "version control" in doc


def test_two_writers_cannot_interleave(tmp_path):
    """再署名と取り消しはどちらもベースラインとアンカーの両方を書く。
    片方だけ適用された対は、あとから誰にも診断できない唯一の状態。"""
    bp = str(tmp_path / "b.json")
    F.snapshot_baseline(F.REPO, bp)
    with F._baseline_lock():
        with pytest.raises(F.BaselineRefused) as exc:
            with F._baseline_lock(timeout_s=0.2):
                pass
    assert "another process is writing" in str(exc.value)


def test_the_loop_refuses_to_run_while_an_approval_is_withdrawn(tmp_path, monkeypatch):
    """Fable の指摘: revoke 直後の自動ループ挙動は fail-safe のはずだが、
    この経路は今日できたばかりで一度も踏んでいない。実際に踏む。

    取り消すと凍結照合が破れる（それが狙い）。その状態で走行の事前条件が
    通ってしまえば、承認を取り消しても自己改善は動き続けることになる。"""
    from relay.selfimprove import scheduler as S
    # 状態を**構成する**。いまたまたまツリーが漂流しているから通る、では
    # 再署名した瞬間に落ちるテストになる（実際、最初の版がそうだった）。
    # scheduler が実際に呼ぶのは F.frozen_intact。S 側に新しい属性を生やしても
    # 誰も読まず、テストは周囲の状態で通ってしまう（最初の版がまさにそれだった）。
    monkeypatch.setattr(F, "frozen_intact",
                        lambda *a, **k: (False, ["relay/selfimprove/manifest.py"]))
    broken = S.preconditions(budget_candidates=5, activate=False, level="B")
    assert any("frozen" in r for r in broken), (
        "凍結が破れているのに走行が拒否されていない: %s" % broken)

    # 逆も見る。intact のときにこの理由が残っていたら、上の assert は
    # 何も証明していない。
    monkeypatch.setattr(F, "frozen_intact", lambda *a, **k: (True, []))
    intact = S.preconditions(budget_candidates=5, activate=False, level="B")
    assert not any("frozen" in r for r in intact), intact
