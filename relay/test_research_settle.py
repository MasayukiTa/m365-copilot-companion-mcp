"""Research プロファイルの完了検出。settle 4実装のうち最後の1つ。

3つの欠陥が同居していた:

  * `DONE_MARKERS` の5語のうち3語は、完成レポートの**本文**が普通に含む語。
    スライドについて調べたレポートは第2段落で "PowerPoint" と書く。判定は
    「どこかにマーカー OR 1000字以上」だったので、たまたまその語を含む短い
    状態表示が最終レポートとして capture された。
  * `_looks_like_clarification` の上限 1500 と `substantial` の下限 1000 が
    **重複**していた。1000〜1500字の完成レポートが承認語を含むと質問と誤認され、
    承認文を送り返して本物のレポートを捨て、ターンはタイムアウトした。
  * サンプル要求が無く dwell を無条件に倍にするだけ。数分ストリームするレポートが
    2×dwell より長く止まれば、その中断点で capture される。

マーカーと帯の修正はゲート無しで入れる -- それ自体が誤っている述語の訂正であって、
settle 方針の変更ではないため。settle 規則の採用だけが `MCP_SETTLE_UNIFIED` の下。
"""
import time

from relay import agent_profiles as AP
from relay import settle as S


# ---- マーカーは位置で判断する -------------------------------------------------------------------

def test_a_status_line_that_merely_mentions_powerpoint_is_not_a_finished_report():
    """これが早期 capture の実際の形。"""
    assert AP._report_marker("進捗: PowerPoint への変換を検討中です。" + "x" * 200) is False


def test_a_long_report_discussing_powerpoint_is_not_finished_either():
    """本文がその語を含むのは当たり前で、それを完了の証拠にはできない。"""
    assert AP._report_marker("PowerPoint について論じる。" + "y" * 3000) is False


def test_the_completion_header_at_the_top_counts():
    assert AP._report_marker("推論が 12 ステップで完了しました\n" + "body" * 300) is True


def test_the_same_header_buried_in_the_body_does_not():
    """位置が信号の半分。本文中の言及と UI の完了通知は別物。"""
    assert AP._report_marker("z" * 2000 + "推論が 5 ステップで完了") is False


def test_the_affordance_row_counts_only_under_an_actual_body():
    """`t[-600:]` は 200 字の状態表示では全文になる -- アンカーが何も買っていない。
    書き直しの中でその失敗が生き延びていたので、明示的に固定する。"""
    assert AP._report_marker("body" * 300 + "\nに変換: PowerPoint") is True
    assert AP._report_marker("に変換: PowerPoint") is False


def test_the_bare_word_matching_is_gone():
    """"推論が" 単独は本文に出る。形として一致させる。"""
    assert AP._report_marker("推論がどう進むかを説明する。" + "x" * 100) is False


# ---- 重複していた帯 -----------------------------------------------------------------------------

def test_a_finished_report_of_the_overlapping_length_is_not_read_as_a_question():
    """1000〜1500 字は両方の帯に属していた。承認語を含めば質問と誤認され、
    承認文が送り返され、本物のレポートは捨てられ、ターンはタイムアウトした。"""
    report = "でよいですか " + "x" * 1200
    assert AP._looks_like_clarification(report) is False


def test_a_real_clarification_is_still_recognised():
    """厳しくするだけなら簡単。短い問い返しは従来どおり承認経路へ行かねばならない。"""
    assert AP._looks_like_clarification("対象範囲は直近3年でよいですか？") is True


def test_the_two_bands_no_longer_touch():
    """100字の緩衝。どちらかが忍び寄っても気づけるように、数として固定する。"""
    assert AP.SUBSTANTIAL_CHARS == 1000
    long_enough_to_be_a_report = "でよいですか " + "x" * 950
    assert AP._looks_like_clarification(long_enough_to_be_a_report) is False
    assert len(long_enough_to_be_a_report) < AP.SUBSTANTIAL_CHARS, (
        "緩衝帯そのものを測っている -- 900 と 1000 の間はどちらでもない")


def test_the_clarification_test_uses_the_strict_finished_report_evidence():
    """緩い語リストで「完成レポートだから質問ではない」と判定していた --
    正しい答えを誤った根拠で出しており、語リストが変わった瞬間に壊れる。"""
    import inspect
    src = inspect.getsource(AP._looks_like_clarification)
    assert "_report_marker" in src and "DONE_MARKERS" not in src


# ---- settle 規則の採用（ゲート下） ---------------------------------------------------------------

class _Drv:
    def __init__(self, script):
        self.script, self.i, self.accepted = list(script), 0, []

    def _answers(self):
        class _L:
            def count(_self):
                return 99
        return _L()

    def read_last_response(self):
        t = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return t

    def _is_stale_repeat(self, _t):
        return False

    def _accept_new_reply(self, t):
        self.accepted.append(t)

    def send(self, _t):
        pass


class _Session:
    timeout_s = 10_000
    dwell_s = 2.0
    approval = ""
    page = None

    def __init__(self, script):
        self.drv = _Drv(script)
        self._count_before = 0
        self._t_send = time.time()
        self._last = None
        self._stable_since = None
        self._settle_state = S.SettleState()
        self._approved = True
        self._done = None
        self._pending_open = False
        self._report_full = ""
        self.finished = []

    def _finish(self, report):
        self.finished.append(report)
        self._done = report or ""

    def _persist(self):
        pass

    def close(self):
        pass

    poll = AP.ResearchSession.poll


def _run(script, *, unified, monkeypatch, polls=8, tick=3.0):
    monkeypatch.setenv("MCP_SETTLE_UNIFIED", "1" if unified else "0")
    s = _Session(script)
    base = [time.time()]
    monkeypatch.setattr(time, "time",
                        lambda: base.__setitem__(0, base[0] + tick) or base[0])
    for _ in range(polls):
        s.poll()
        if s._done is not None:
            break
    return s


def test_a_finished_report_still_comes_back_under_the_unified_rule(monkeypatch):
    report = "推論が 9 ステップで完了しました\n" + "本文" * 800
    s = _run([report] * 8, unified=True, monkeypatch=monkeypatch)
    assert s.finished and s.finished[0].startswith("推論が")
    assert s._report_full == report, "全文を保持していない -- サブトランスクリプトが痩せる"


def test_a_paused_stream_is_not_captured_at_the_pause(monkeypatch):
    """マーカーの無い長文が一瞬止まっただけで確定させない。"""
    partial = "途中まで書かれた本文" * 120          # substantial だがマーカー無し
    legacy = _run([partial], unified=False, monkeypatch=monkeypatch, polls=2, tick=30.0)
    unified = _run([partial], unified=True, monkeypatch=monkeypatch, polls=2, tick=30.0)
    assert legacy.finished, "旧経路が中断点で確定しなかった -- 前提が変わっている"
    assert unified.finished == [], "統一経路が中断点で確定した"


def test_a_short_status_line_is_never_the_report_in_either_path(monkeypatch):
    """`substantial` の要求は統一経路でも残す -- settle しただけの状態表示を
    レポートとして返さない。"""
    status = "進捗: PowerPoint への変換を検討中です。"
    for unified in (False, True):
        s = _run([status] * 8, unified=unified, monkeypatch=monkeypatch)
        assert s.finished == [], "unified=%s で状態表示を capture した" % unified
