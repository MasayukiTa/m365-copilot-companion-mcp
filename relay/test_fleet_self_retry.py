"""fleet 自身の再投入。cockpit が見ていなくても効くこと、そして必ず止まること。

再投入は cockpit のティックにしかなく、あれは .fleet/status.json を見て add_goal を注ぐ。
つまりコマンドラインからの走行、定時実行、別の state ディレクトリを使う走行には
一切効かなかった。「走行が拒否なしで終わる」を、窓が開いているかどうかに依存させない。

2026-08-25 の実測: 6走行28ゴールで 25% が Copilot の定型拒否。落ちる先は毎回入れ替わり、
同時実行数にも依らず(1でも2でも25%)、落ちたゴールは再投入すると通った。
"""
import inspect

from relay import fleet_runner as FR
from relay import relay_fleet as RF


def test_the_settings_come_from_the_same_keys_as_the_cockpit():
    """再試行方針が2つあって食い違うのは、どちらか一方だけより悪い。"""
    src = inspect.getsource(FR.settings_autoretry)
    assert '"autoretry"' in src and '"autoretry_max"' in src


def test_the_cap_can_never_be_unbounded():
    """上限が消えれば、決定的に失敗する課題が永久に回る。"""
    src = inspect.getsource(FR.settings_autoretry)
    assert "min(3" in src, "上限に天井が無い"
    assert "max(0" in src


def test_only_no_answer_outcomes_are_retried():
    """DONE や、課題そのものが誤っている結末を再投入しても、同じ答えが返るだけ。"""
    assert "INFRA_STUCK" in FR.RETRYABLE_OUTCOMES
    assert "REFUSED" in FR.RETRYABLE_OUTCOMES
    for bad in ("DONE", "CANCELLED", "MAXTURNS", "ERROR"):
        assert bad not in FR.RETRYABLE_OUTCOMES, "%s を再投入対象にしている" % bad


def test_each_worker_is_considered_once():
    """掃引は毎秒何度も回り、終端ワーカーは終端のまま。1回だけに絞らなければ
    1件の失敗が毎回再投入され、上限があっても即座に使い切って止まらなく見える。"""
    src = inspect.getsource(RF.run_relay_fleet)
    assert "_retry_seen" in src
    i = src.index("if _retry_on:")
    block = src[i:i + 900]
    assert "id(_w) in _retry_seen" in block
    assert "_retry_seen.add(id(_w))" in block


def test_the_budget_is_keyed_by_goal_text_not_worker_name():
    """再投入されたゴールは新しいワーカー名を得る。名前で数えると予算が毎回リセットされる。"""
    src = inspect.getsource(RF.run_relay_fleet)
    i = src.index("if _retry_on:")
    block = src[i:i + 900]
    assert "_retry_used.get(_g" in block and "_w.name" not in block.split("print(")[0]


def test_it_does_nothing_when_there_is_nowhere_to_queue():
    """add_box を渡されない呼び出し方もある。そこで積もうとすれば例外になる。"""
    src = inspect.getsource(RF.run_relay_fleet)
    assert "if add_box is None:" in src
    i = src.index("if add_box is None:")
    assert "_retry_on = False" in src[i:i + 160]


def test_a_missing_settings_module_does_not_break_the_run():
    """設定が読めないことは、走行を落とす理由にならない。読めなければ再投入しないだけ。"""
    src = inspect.getsource(RF.run_relay_fleet)
    i = src.index("from relay.fleet_runner import RETRYABLE_OUTCOMES")
    block = src[i - 60:i + 320]
    assert "except Exception:" in block
    assert "_retry_on, _retry_cap" in block
