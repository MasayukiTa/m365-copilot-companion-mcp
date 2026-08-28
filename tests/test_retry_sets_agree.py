"""再試行してよい結末の集合が、Python と C# で一致していること。

## これが何を壊していたか

`relay/fleet_runner.py` は「**2つの再試行方針が食い違うのは、どちらか一方だけより悪い**」と
書いている。実際に食い違っていた:

- Python(`relay/outcomes.py`): `RETRYABLE = {STUCK, INFRA_STUCK, REFUSED}`。
  `FANOUT` は status=done で **再試行対象外**
- C#(`ui/FleetCockpit.cs`): `outcome == "DONE"` **だけ**を除外。つまり `FANOUT` は再投入対象

fan-out の親は terminal・outcome=FANOUT で終わるので、コックピットがこれを再投入していた。
複製は**同じゴール文**と**大きいワーカー番号**を持ち、フリートは結果をゴール文で引くので、
統合結果を書き込んだ直後の親を**複製が上書き**する。

2026-08-28 実測(coordinator ログ100本):
分割した24走行のうち**22走行**が、統合結果ではなく複製を返していた。
8ゴールを63ワーカーに分割し、8件すべて統合まで到達して、**1件も届かなかった**走行がある。

つまり fan-out は壊れていなかった。**動いて、分割して、統合して、その答えをこの1行が捨てていた。**
"""
import io
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cs():
    with io.open(os.path.join(REPO, "ui", "FleetCockpit.cs"), encoding="utf-8") as fh:
        return fh.read()


def test_the_cockpit_retry_set_matches_python():
    from relay.outcomes import RETRYABLE

    src = _cs()
    i = src.index("static readonly string[] _retryableOutcomes")
    decl = src[i:src.index(";", i)]
    got = set(re.findall(r'"([A-Z_]+)"', decl))
    assert got == set(RETRYABLE), (
        "再試行集合がずれている: C#=%r python=%r -- ずれた側が finished な仕事を"
        "再投入するか、直すべき仕事を放置する" % (sorted(got), sorted(RETRYABLE)))


def test_fanout_is_not_retryable_on_either_side():
    """この試験が生まれた当の事象を名指しで固定する。"""
    from relay.outcomes import RETRYABLE, STATUS_OF

    assert STATUS_OF.get("FANOUT") == "done"
    assert "FANOUT" not in RETRYABLE

    src = _cs()
    i = src.index("static readonly string[] _retryableOutcomes")
    assert "FANOUT" not in src[i:src.index(";", i)], (
        "C# が FANOUT を再試行対象にしている -- 統合結果が複製に上書きされる")


def test_no_retry_site_still_selects_on_not_done():
    """「DONE 以外は全部」という判定が、どの再試行経路にも残っていないこと。

    3箇所あった(自動再試行 / 一括再試行 / そのボタンの出現条件)。1つでも残ると、
    その経路から fan-out の答えが消える。
    """
    src = _cs()
    code = re.sub(r"//[^\n]*", "", src)
    bad = re.findall(r'S\((?:w|rw), "outcome"\)\s*==\s*"DONE"', code)
    assert not bad, (
        "「DONE 以外は再試行」の判定が %d 箇所残っている" % len(bad))


def test_every_retry_site_goes_through_the_shared_predicate():
    """3経路とも同じ述語を通ること。増えた経路が独自判定を持たないように。"""
    src = _cs()
    code = re.sub(r"//[^\n]*", "", src)
    assert code.count("IsRetryableOutcome(") >= 4, (
        "共有述語の呼び出しが足りない(定義1 + 3経路を期待) -- どこかが独自判定に戻っている")
