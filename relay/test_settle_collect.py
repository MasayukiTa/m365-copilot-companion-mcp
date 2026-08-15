"""The turn generator: does it drive the predicate under study, and refuse when it would not?

The first generator drove 44 benchmark episodes through the bridge and recorded two turns,
neither of them an episode -- because the interactive bridge turn does not enter `wait_for_idle`
at all. An hour of a live tenant produced nothing replayable, and the run reported success
throughout. So the tests here are mostly about the two ways that repeats: driving something
that is not the predicate, and recording in a mode that cannot be replayed.
"""
from __future__ import annotations

import os

import pytest

from relay import settle_collect as SC


class _Driver:
    """Records what it was asked to do."""

    instances = []

    def __init__(self, page):
        self.page = page
        self.sent = []
        self.waited = []
        _Driver.instances.append(self)

    def send(self, text):
        self.sent.append(text)

    def wait_for_idle(self, **kw):
        self.waited.append(kw)
        return True


@pytest.fixture()
def driven(monkeypatch):
    """Run collect() against a fake browser, returning the drivers it built."""
    _Driver.instances = []
    pages = []

    class _Ctx:
        pages = []

        def new_page(self):
            return object()

    class _Browser:
        contexts = [_Ctx()]

    class _PW:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        @property
        def chromium(self):
            class _C:
                def connect_over_cdp(self, _url):
                    return _Browser()
            return _C()

    monkeypatch.setitem(os.environ, "MCP_SETTLE_TRACE_COLLECT", "1")
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: _PW())

    import relay.copilot_autopilot_relay as CAR
    monkeypatch.setattr(CAR, "CopilotWebDriver", _Driver)
    monkeypatch.setattr(CAR, "find_conversation_page",
                        lambda ctx, url: pages.append(url) or object())
    return _Driver.instances, pages


# ---- what it drives -----------------------------------------------------------------------

def test_it_waits_with_the_real_predicate_under_study(driven):
    """ベンチをブリッジ経由で44エピソード流して、記録されたターンは2件。
    対話ブリッジのターンは wait_for_idle に入らないため。
    計器が対象の述語を通っていなければ、実テナントを1時間動かしても何も得られない。"""
    drivers, _ = driven
    SC.collect(cdp_url="http://x", agent_url="http://agent", turns=3)
    assert len(drivers) == 3
    assert all(d.waited for d in drivers), "settle 述語を通っていない"


def test_every_turn_starts_a_fresh_conversation(driven):
    """前のターンの本文を引き継ぐと、安定カウントの基準が誤る -- 計測自身が入れる欠陥。"""
    _, pages = driven
    SC.collect(cdp_url="http://x", agent_url="http://agent", turns=3)
    assert len(pages) == 3


def test_the_prompts_vary(driven):
    """同一プロンプトの反復は標本ではなく、同じ応答形状を繰り返し引くだけ。"""
    drivers, _ = driven
    SC.collect(cdp_url="http://x", agent_url="http://agent", turns=6)
    sent = [d.sent[0] for d in drivers]
    assert len(set(sent)) == 6


def test_the_prompts_ask_for_a_range_of_answer_lengths():
    """述語が判定しているのは応答の長さとストリーミングの形。
    プロンプトの文字数は応答長の代理にならないので、要求している分量の方を見る。"""
    import re

    asked = sorted(int(m.group(1))
                   for p in SC.PROMPTS
                   for m in [re.search(r"about (\d+) words", p)] if m)
    assert asked, "分量を指定したプロンプトが1つも無い"
    assert min(asked) <= 120 and max(asked) >= 250, "要求分量が狭い範囲に固まっている"
    # and at least one that asks for a single token, where an early accept is most likely
    assert any("exactly the word" in p for p in SC.PROMPTS)


# ---- what it refuses ----------------------------------------------------------------------

def test_it_refuses_to_drive_a_tenant_without_collect_mode(monkeypatch):
    """通常モードは60秒超のターンしか記録せず全文も持たない。
    そのまま30分回せば、残るのは再生できないファイルだけ。"""
    monkeypatch.delenv("MCP_SETTLE_TRACE_COLLECT", raising=False)
    with pytest.raises(SystemExit) as exc:
        SC.collect(cdp_url="http://x", agent_url="http://agent", turns=1)
    assert "nothing replayable" in str(exc.value)


def test_one_failing_turn_does_not_end_the_run(driven, monkeypatch):
    """settle しなかったターンは誤りではなくデータ。それで収録全体を失うのは割に合わない。"""
    drivers, _ = driven
    calls = {"n": 0}

    def flaky(self, text):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("page went away")
        self.sent.append(text)

    monkeypatch.setattr(_Driver, "send", flaky)
    out = SC.collect(cdp_url="http://x", agent_url="http://agent", turns=4)
    assert out["turns_failed"] == 1
    assert out["turns_driven"] == 3


# ---- the agent-url incident ---------------------------------------------------------------

def test_a_research_agent_url_is_refused():
    """リサーチ エージェントは問い合わせにスコーピング質問を返して待つ。
    その質問自体が短く安定した応答なので wait_for_idle は受理し、
    実行は『ok』を出しながら何もストリームしていない。
    さらに停止したターンごとに運用者のスマホへ通知が飛ぶ -- 計測が人に仕事を作っていた。"""
    with pytest.raises(SystemExit) as exc:
        SC.refuse_an_agent_url(
            "https://m365.cloud.microsoft/chat/agent/P_552e6eda-fc18-7fb9-0ef6-1bf2de3393e4.dr_work")
    assert "scoping question" in str(exc.value)


def test_the_other_agent_url_shape_is_refused_too():
    with pytest.raises(SystemExit):
        SC.refuse_an_agent_url("https://m365.cloud.microsoft/agents/researcher/conversation/abc")


def test_the_plain_chat_is_accepted():
    SC.refuse_an_agent_url(SC.DEFAULT_CHAT_URL)          # must not raise


def test_collect_refuses_an_agent_url_before_touching_the_browser(monkeypatch):
    """ブラウザに触る前に断ること。断るのが遅ければ、会話は既に作られている。"""
    monkeypatch.setitem(os.environ, "MCP_SETTLE_TRACE_COLLECT", "1")
    with pytest.raises(SystemExit):
        SC.collect(cdp_url="http://x",
                   agent_url="https://m365.cloud.microsoft/chat/agent/P_x.dr_work", turns=1)
