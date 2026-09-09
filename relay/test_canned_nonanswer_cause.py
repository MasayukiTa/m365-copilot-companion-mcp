"""定型の無回答には原因が2つ以上あり、検出器は1つしか知らなかった。

この検出器は 2026-07-03 に、headless の窓詰まりで `?titleId=` のカスタムエージェントが
解決できず、MCPコネクタの無い既定 Copilot に落ちる事象のために書かれた。そしてそれ以降、
定型文を見たら常にその原因を主張していた。

2026-08-25 の実測: goal 4本を2回。同じ goal が2回とも定型文を引き、同じ走行の隣の
ワーカーは正しく答え、うち1つは CloseIntentTool を呼んでいた -- つまりカスタム
エージェントは解決しており、コネクタは在った。返ってきたのは「その指示に対する拒否」で、
それが INFRA_STUCK として、しかも「Edge をヘッドフルで再起動せよ」という案内つきで
報告された。8 goal 中3本、運用者をブラウザ調査へ送り出していた。
"""
import time

from relay import relay_fleet as F


def test_a_plausible_reply_is_not_proof(monkeypatch):
    """既定 Copilot も『東京』とは答えられる。文面が正しく見えることは、
    どちらのエージェントが答えたかについて何も証明しない。"""
    import inspect
    # connector_proven now delegates to connector_proof_source, which is where the evidence
    # is read. A source assertion has to follow the logic it is guarding, or it silently
    # guards an empty wrapper.
    src = inspect.getsource(F.connector_proof_source)
    assert "last_inbound_ts" in src, "証拠が文字列照合に戻っている"


def _probe(monkeypatch, value):
    """実物の tool_probe に差し替えを当てる。

    sys.modules["tools.tool_probe"] を置き換えても `from tools import tool_probe` は
    パッケージ属性を見るので効かない -- 最初にそう書き、2つのテストがどちらも実機の
    タイムスタンプを読んでいた。片方はたまたま通っていただけで、今夜2度目の
    「実機を読んで別の理由で緑になる」だった。"""
    from tools import tool_probe
    if isinstance(value, Exception):
        def boom():
            raise value
        monkeypatch.setattr(tool_probe, "last_inbound_ts", boom)
    else:
        monkeypatch.setattr(tool_probe, "last_inbound_ts", lambda: value)


def test_an_arriving_tool_call_is_proof(monkeypatch):
    """カスタムエージェントにしかできないのは MCP コネクタに届くこと。
    届けば、この機械自身のサーバが刻む。"""
    monkeypatch.setattr(F, "_PROCESS_START", 1000.0)
    _probe(monkeypatch, 2000.0)
    assert F.connector_proven() is True


def test_no_tool_call_since_the_run_began_is_not_proof(monkeypatch):
    """走行より前の呼び出しは、この走行のコネクタについて何も言わない。"""
    monkeypatch.setattr(F, "_PROCESS_START", 3000.0)
    _probe(monkeypatch, 2000.0)
    assert F.connector_proven() is False


def test_unknowable_is_not_proven(monkeypatch):
    """判定できないときは従来の診断を残す。保守的な向きはこちら --
    ブラウザを見に行かせて時間を無駄にするほうが、壊れたコネクタを『問題なし』と
    言って走行を失うよりまし。"""
    monkeypatch.setattr(F, "_PROCESS_START", 1000.0)
    _probe(monkeypatch, RuntimeError("probe unavailable"))
    assert F.connector_proven() is False


def test_the_two_causes_get_different_outcomes():
    """コネクタが証明済みなら REFUSED、そうでなければ INFRA_STUCK。
    同じ結末に丸めると、言い換えれば済む話にブラウザ再起動を勧め続けることになる。"""
    import inspect
    src = inspect.getsource(F.RelayWorker._decide)
    assert "connector_proof_source()" in src, "判定が原因を分けていない"
    assert '"REFUSED"' in src, "拒否という結末が無い"
    i = src.index("connector_proof_source()")
    near = src[i:i + 2400]
    assert "REFUSED" in near and "INFRA_STUCK" in near, "分岐の両側が揃っていない"


# ===========================================================================================
# 2026-09-09: "one process per run, so process scope is run scope" has a hole in it.
#
# It holds only while a run has SIBLINGS. A single-goal run has one worker, and that worker is
# the only possible witness -- so if its first reply is the canned non-answer, no tool call can
# ever land in this process and the connector can never be proven, whatever the truth is.
# Measured: an autostarted single-goal run was filed INFRA_STUCK at 11:34 while
# .fleet/probe_inbound.json recorded a real list_directory call arriving at 11:47. A
# 900-worker run never shows this -- one sibling proves it within seconds -- so the
# classification was reliable exactly where it was not needed.
#
# These tests pass an explicit `now`. The pre-existing tests above use tiny fake timestamps
# (1000.0, 2000.0) against the real clock, so they land outside any window by accident rather
# than by intent; a window test written that way would prove nothing.
# ===========================================================================================


def test_a_lone_worker_inherits_proof_from_the_liveness_probe(monkeypatch):
    """The regression. Nothing in this process called a tool -- there was nobody to."""
    monkeypatch.setattr(F, "_PROCESS_START", 5000.0)
    monkeypatch.setattr(F, "CONNECTOR_PROOF_WINDOW_S", 1200.0)
    _probe(monkeypatch, 4700.0)                     # 300s before this run began
    assert F.connector_proof_source(now=5000.0) == "probe"
    assert F.connector_proven(now=5000.0) is True


def test_proof_older_than_the_window_is_not_proof(monkeypatch):
    """Recency is the whole reason a foreign process's stamp means anything. Yesterday's
    successful tool call says nothing about the connector now."""
    monkeypatch.setattr(F, "_PROCESS_START", 5000.0)
    monkeypatch.setattr(F, "CONNECTOR_PROOF_WINDOW_S", 1200.0)
    _probe(monkeypatch, 3000.0)                     # 2000s old, window is 1200s
    assert F.connector_proof_source(now=5000.0) == ""
    assert F.connector_proven(now=5000.0) is False


def test_never_seen_is_not_proof(monkeypatch):
    """0.0 means no stamp has ever been written. An age computed from it would look ancient,
    but relying on that is relying on an accident -- so it is rejected explicitly."""
    monkeypatch.setattr(F, "_PROCESS_START", 5000.0)
    monkeypatch.setattr(F, "CONNECTOR_PROOF_WINDOW_S", 1200.0)
    _probe(monkeypatch, 0.0)
    assert F.connector_proof_source(now=5000.0) == ""


def test_a_stamp_from_the_future_is_a_clock_fault_not_proof(monkeypatch):
    """A skewed clock would otherwise read as permanently proven."""
    monkeypatch.setattr(F, "_PROCESS_START", 5000.0)
    monkeypatch.setattr(F, "CONNECTOR_PROOF_WINDOW_S", 1200.0)
    _probe(monkeypatch, 9000.0)                     # ahead of `now`, but also > _PROCESS_START
    # > _PROCESS_START still counts as run-scoped proof (a real call did arrive during the run);
    # what must not happen is the WINDOW accepting a future stamp on its own.
    monkeypatch.setattr(F, "_PROCESS_START", 99000.0)
    assert F.connector_proof_source(now=5000.0) == ""


def test_in_run_proof_still_wins_and_is_reported_as_such(monkeypatch):
    """A sibling answering is stronger evidence than the probe, and the REFUSED message says
    so. Reporting the wrong evidence is a defect even when the verdict is right."""
    monkeypatch.setattr(F, "_PROCESS_START", 5000.0)
    monkeypatch.setattr(F, "CONNECTOR_PROOF_WINDOW_S", 1200.0)
    _probe(monkeypatch, 5500.0)
    assert F.connector_proof_source(now=5600.0) == "run"


def test_the_window_is_disabled_when_the_probe_is(monkeypatch):
    """MCP_TOOL_PROBE_SEC=0 turns the probe off, so there is no independent stamp to trust and
    the rule falls back to process scope alone."""
    monkeypatch.setattr(F, "_PROCESS_START", 5000.0)
    monkeypatch.setattr(F, "CONNECTOR_PROOF_WINDOW_S", 0.0)
    _probe(monkeypatch, 4900.0)
    assert F.connector_proof_source(now=5000.0) == ""


def test_the_infra_message_labels_its_guess_as_a_guess():
    """The old wording asserted the headless/?titleId= fallback as the cause. Nothing in that
    branch measures it -- it only knows no tool call arrived. A reader took that sentence for a
    finding and reported it as the root cause, which cost a full investigation."""
    import inspect
    src = inspect.getsource(F.RelayWorker._decide)
    # _decide names INFRA_STUCK ten times, 23k characters apart. index() takes the first,
    # so the window landed nowhere near the branch under test and the test failed for a
    # reason unrelated to what it checks. Scan every occurrence.
    spots = [i for i in range(len(src)) if src.startswith("INFRA_STUCK", i)]
    assert any('仮説' in src[i:i + 1400] for i in spots), (
        'INFRA_STUCK の文言が仮説を仮説と名乗していない')
