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


def test_the_outcome_the_fleet_actually_emits_is_retried():
    """最初の版は、実際には出ない outcome を並べていた。

    結末を設定している分岐を読んで決め、フリートが実際に何を出すかを数えなかった。
    記録された履歴の分布は DONE 4 / MAXTURNS 4 / STUCK 2 で、ここで実際に起きる唯一の
    失敗である STUCK が抜けていた。実走行で w1 が STUCK のまま止まり、理由は
    「token-limit recycle: fresh conversation did not render」 -- まさにこの機構が
    対象とすべき一過性で、再投入されないままだった。"""
    assert "STUCK" in FR.RETRYABLE_OUTCOMES, "実際に出る結末が対象外"
    assert "INFRA_STUCK" in FR.RETRYABLE_OUTCOMES
    assert "REFUSED" in FR.RETRYABLE_OUTCOMES


def test_repetition_is_not_recovery():
    """MAXTURNS はターン予算を使い切ったということ。同じ予算で同じ課題を回せば同じ結末。
    CANCELLED は人が止めたもの。どちらも再投入は回復ではなく繰り返し。"""
    for bad in ("DONE", "MAXTURNS", "CANCELLED"):
        assert bad not in FR.RETRYABLE_OUTCOMES, "%s を再投入対象にしている" % bad
        assert bad in FR.NON_RETRYABLE_OUTCOMES
    assert not (FR.RETRYABLE_OUTCOMES & FR.NON_RETRYABLE_OUTCOMES), "両方に入っている結末がある"


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
    # 文字数窓と「最初の print まで」で見ていた。統合の再試行を止めるガードが print を1本
    # 手前に足した瞬間、区切り位置が動いて落ちた —— 予算の数え方は何も変わっていないのに。
    #
    # 見たいのは「予算のキーがゴール文であること」なので、キーそのものを見る。
    # THE BLOCK, NOT A CHARACTER WINDOW. This read src[i:i+2600], so adding lines to the
    # retry branch -- instrumentation, in this case, which changed no behaviour -- pushed
    # the keying past the window and the test failed while the property it names still
    # held. A fixed byte count is not a scope.
    src = inspect.getsource(RF.run_relay_fleet)
    lines = src.splitlines()
    start = next(k for k, l in enumerate(lines) if l.strip() == "if _retry_on:")
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = len(lines)
    for k in range(start + 1, len(lines)):
        l = lines[k]
        if l.strip() and (len(l) - len(l.lstrip())) <= indent:
            end = k
            break
    block = chr(10).join(lines[start:end])
    assert "_retry_used.get(_g" in block, "予算をゴール文で引いていない"
    assert "_retry_used[_g] = _retry_used.get(_g, 0) + 1" in block, (
        "予算をゴール文で数えていない —— ワーカー名で数えると再投入のたびにリセットされる")
    assert "_retry_used[_w.name" not in block, "予算をワーカー名で数えている"


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


def test_no_comment_still_claims_the_socket_cannot_reach_work_iq():
    """その前提は 2026-08-21 に測定され、偽と判明している。

    否定の記録は transport_policy.py にあり、socket_route.py には 20+ ターン・
    8リクエスト種別・fallback ゼロ・Work IQ 含む(受信箱/予定表の読み書き/SharePoint、
    ground truth に対して検証)が残っている。

    それでも attach() には古い主張が残り、読んだ人間が数分後にそれを事実として述べ、
    自分で反対の測定をしながら矛盾に気づかなかった。前提より長生きしたコメントは、
    無いより悪い -- 証拠として扱われ、しかも間違っている。"""
    import inspect

    src = inspect.getsource(RF.RelayWorker.attach)
    assert "cannot reach Work IQ" not in src, "否定済みの主張がまだ書かれている"
    policy = (__import__("pathlib").Path(RF.__file__).parent / "transport_policy.py"
              ).read_text(encoding="utf-8", errors="replace")
    assert "IS FALSE" in policy, "否定の記録側が消えている"
