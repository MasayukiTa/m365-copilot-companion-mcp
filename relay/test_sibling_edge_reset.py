"""共有 Edge を、まだ動いている兄弟走行から引き抜かないこと。

このリポジトリでは複数のフリート走行が並走するのが普通で、companion Edge プロファイル
(:9222) は分離できない。2026-08-25、4本が同居しているところへ5本目が起動し、空きRAMが
自分のリサイクル閾値を割っているのを見て**共有ブラウザを hard-reset** した。隣の走行は
実行中コンテキストを失い、ゴールを再投入し直した。同日3本の走行で計6回。

任意のリセット（停滞watchdog・起動時リサイクル）だけを兄弟認識にする。
本当に死んでいるブラウザのリセットは抑止しない -- 守るものが残っていない。
"""
import relay.edge_recover as ER


def test_the_port_defaults_to_the_companion_edge():
    assert ER._cdp_port_of("python -m relay.fleet_runner --goals-file x.jsonl") == 9222


def test_an_explicit_cdp_url_is_read_from_the_command_line():
    assert ER._cdp_port_of("python -m relay.fleet_runner --cdp-url http://127.0.0.1:9333") == 9333


def test_runs_on_another_port_are_not_siblings(monkeypatch):
    """別ポート＝別ブラウザ。巻き添えにならないし、抑止の理由にもならない。"""
    monkeypatch.setattr(ER, "os", ER.os)
    procs = [{"pid": 111, "name": "python.exe",
              "cmdline": ["python", "-m", "relay.fleet_runner", "--cdp-url", "http://x:9333"]}]
    _install(monkeypatch, procs)
    assert ER.other_fleet_runs(9222) == []


def test_a_run_on_the_same_port_is_a_sibling(monkeypatch):
    procs = [{"pid": 222, "name": "python.exe",
              "cmdline": ["python", "-m", "relay.fleet_runner", "--goals-file", "g.jsonl"]}]
    _install(monkeypatch, procs)
    assert ER.other_fleet_runs(9222) == [222]


def test_we_are_never_our_own_sibling(monkeypatch):
    import os as _os

    procs = [{"pid": _os.getpid(), "name": "python.exe",
              "cmdline": ["python", "-m", "relay.fleet_runner"]}]
    _install(monkeypatch, procs)
    assert ER.other_fleet_runs(9222) == []


def test_a_non_fleet_python_is_not_a_sibling(monkeypatch):
    procs = [{"pid": 333, "name": "python.exe", "cmdline": ["python", "main.py"]}]
    _install(monkeypatch, procs)
    assert ER.other_fleet_runs(9222) == []


def test_unknown_counts_as_alone(monkeypatch):
    """psutil が無い・列挙が失敗した、を『兄弟がいるかもしれない』に倒すと、
    詰まったブラウザを直せないまま走行が止まる。診断の欠落を障害にしてはいけない。"""
    class _Boom:
        @staticmethod
        def process_iter(_attrs):
            raise RuntimeError("no")

    monkeypatch.setitem(__import__("sys").modules, "psutil", _Boom)
    assert ER.other_fleet_runs(9222) == []


def _install(monkeypatch, procs):
    class _P:
        def __init__(self, info):
            self.info = info

    class _Fake:
        @staticmethod
        def process_iter(_attrs):
            return [_P(d) for d in procs]

    monkeypatch.setitem(__import__("sys").modules, "psutil", _Fake)


# ---- 配線: フラグを足しても、呼び出し側が渡していなければ何も変わらない ------------------------

def _runner_source():
    import inspect

    from relay import fleet_runner as FR

    src = inspect.getsource(FR)
    # コメントを除いて見る（説明文の中の文字列に反応して壊れる罠を既に踏んでいる）
    return "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))


def test_the_stall_watchdog_asks_before_it_resets():
    src = _runner_source()
    i = src.index("[watchdog] fleet stalled")
    assert "discretionary=True" in src[i:i + 400]


def test_the_startup_recycle_asks_before_it_resets():
    src = _runner_source()
    i = src.index("hard-resetting the companion Edge for a clean start")
    assert "discretionary=True" in src[i:i + 300]


def test_a_dead_browser_is_reset_without_asking():
    """cdp_alive が偽なら守るものは無い。ここで抑止すると走行が止まる。"""
    src = _runner_source()
    i = src.index("[recover] Edge unreachable")
    seg = src[i:i + 300]
    assert "hard_reset(port)" in seg and "discretionary" not in seg


def test_a_lost_context_no_longer_resets_a_healthy_browser():
    """コンテキスト喪失の原因が『兄弟がリセットした』ことなので、
    ここで無条件にリセットすると、1回のリセットが連鎖になる。"""
    src = _runner_source()
    i = src.index("if args.no_auto_recover or attempt > args.max_recover:")
    seg = src[i:i + 400]
    assert seg.index("cdp_alive(args.cdp_url)") < seg.index("hard_reset(port)")


def test_suppression_reports_which_runs_held_it():
    """『リセットしませんでした』だけでは、運用者が次に何を見ればいいか分からない。"""
    src = _runner_source()
    i = src.index("other_fleet_runs(port)")
    seg = src[i:i + 500]
    assert "other fleet run(s) are on this Edge" in seg
    assert "return False" in seg


def test_deferring_forever_is_not_an_option():
    """2走行が同じ詰まったEdgeに乗ると、双方が相手を見て譲り合い、対称に永久停止する。
    cdp_alive は『詰まっているが応答する』ブラウザに真を返すので他の逃げ道も無い。
    N回譲ったら取る -- 二重リセットは各走行がコンテキスト喪失経路で回復できるが、
    デッドロックは誰も回復できない。"""
    src = _runner_source()
    i = src.index("other_fleet_runs(port)")
    seg = src[i:i + 2200]
    assert "EDGE_SUPPRESS_MAX" in seg
    assert "resetting anyway" in seg
    assert "_suppressed[0] < EDGE_SUPPRESS_MAX" in seg


def test_the_deferral_counter_resets_when_a_reset_actually_happens():
    src = _runner_source()
    i = src.index("other_fleet_runs(port)")
    seg = src[i:i + 1800]
    assert "_suppressed[0] = 0" in seg


def test_the_ceiling_is_tunable_without_editing_source():
    src = _runner_source()
    assert 'os.environ.get("MCP_FLEET_EDGE_SUPPRESS_MAX"' in src


def test_memory_pressure_never_escalates_over_a_working_sibling():
    """escalation の根拠は「相手も進んでいない」で、それは停滞 watchdog の話。
    起動時のメモリリサイクルには当てはまらない -- 相手は普通に働いていることがある。
    ひとつのカウンタで両方を数えていたので、静かな3回の見送りの果てに、
    元気な兄弟から共有Edgeを引き抜くところだった。"""
    src = _runner_source()
    i = src.index("other_fleet_runs(port)")
    seg = src[i:i + 2200]
    assert "if not escalate:" in seg
    assert seg.index("if not escalate:") < seg.index("_suppressed[0] += 1")
    assert "a working sibling is not an emergency" in seg


def test_the_watchdog_is_the_one_allowed_to_escalate():
    src = _runner_source()
    i = src.index("[watchdog] fleet stalled")
    assert "escalate=True" in src[i:i + 400]


def test_the_memory_recycle_is_not():
    src = _runner_source()
    i = src.index("hard-resetting the companion Edge for a clean start")
    seg = src[i:i + 300]
    assert "discretionary=True" in seg and "escalate=True" not in seg


def test_the_tally_forgets_once_the_siblings_are_gone():
    """減衰しないカウンタは、何時間も前の見送りを今の判断に持ち込む。"""
    src = _runner_source()
    i = src.index("other_fleet_runs(port)")
    seg = src[i:i + 600]
    assert "if not others:" in seg and "_suppressed[0] = 0" in seg


def test_the_recycle_thresholds_can_be_reached_on_purpose():
    """定数のままだと、この経路とその上に乗る抑止/escalation を実機で一度も踏めない。"""
    import inspect

    src = inspect.getsource(ER)
    assert "MCP_EDGE_RECYCLE_CAP_MB" in src and "MCP_EDGE_RECYCLE_FLOOR_MB" in src


def test_a_fleet_driven_some_other_way_is_still_a_sibling(monkeypatch):
    """run_relay_fleet を import して回すベンチも同じ共有ブラウザを使う。
    見えていなかったので、抑止なしで足元のEdgeをリセットされていた
    -- この関数が止めるために存在する、まさにその場合。"""
    procs = [{"pid": 444, "name": "python.exe",
              "cmdline": ["python", "-m", "bench.review_run", "--use", "run_relay_fleet"]}]
    _install(monkeypatch, procs)
    assert ER.other_fleet_runs(9222) == [444]
