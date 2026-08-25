"""閉じたページから復帰できること。

`.fleet/delete_log.jsonl` の記録: 削除試行14件中12件が失敗し、最大の塊(5件)が
TargetClosedError。原因はページが死んだことではなく、死んだ後に戻る道が無かったこと。

`_find_or_open_agent(ctx)` は起動時に1回だけ、`ctx` をローカル変数として呼ばれていた。
だから会話のリサイクル、Edge の再起動、レンダラのクラッシュでページが閉じると、
bridge は人が再起動するまで全ての要求に TargetClosedError を返し続けた。
"""
import pathlib

import bridge.copilot_bridge as CB

ROOT = pathlib.Path(__file__).resolve().parents[1]


class _Page:
    def __init__(self, closed=False):
        self._closed = closed
        self.url = "https://x/chat/agent/T_a"

    def is_closed(self):
        return self._closed


class _DeadHandle:
    """閉じた handle は is_closed を訊いただけで例外を投げることがある。"""
    def is_closed(self):
        raise RuntimeError("Target page, context or browser has been closed")


def test_a_live_page_is_left_alone(monkeypatch):
    """生きているページを開き直せば、そのページが抱えている会話を捨てることになる。"""
    live = _Page()
    monkeypatch.setattr(CB, "PAGE", live)
    monkeypatch.setattr(CB, "CTX", object())
    called = []
    monkeypatch.setattr(CB, "_find_or_open_agent", lambda ctx: called.append(1) or _Page())
    assert CB.ensure_page_alive() is True
    assert called == [], "生きているページを開き直した"
    assert CB.PAGE is live


def test_a_closed_page_is_reopened(monkeypatch):
    fresh = _Page()
    monkeypatch.setattr(CB, "PAGE", _Page(closed=True))
    monkeypatch.setattr(CB, "CTX", object())
    monkeypatch.setattr(CB, "_find_or_open_agent", lambda ctx: fresh)
    monkeypatch.setattr(CB, "CopilotWebDriver", lambda pg: ("driver", pg))
    assert CB.ensure_page_alive() is True
    assert CB.PAGE is fresh
    assert CB.DRIVER == ("driver", fresh), "ドライバを作り直していない"


def test_a_handle_that_raises_counts_as_dead(monkeypatch):
    """死んだ handle は問い合わせ自体が失敗する。そこで例外を上げたら復帰できない。"""
    fresh = _Page()
    monkeypatch.setattr(CB, "PAGE", _DeadHandle())
    monkeypatch.setattr(CB, "CTX", object())
    monkeypatch.setattr(CB, "_find_or_open_agent", lambda ctx: fresh)
    monkeypatch.setattr(CB, "CopilotWebDriver", lambda pg: pg)
    assert CB.ensure_page_alive() is True
    assert CB.PAGE is fresh


def test_without_a_context_it_reports_failure_rather_than_raising(monkeypatch):
    """起動前や CDP が落ちている間に呼ばれうる。落とさずに False を返すこと。"""
    monkeypatch.setattr(CB, "PAGE", _Page(closed=True))
    monkeypatch.setattr(CB, "CTX", None)
    assert CB.ensure_page_alive() is False


def test_reopening_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(CB, "PAGE", _Page(closed=True))
    monkeypatch.setattr(CB, "CTX", object())

    def boom(ctx):
        raise RuntimeError("cdp gone")

    monkeypatch.setattr(CB, "_find_or_open_agent", boom)
    assert CB.ensure_page_alive() is False


def test_navigation_checks_before_spending_three_timeouts():
    """閉じたページでは3回とも同じように失敗する。先に確認しなければ、
    タイムアウトを3回払ってから『到達不能』と報告するだけになる。"""
    src = (ROOT / "bridge" / "copilot_bridge.py").read_text(encoding="utf-8", errors="replace")
    i = src.index("def _goto_settled(")
    j = src.index("for _ in range(max(1, tries)):", i)
    assert "ensure_page_alive()" in src[i:j], "navigation の前に確認していない"


def test_the_context_is_kept_so_a_reopen_is_possible():
    """ctx がローカル変数だったことが、復帰不能の直接の原因だった。"""
    src = (ROOT / "bridge" / "copilot_bridge.py").read_text(encoding="utf-8", errors="replace")
    assert "\nCTX = None" in src
    assert "        CTX = ctx" in src, "起動時に context を保持していない"
    assert "global PAGE, DRIVER, CTX," in src
