"""評価器がコントローラに本当に届いているか。「配線した気になっている」を潰すためのテスト。

この系はこれまで4つの座標を正当に拒否してきた。拒否は健全だったが、
**一度も KEEP/REJECT/INCONCLUSIVE に到達していない**ので、
ゲート辞書の形が合っているかは一度も実行で確かめられていない。
ここが違えば、判定は出たのに controller が全部 REJECT に読む、が黙って起きる。
"""
import ast
import inspect
import re

from relay.selfimprove import decision as D
from relay.selfimprove import route_evaluator as RV
from relay.selfimprove import scheduler as S


def _gate_from(control, candidate):
    """アダプタが組み立てるのと同じ形。judge の3値をそのまま通す。"""
    v = RV.decide(control, candidate)
    return {"keep": v["verdict"] == "keep", "verdict": v["verdict"], "reason": v["why"]}


def _arm(done, peak_mb, goals=4):
    return {"done": done, "peak_mb": peak_mb, "goals": goals}


# ---- 契約: 3値がコントローラで3値のまま出ること ------------------------------------------------

def test_a_memory_win_reaches_the_controller_as_keep():
    out = D.decide(gate=_gate_from(_arm(4, 1653.0), _arm(4, 205.0)), frozen_ok=True)
    assert out["state"] == D.KEEP, out


def test_a_completion_loss_reaches_the_controller_as_reject():
    out = D.decide(gate=_gate_from(_arm(4, 1653.0), _arm(3, 5.0)), frozen_ok=True)
    assert out["state"] == D.REJECT
    assert "never a capability" in out["reason"]


def test_a_null_result_does_not_arrive_as_reject():
    """ここが一番落ちやすい。REJECT に潰れると、
    『何も分からなかった』が『悪かった』として最適化器に教えられる。"""
    out = D.decide(gate=_gate_from(_arm(4, 500.0), _arm(4, 400.0)), frozen_ok=True)
    assert out["state"] != D.REJECT, out
    assert out["state"] == D.INCONCLUSIVE, out


def test_the_reason_survives_the_hop():
    """判定理由が消えると、台帳には verdict だけが残り、
    なぜそう判定したかは二度と復元できない。"""
    out = D.decide(gate=_gate_from(_arm(4, 1653.0), _arm(4, 205.0)), frozen_ok=True)
    assert "1448" in out["reason"] or "peak memory fell" in out["reason"], out["reason"]


# ---- 拒否は結果ではない ---------------------------------------------------------------------------

def test_a_refusal_arrives_as_infra_and_not_as_a_gate():
    """プリフライト拒否は『経路について分かったこと』ではない。
    gate に入れると、走らなかった実験が judge の結果として記録される。"""
    src = inspect.getsource(S.route_evaluator_for)
    i = src.index("refusals:")
    block = src[i:i + 400]
    assert '"gate": None' in block, "拒否が gate を埋めている"
    assert '"aborted": True' in block
    # None であって {} ではない。decision.py は「未評価」と「空で通過」を区別する。
    assert '"gate": {}' not in block


def test_the_token_precondition_is_actually_wired_not_asserted():
    """最初の版は token_ok=True をハードコードしていた。プリフライトの2条件のうち
    片方 -- 『トークンが無ければ両腕は同一プログラム』 -- を、自分で書いて自分で無効化していた。
    これは今日この系が拒否してきた4座標と同じ欠陥が、
    『実験は問題なく走った』という顔で入ってくる経路。"""
    src = inspect.getsource(S.route_evaluator_for)
    assert "token_ok=True" not in src, "トークン条件が定数で潰されている"
    assert "token_ok=_token_is_capturable()" in src


def test_the_token_check_captures_rather_than_infers():
    """『トークンがあるはず』は検査ではない。実際に1枚開いて掴んで閉じる。"""
    src = inspect.getsource(S.route_evaluator_for)
    i = src.index("def _token_is_capturable")
    body = src[i:i + 1200]
    assert "capture_via_tab" in body
    assert "expires_in" in body, "期限切れトークンを有効として通す"


# ---- 両腕が同じプログラムにならないこと ----------------------------------------------------------

def test_the_route_singleton_is_rebuilt_between_arms():
    """SocketRoute.enabled は構築時に読まれ、フリートは1プロセス1インスタンスを抱える。
    環境変数だけ倒すと、候補腕が対照腕の(無効な)経路を使い回して
    黙ってタブを開き、きれいな null を報告する。"""
    src = inspect.getsource(S.route_evaluator_for)
    assert "_SOCKET_ROUTE = route" in src, "シングルトンを差し替えていない"
    i, j = src.index("def _fresh_route"), src.index("def _run(")
    assert "enabled=enabled" in src[i:j], "構築時に enabled を渡していない"
    assert "_fresh_route(socket_on)" in src[j:], "腕ごとに作り直していない"


def test_the_arms_differ_by_the_switch_and_the_switch_is_set_both_ways():
    src = inspect.getsource(S.route_evaluator_for)
    assert 'MCP_FLEET_SOCKET"] = "1" if socket_on else "0"' in src
    assert "socket_on=False" in src and "socket_on=True" in src


def test_fallbacks_come_from_the_route_this_arm_built():
    """最初の版は存在しない SR._SINGLETON を読み、例外を握って常に 0 を報告していた。
    フォールバック数が常に 0 の測定は、経路が全滅した腕を健全として通す。"""
    src = inspect.getsource(S.route_evaluator_for)
    assert "_SINGLETON" not in src
    assert 'route.status().get("fallbacks"' in src


# ---- 実験行が本番の学習データを汚さないこと ------------------------------------------------------

def test_campaign_fallbacks_do_not_land_in_the_live_training_log():
    """両腕とも輸送を強制するので、socket 腕は分類機ならタブへ送った目標を運ぶ。
    有益なラベルだが、無印で本番ログに混ぜると本番に現れない分布を学ばせる。
    このリポジトリはテストスイートで本番台帳を2つ汚した実績がある。"""
    from relay import socket_route as SR
    assert S.CAMPAIGN_SOCKET_LOG != SR.DEFAULT_LOG
    src = inspect.getsource(S.route_evaluator_for)
    assert "log_path=log_path or CAMPAIGN_SOCKET_LOG" in src


def test_the_reason_for_the_separate_log_is_written_down():
    """『分けた』だけ残ると、次の人が統合して同じ汚染を再現する。"""
    src = inspect.getsource(S)
    i = src.index("CAMPAIGN_SOCKET_LOG =")
    block = re.sub(r"\s+", " ", src[max(0, i - 1200):i])
    assert "distribution production never shows" in block


# ---- 測っているものが仮説の量であること ----------------------------------------------------------

def test_this_evaluator_measures_memory_and_not_pass_at_1():
    """CompanionBench 評価器を輸送仮説に当てると、両腕が同点で永遠に inconclusive になる。
    測る量が仮説の量でなければ、ループは測定しているふりをして何も学ばない。"""
    src = inspect.getsource(S.route_evaluator_for)
    assert "peak_sampler=_edge_mb" in src
    tree = ast.parse(src.lstrip())
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "msedge" in src.lower()
    # 完了数だけを見ていないこと。両腕が同点でも記憶の差で判定が出る。
    assert "memory_gain_mb" in src and "peak_mb" not in names


def test_the_evaluator_signature_is_the_one_the_controller_calls():
    ev = S.route_evaluator_for(["g"], agent_url="http://x")
    params = list(inspect.signature(ev).parameters)
    assert params[:2] == ["candidate_manifest", "experiment_id"], params


# ---- 候補マニフェストが候補腕に本当に届くこと ----------------------------------------------------

def test_the_candidate_manifest_reaches_the_candidate_arm():
    """届かなければ transport/v1 と transport/v2 が同じ測定になる。
    今日この系が4座標を正当に拒否した『両腕が同じプログラム』欠陥の、
    マニフェスト階層での再発。"""
    src = inspect.getsource(S.route_evaluator_for)
    assert "manifest=candidate_manifest" in src, "候補腕が候補を読んでいない"
    assert "manifest=None" in src, "対照腕が基準に戻っていない"


def test_the_arm_does_not_rewrite_the_operators_active_harness():
    """生きた設定を書き換えるA/Bは A/B ではなく逐次デプロイ2回。"""
    from relay.selfimprove import runtime_config as RC
    src = inspect.getsource(S.route_evaluator_for)
    assert RC.OVERRIDE_ENV in src or "OVERRIDE_ENV" in src
    assert "ACTIVE_PATH" not in src, "実アクティブファイルに触れている"


def test_the_override_is_cleared_even_if_an_arm_raises():
    """候補の上書きが残ると、以後この端末は誰も選んでいない harness で走る。"""
    src = inspect.getsource(S.route_evaluator_for)
    i = src.index("if candidate_first:")
    tail = src[i:i + 500]
    assert "finally:" in tail and "_activate(None)" in tail


def test_clearing_the_override_invalidates_the_cache():
    """キャッシュは (path, mtime) キー。上書きを外すと腕の前の path に戻り、
    キャッシュ済みなら候補が対照腕へ漏れる。"""
    src = inspect.getsource(S.route_evaluator_for)
    assert "active_manifest(refresh=True)" in src


# ---- 床は開始条件ではなく走行中の条件 -------------------------------------------------------------

def test_the_memory_floor_is_checked_during_the_run_not_only_at_its_start():
    """2.2GB で始めてタブを2枚開けば途中で床を割り、残りはページファイルを測る。
    preflight は一度しか見ないので、それだけでは論拠が持たない。"""
    src = inspect.getsource(S.route_evaluator_for)
    assert "min_free_mb" in src
    assert "virtual_memory().available" in src, "サンプルごとに空きを見ていない"


def test_breaking_the_floor_mid_run_is_an_abort_and_not_a_verdict():
    """inconclusive にすると、スワップの測定が verdict を着て台帳に入る。
    『測れなかった』と『区別がつかなかった』は別の主張。"""
    src = inspect.getsource(S.route_evaluator_for)
    i = src.index('low < RV.MIN_FREE_MB')
    block = src[i:i + 700]
    assert '"gate": None' in block and '"aborted": True' in block
    assert '"inconclusive"' not in block


def test_arm_order_is_swappable_and_recorded():
    """腕は逐次で、Edge は腕の間にメモリを返さない。2番目の腕は1番目の残渣から始まり、
    圧迫下では OS が作業セットを削るので、バイアスの向きは
    『どちらが2番目か』だけで決まる。最初の2キャンペーンは両方 control 先行で
    符号が逆に出た。順序は議論で退けられない。"""
    src = inspect.getsource(S.route_evaluator_for)
    assert "candidate_first" in src
    assert "arm_order" in src, "順序が結果に記録されない"
    i = src.index("if candidate_first:")
    block = src[i:i + 300]
    assert "candidate = _candidate()" in block and "control = _control()" in block


# ---- 従属変数が腕に帰属できる量であること --------------------------------------------------------

def test_the_measured_quantity_is_attributable_to_the_arm():
    """合計RSSのピークは腕に帰属できない。別セッションがタブを開けば
    その上昇は丸ごと走行中の腕に計上され、OS のトリミングで RSS は
    需要ではなくシステム全体の圧を測り、第2腕は先行腕の高水位標に潰される。
    腕の開始時に存在しなかったプロセスだけが、その腕のもの。"""
    src = inspect.getsource(S.route_evaluator_for)
    assert "baseline_pids" in src
    assert "private" in src, "commit ではなく RSS を読んでいる"
    i = src.index("def _edge_mb")
    body = src[i:i + 3600]
    assert "base.get(pid, 0.0)" in body, "既存プロセスの増分を見ていない"


def test_the_baseline_is_reset_before_the_arm_not_inside_the_sampler():
    """measure_arm は run_goals の前に1回サンプルを取る。サンプラ内で遅延初期化すると、
    その1回 -- start_mb になる値 -- だけ前の腕のベースラインを引き継ぐ。"""
    src = inspect.getsource(S.route_evaluator_for)
    for arm in ("def _control():", "def _candidate():"):
        i = src.index(arm)
        assert "_begin_attribution()" in src[i:i + 200], arm


def test_the_renderer_count_is_reported_because_the_mechanism_beats_the_statistic():
    """観測された run 間スイングは判定閾値の約4倍。単発 run で検出しようとするより、
    『socket ゴールはレンダラーを生まない』を測って算術で出すほうが強い。"""
    src = inspect.getsource(S.route_evaluator_for)
    assert "new_renderers" in src and '"renderers"' in src


def test_a_warmup_pass_is_available_and_its_numbers_are_discarded():
    """セッション最初の腕はレンダラー生成・認証・セッション確立の代金を払う。"""
    src = inspect.getsource(S.route_evaluator_for)
    i = src.index("def _warmup")
    body = src[i:i + 900]
    assert "goals[:1]" in body
    assert '_floor["min_free_mb"] = None' in body, "捨て走行の圧が本測定の床判定に残る"


def test_growth_inside_existing_processes_is_counted():
    """最初の版は『腕が新たに生んだプロセス』だけを数え、警告走行の後に
    タブ4枚を開いた腕が新規1プロセス18MBという値を返した。Edge は
    レンダラープールを再利用するので、タブのコストは既存プロセスの
    増分として乗る -- 新規プロセス規則はそれをゼロと採点する。"""
    src = inspect.getsource(S.route_evaluator_for)
    i = src.index("def _edge_mb")
    body = src[i:i + 3600]
    assert "grew = value - base.get(pid, 0.0)" in body


def test_a_shrinking_process_does_not_pay_the_arm_a_credit():
    """腕が触れていないレンダラーが OS にトリミングされただけで
    『この腕はメモリを減らした』ことにされる。"""
    src = inspect.getsource(S.route_evaluator_for)
    i = src.index("def _edge_mb")
    body = src[i:i + 3600]
    assert "if grew > 0:" in body
