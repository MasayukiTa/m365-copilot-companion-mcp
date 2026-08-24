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

def _inner_source(name: str) -> str:
    """`route_evaluator_for` の中の入れ子関数の本文。固定文字数で切り出すと、
    注記を1つ足しただけで対象行が窓の外に出て、検査が静かに通らなくなる
    -- 同じ直しを2回している。"""
    import ast
    src = inspect.getsource(S.route_evaluator_for)
    fn = next(n for n in ast.walk(ast.parse(src.lstrip()))
              if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.unparse(fn)


def _edge_mb_source() -> str:
    return _inner_source("_edge_mb")


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
    # 検査すべきは「両腕が同じ設定にならない」ことで、その書き方ではない。
    # この assert は今日2度、不変条件が何も変わっていないのに書き換えで落ちている。
    i, j = src.index("def _control():"), src.index("def _candidate():")
    k = src.index("def _warmup")
    control, candidate = src[i:j], src[j:k]
    assert "socket_on=bool(control_socket)" in control
    assert "else True" in candidate, "候補腕が経路を有効にしていない"
    assert "control_manifest" in control and "candidate_manifest" in candidate


def test_fallbacks_come_from_the_route_this_arm_built():
    """最初の版は存在しない SR._SINGLETON を読み、例外を握って常に 0 を報告していた。
    フォールバック数が常に 0 の測定は、経路が全滅した腕を健全として通す。"""
    src = inspect.getsource(S.route_evaluator_for)
    assert "_SINGLETON" not in src
    # `status = route.status()` に変わった。不変条件は「この腕が作った経路から読む」ことで、
    # その書き方ではない。
    assert "status = route.status()" in src
    assert 'status.get("fallbacks"' in src


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
    assert "candidate_manifest" in src, "候補腕が候補を読んでいない"
    i = src.index("def _candidate():")
    j = src.index("def _warmup")
    assert "candidate_manifest" in src[i:j], "候補腕の中で候補が使われていない"
    k = src.index("def _control():")
    # 対照腕は候補を読まない。既定 control_manifest=None なので基底に戻る。
    assert "candidate_manifest" not in src[k:i], "対照腕が候補を読んでいる"
    assert "manifest=control_manifest" in src[k:i]


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
    帰属できるのはプロセスごとの増分。新規プロセスだけを数える規則は、Edge が
    レンダラーを再利用するせいでタブ4枚を18MBと採点した（別テスト参照）。"""
    src = inspect.getsource(S.route_evaluator_for)
    assert "private" in src, "commit ではなく RSS を読んでいる"
    body = _edge_mb_source()
    assert "baseline_total" in body, "木全体の基準を持っていない"


def test_the_baseline_is_reset_before_the_arm_not_inside_the_sampler():
    """measure_arm は run_goals の前に1回サンプルを取る。サンプラ内で遅延初期化すると、
    その1回 -- start_mb になる値 -- だけ前の腕のベースラインを引き継ぐ。"""
    src = inspect.getsource(S.route_evaluator_for)
    # docstring が伸びた分で落ちないよう、位置ではなく「腕の本体の最初の実文」を見る。
    for arm, end in (("def _control():", "def _candidate():"),
                     ("def _candidate():", "def _warmup")):
        body = src[src.index(arm):src.index(end)]
        assert "_begin_attribution()" in body, arm
        assert body.index("_begin_attribution()") < body.index("RV.measure_arm"), arm


def test_the_renderer_count_is_reported_because_the_mechanism_beats_the_statistic():
    """観測された run 間スイングは判定閾値の約4倍。単発 run で検出しようとするより、
    『socket ゴールはレンダラーを生まない』を測って算術で出すほうが強い。"""
    src = inspect.getsource(S.route_evaluator_for)
    assert "new_renderers" in src and '"renderers"' in src


def test_a_warmup_pass_is_available_and_its_numbers_are_discarded():
    """セッション最初の腕はレンダラー生成・認証・セッション確立の代金を払う。"""
    body = _inner_source("_warmup")
    assert "goals[:1]" in body
    # ast.unparse は引用符を正規化するので、引用符ごと照合しない。
    assert "_floor[" in body and "min_free_mb" in body, \
        "捨て走行の圧が本測定の床判定に残る"
    # 温めは常にタブで行う。Edge のレンダラープールを温めるのはタブだけなので、
    # 経路に追随させると比較する2群で条件が変わる。
    assert "socket_on=False" in body
    src = inspect.getsource(S.route_evaluator_for)
    assert src.count("_warmup()") >= 4, "腕ごとに呼ばれていない"


def test_the_sum_is_signed_at_the_level_of_the_whole_tree():
    """プロセス単位の増分を足し上げる形（減った分は0に潰す）は 2026-08-24 に外した。

    それが正しく見えた理由も残す: 最初の版は『腕が新たに生んだプロセス』だけを数え、
    タブ4枚を開いた腕が新規1プロセス18MBを返した。Edge はレンダラープールを再利用
    するので、既存プロセスの増分を見る必要は本当にあった。誤りは「増分を見る」ことでは
    なく「減少を0に潰す」ことだった。

    潰すと churn が増分に化ける。腕の途中でレンダラーが入れ替わると、消えた側は0に
    潰され、代わりの pid は基準ゼロ扱いなので定常値まるごとが腕の増分になる。実メモリ
    不変で数百MB。腕を1本も走らせずに実証済み: アイドルのブラウザ2分で旧統計量が
    82.1MB を出し、符号付き差分は 6.1MB で終わった（新規プロセス3個）。"""
    body = _edge_mb_source()
    assert "grew" not in body, "プロセス単位の片側加算が戻っている"
    assert "now_total" in body and "baseline_total" in body


def test_the_floors_own_justification_is_recorded_as_void():
    """床の根拠は『OS のトリミングがこの腕に credit を払う』だった。
    しかしこのサンプラは commit を読む -- トリミングでは動かない量。
    床は、読む量を選んだ時点で消えていた脅威から守っていた。"""
    body = _edge_mb_source()
    assert "COMMIT" in body or "commit" in body
    assert "trim" in body.lower()


def test_the_baseline_waits_for_the_browser_to_settle_not_for_a_guessed_duration():
    """基準が1標本だと、その瞬間の過渡状態が走行の残り全部に乗る。
    固定 1.6 秒でも足りなかった -- 2026-08-24 の実測で、socket 腕は2本とも
    終了時が −210 / −267MB。温めタブの解体が腕の開始後も続いており、
    膨らんだ値を基準にしていた。socket 腕はタブを開かないので減る一方になり、
    全区間が負 -> 凍結された measure_arm がピークを 0 に刈る。

    そしてこの刈り取りは処置走行で socket を必ず勝たせる: タブ腕の素直な
    ピークから、0 に刈られた socket 腕を引くことになる。帰無は静かで処置は
    大差という、今日3回出た『綺麗だが間違った答え』の完成形だった。

    そして3標本連続では足りなかった。0.5秒×3回で幅25MB未満は「毎秒16.7MB未満」で、
    捕まえたい漏れは毎秒3.2MB。実測で中央値1.1秒、24腕とも2.6秒を超えず通過していた。
    落ち着きは、遅い漏れが見える長さの窓で測る。"""
    from relay.selfimprove import scheduler as S

    # 判定は _edge_mb の中ではなく settle_baseline に在る。ループを閉じ込めたままだと
    # テストが再実装を検査することになり、それは本体と食い違ったまま緑であり続ける。
    body = inspect.getsource(S.settle_baseline)
    assert "SETTLE_TOLERANCE_MB" in body, "落ち着きを測っていない"
    assert "SETTLE_MAX_S" in body, "打ち切りが無い"
    assert "SETTLE_WINDOW_S" in body, "窓が無い -- 3標本では遅い漏れを見抜けない"
    # _edge_mb 側は、その判定を使っていること
    assert "settle_baseline" in _edge_mb_source()
    assert "max(span) - min(span)" in body, "窓全体の幅で見ていない"
    assert "probes[len(probes) // 2]" not in body, "固定回数の中央値に戻っている"

    # そして本当に見抜けること。文字列ではなく挙動で確かめる --
    # 前の版は自分のコメントで『静止を待つ』と書きながら待っていなかった。
    class _C:
        def __init__(self):
            self.t = 0.0

        def time(self):
            return self.t

        def sleep(self, dt):
            self.t += dt

    MB = 1024.0 * 1024.0
    c = _C()
    _lvl, _w, settled = S.settle_baseline(
        lambda: (1231.0 - 3.2 * c.t) * MB, sleep=c.sleep, now=c.time)
    assert not settled, "毎秒3.2MB 漏れているブラウザを整定と判定している"


def test_whether_it_settled_is_recorded():
    """打ち切りで抜けた走行は、落ち着いた走行と同じ意味を持たない。
    記録が無ければ、後から区別できない。"""
    src = inspect.getsource(S.route_evaluator_for)
    assert 'out["settled"]' in src and 'out["settle_s"]' in src


def test_the_tabs_case_was_already_working_before_this_change():
    """直す前にタブ帰無は +4.5 と −0.8 を出していた（世代5では +112.0 や −40.0）。
    符号付き差分は効いていた。壊れていたのは socket 腕だけで、
    原因は統計量ではなく基準を取る時点だった -- 両者を混ぜて『全部直した』と
    言わないために、ここに残す。"""
    body = _edge_mb_source()
    assert "now_total" in body and "baseline_total" in body


def test_the_peak_is_taken_from_a_smoothed_signal():
    """呼び出し側は標本の最大を取る。生の信号だと、腕が支えた水準ではなく
    最大の一過性を報告することになる。"""
    body = _edge_mb_source()
    assert "window" in body and "smoothed" in body


# ---- 閾値をノイズから較正するための帰無走行 ------------------------------------------------------

def test_a_null_run_makes_both_arms_the_control():
    """判定閾値 300MB は、いま壊れていると分かった総RSS計測から較正された値。
    観測結果に合わせて下げれば『定規を対象に合わせて削る』ことになる。
    同一の2腕がどれだけ離れて着地するかを測るのが、ゲームできない版の同じ問い。"""
    src = inspect.getsource(S.route_evaluator_for)
    assert "null_arm" in src
    i, j = src.index("def _candidate():"), src.index("def _warmup")
    body = src[i:j]
    # 対照腕を一般化したので、帰無腕は「基底に戻す」ではなく「対照と同じものを使う」。
    # 基底に固定したままだと、枝A対枝Bの帰無走行が枝A対基底を測ってノイズと呼ぶ。
    assert "control_manifest if null_arm" in body
    assert "bool(control_socket) if null_arm" in body, "帰無腕が対照とスイッチで食い違う"


def test_a_null_run_is_labelled_in_the_result():
    """帰無走行の数字が処置の結果として読まれると、
    『効果ゼロ』が『効果を検出できなかった』と混同される。"""
    src = inspect.getsource(S.route_evaluator_for)
    assert '"null_run"' in src


# ---- 腕が独立な実験単位であること ----------------------------------------------------------------

def test_each_arm_gets_its_own_memory_store():
    """フリートは `_with_theme_memory` でそのテーマの過去メモを送信本文に前置し、
    完了時に `record_task` で書き戻す。両腕が同じゴールを走るので、
    腕2は腕1が直前に書いたものを読んでいた -- 実際の走行記録に
    'The task is already complete per prior work memory' で始まる回答が残っている。
    腕2は仕事をしておらず、その commit 増分は仕事のコストではない。"""
    src = inspect.getsource(S.route_evaluator_for)
    assert "FLEET_STATE_DIR" in src, "記憶ストアを隔離していない"
    assert "isolate_memory" in src
    i = src.index("if isolate_memory:")
    block = src[i:i + 400]
    assert "_arm_seq" in block, "腕ごとに別のパスになっていない"


def test_memory_isolation_is_per_arm_and_not_a_global_switch():
    """運用者の実フリートの記憶を消してはいけない。消すのはこの腕の間だけ。"""
    src = inspect.getsource(S.route_evaluator_for)
    i = src.index("if isolate_memory:")
    block = src[i:i + 500]
    assert "os.environ[\"FLEET_STATE_DIR\"]" in block.replace("_os.", "os.")
    assert "record_task" not in block, "本番の記録機構そのものに手を入れている"


def test_the_estimand_is_named_in_the_code():
    """記憶を消すか残すかは推定対象の選択であって、掃除ではない。
    どちらを測っているか書いていなければ、読む側は取り違える。"""
    src = inspect.getsource(S.route_evaluator_for)
    assert "estimand" in src


def test_the_measurements_reach_the_field_the_ledger_reads():
    """最初の nightly() は actual_effect {} を記録した。評価器は両腕・gain・
    レンダラー数・メモリ床を返していたのに、契約が名指すフィールドを
    埋めていなかったので、durable record に残った数値は
    たまたま文章の中にあった1つだけだった。"""
    src = inspect.getsource(S.route_evaluator_for)
    assert '"actual_effect"' in src
    # rindex: 最初の一致は abort 分岐のもの。判定分岐のほうを見る。
    # 固定幅の窓はコメントが増えるだけで検査が落ちる -- 実際そうなった。
    # 辞書の終わりまでを見る。
    i = src.rindex('"actual_effect": {')
    block = src[i:src.index(chr(10) + " " * 8 + "}", i)]
    for key in ("control", "candidate", "memory_gain_mb", "renderers", "arm_order"):
        assert key in block, key


def test_an_aborted_run_also_records_what_it_saw():
    """床を割った走行も『何を見たか』は残す。判定は出さないが観測は消さない。"""
    src = inspect.getsource(S.route_evaluator_for)
    i = src.index('low < RV.MIN_FREE_MB')
    assert '"actual_effect"' in src[i:i + 1600]


def test_no_goal_set_hands_the_harness_an_8_3_short_path():
    """`TEMP` はこの端末では短縮形を返す。短縮名を含むパスを渡すと、
    ハーネスがそれを突き合わせる手間が測定値に乗る -- しかもどちらの腕に
    乗るかは引きなので、輸送の効果と見分けがつかない。

    multiturn 集合で見つけて直したが、飽和集合は同じ欠陥を8走行ぶん抱えたままだった。
    片方だけ直して同じ穴を残さないよう、検査は両方を横断する。"""
    from scripts.run_route_campaign import GOALS
    from scripts import workload_multiturn as W

    for name, goals in (("saturated-v1", GOALS), ("multiturn", W.goals())):
        for g in goals:
            assert "~" not in g["text"], (name, g["text"][:70])
