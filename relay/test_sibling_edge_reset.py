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


# --- owns_edge: the (user-data-dir, port) owner key the ps1 kill path mirrors -----------
# これらは実プロセス・pywin32 を使わず、偽のコマンドライン文字列だけで owns_edge を突く。
# ps1 の Get-OwnedEdgeProcesses と同じ規則（プロファイル名の部分一致ではなく、
# user-data-dir をトークン一致 ＋ --remote-debugging-port をトークン一致）を固定する。

_DD = r"C:\ProgramData\fleet\profiles\copilot-companion-edge"


def _main(dd, port, extra=""):
    return ('"C:\\Program Files\\msedge.exe" --user-data-dir="' + dd + '" '
            '--remote-debugging-address=127.0.0.1 --remote-debugging-port=' + str(port)
            + (' ' + extra if extra else ''))


def test_owns_edge_matches_our_profile_and_port():
    assert ER.owns_edge(_main(_DD, 9222), _DD, 9222) is True


def test_owns_edge_rejects_a_prefix_sibling_profile():
    """copilot-companion-edge は copilot-companion-edge-2 の部分文字列。
    旧コードの『プロファイル名 -match』だと隣プロファイルの Edge まで殺していた。"""
    sibling = _DD + "-2"
    assert ER.owns_edge(_main(sibling, 9222), _DD, 9222) is False


def test_owns_edge_rejects_the_wrong_port():
    """同じプロファイル文字列でも別ポートを駆動している Edge は我々のものではない。"""
    assert ER.owns_edge(_main(_DD, 9223), _DD, 9222) is False


def test_owns_edge_matches_a_bare_unquoted_user_data_dir():
    bare = ("msedge --user-data-dir=" + _DD
            + " --remote-debugging-port=9222 --no-first-run")
    assert ER.owns_edge(bare, _DD, 9222) is True


def test_owns_edge_rejects_port_that_is_only_a_prefix():
    """--remote-debugging-port=92220 は 9222 ではない。トークン境界で見る。"""
    assert ER.owns_edge(_main(_DD, 92220), _DD, 9222) is False


def test_owns_edge_matches_a_child_process_that_inherited_both_flags():
    """子プロセス（--type=renderer）も両フラグを継承する。kill 対象＝ツリー全体でよい。"""
    child = _main(_DD, 9222, extra="--type=renderer --lang=ja")
    assert ER.owns_edge(child, _DD, 9222) is True


def test_owns_edge_empty_or_missing_is_not_ownership():
    assert ER.owns_edge("", _DD, 9222) is False
    assert ER.owns_edge(_main(_DD, 9222), "", 9222) is False


# --- the ps1 kill path must mirror the same rule --------------------------------------------
# owns_edge は Python 側の分類器だが、実際に Stop-Process するのは
# scripts/start_companion_edge.ps1 の Get-OwnedEdgeProcesses。両者が同じ規則でないと意味がない。

def _ps1_source():
    import os as _os

    here = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    with open(_os.path.join(here, "scripts", "start_companion_edge.ps1"),
              encoding="utf-8") as fh:
        return fh.read()


def _owned_fn_body(src):
    """The text of the Get-OwnedEdgeProcesses function definition."""
    start = src.index("function Get-OwnedEdgeProcesses")
    return src[start:start + 900]


def test_ps1_has_one_owner_helper_every_kill_path_uses():
    src = _ps1_source()
    assert src.count("function Get-OwnedEdgeProcesses") == 1
    # Get-CompanionPids routes through it
    assert "Get-OwnedEdgeProcesses | ForEach-Object { [int]$_.ProcessId }" in src
    # Reset-CompanionSession kills what the helper returns
    reset = src[src.index("function Reset-CompanionSession"):]
    reset = reset[:reset.index("Start-Sleep")]
    assert "Get-OwnedEdgeProcesses" in reset and "Stop-Process" in reset
    # Test-CompanionHeadless reads the same owned set
    headl = src[src.index("function Test-CompanionHeadless"):]
    headl = headl[:headl.index("return $false")]
    assert "Get-OwnedEdgeProcesses" in headl


def test_ps1_owner_key_is_user_data_dir_token_not_profile_name():
    body = _owned_fn_body(_ps1_source())
    assert "--user-data-dir=" in body
    assert "[regex]::Escape($dataDir)" in body
    assert "[regex]::Escape($Profile)" not in body


def test_ps1_owner_key_takes_the_child_tree_but_binds_the_main_process_to_our_port():
    body = _owned_fn_body(_ps1_source())
    assert "--remote-debugging-port=" in body and "$Port" in body
    assert "--type=" in body


def test_ps1_kill_path_no_longer_selects_by_bare_profile_name():
    """旧欠陥そのもの: 三つの選択子が $Profile 部分一致で msedge を選んでいた。
    どの関数にも [regex]::Escape($Profile) を使った msedge 選択が残っていないこと。"""
    src = _ps1_source()
    for anchor in ("function Get-CompanionPids", "function Test-CompanionHeadless",
                   "function Reset-CompanionSession"):
        seg = src[src.index(anchor):]
        seg = seg[:seg.index("\n}\n")]
        assert "[regex]::Escape($Profile)" not in seg, anchor



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
    i = src.index("if _give_up:")
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
