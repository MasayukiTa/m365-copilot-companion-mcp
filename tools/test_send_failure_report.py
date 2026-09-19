# -*- coding: utf-8 -*-
"""3か月書かれ続けて、一度も読まれていなかった診断。

`relay/copilot_autopilot_relay.py::_snapshot` は 2026-06-13 から失敗した送信ごとに1行
書いている。2026-09-20 時点で **15,606行 / 8.5MB**。**追跡ファイルのどこにも読み手が無い。**

書き手は読まれる前提で作られている。欄を足したときのコメント自身がこう言っている —
「ここでは検証できなかった、ページの古さを誰も記録していなかったから。取るのは安く、
**ログだけで仮説を反証可能にする**」。3か月前に払った計測を、誰も見ていない。

## 本番の規則が、全データの否定する数字に依っていた

`_page_alive` は死んだページへの送信を即座に打ち切る。根拠として4箇所のコメントが
「send_failures.jsonl の TargetClosedError 競合、**28/72**」を引く — 39%、初日に手で数えた値。
**全ファイルでの実測は 29 / 15,606 = 0.19%。**

ただし結論は見出しより狭い。ここを混ぜないためにテストにも書いておく:

* **規則が誤りという意味ではない。** 本当に閉じたページを速く諦めるのは常に正しく、
  避けている費用（3回 × 12秒）は実在する
* **元の計数が誤りという意味でもない。** 72件は修正のきっかけになった障害のさなかに
  取られていて、まさにその失敗が集中する母集団
* **書かれ方の問題。** 窓を書かない「28/72」は、送信失敗の恒常的な性質に読める。
  3か月ではそうではない。**窓なしの率は、日付なしのベースラインと同じ欠陥**
* **修正自体が数字を動かす。** 死んだページを送信前に弾けば、この行を書く地点に届く送信が
  減る。0.19% は、その規則に形を変えられ続けたファイル上の値。読み手はこれを分離できず、
  きれいな前後比較のふりをしない
"""
from __future__ import annotations

import json

import pytest

from tools import send_failure_report as R


def _log(tmp_path, rows):
    p = tmp_path / "send_failures.jsonl"
    with open(str(p), "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return str(p)


def _row(**kw):
    base = {"ts": "2026-06-13T02:56:38+09:00", "conv_guid": "g", "attempt": 1,
            "phase": "waiting_processing",
            "tab": {"visibility_state": "visible", "has_focus": True},
            "composer": {"text_len": 1, "head80": "x"},
            "send_button": {"match_count": 1, "disabled": False, "visible": True}}
    base.update(kw)
    return base


# ───────────────────────────────────── 規則が引く数字

def test_a_closed_tab_is_counted(tmp_path):
    p = _log(tmp_path, [_row(), _row(tab={"visibility_state": R.TARGET_CLOSED,
                                          "has_focus": R.TARGET_CLOSED})])
    got = R.target_closed_rate(R.read(p))
    assert (got["target_closed"], got["rows_with_a_tab_probe"], got["rate"]) == (1, 2, 0.5)


def test_the_report_names_both_numbers_rather_than_replacing_one_with_the_other(tmp_path):
    """**0.19% を出して 28/72 を消したら、規則がなぜ在るのか読めなくなる。**
    片方だけ残すのは、今日ずっと潰してきた「完了を書かない文書」の逆向きの形。"""
    p = _log(tmp_path, [_row(tab={"visibility_state": R.TARGET_CLOSED})])
    out = R.report(p)
    assert "28/72" in out
    assert "100.00%" in out or "tab already closed" in out


# ───────────────────────────────────── 読み手自身が持っていた欠陥

def test_a_fully_errored_probe_is_counted(tmp_path):
    """**最初の版はここで 0 を返した。** `send_button.match_count` を見ていて、
    そこはページが死んでいても**ふつうの 0** が返る唯一の値だった。"""
    err = "err:AttributeError"
    p = _log(tmp_path, [_row(), _row(
        tab={"visibility_state": err, "has_focus": err},
        composer={"text_len": err, "head80": err},
        # ページが落ちていても match_count は 0 のまま — ここを鍵にすると数えられない
        send_button={"match_count": 0, "disabled": err, "visible": err})])
    got = R.probe_failure_rate(R.read(p))
    assert got["all_probes_errored"] == 1, "the numeric match_count is hiding the row again"


def test_a_healthy_row_is_not_counted_as_an_errored_probe(tmp_path):
    p = _log(tmp_path, [_row(), _row()])
    assert R.probe_failure_rate(R.read(p))["all_probes_errored"] == 0


def test_a_partially_errored_probe_is_not_counted(tmp_path):
    """「ページが答えない」と「一部の問い合わせが失敗した」は別。"""
    p = _log(tmp_path, [_row(tab={"visibility_state": "err:AttributeError",
                                  "has_focus": True})])
    assert R.probe_failure_rate(R.read(p))["all_probes_errored"] == 0


# ───────────────────────────────────── 言えないことを言わない

def test_the_age_summary_states_that_it_has_no_denominator(tmp_path):
    """**このファイルは失敗しか記録していない。** 若いページに失敗が積み上がるのは
    「若いと失敗しやすい」とも「送信はページを開いた直後に多い」とも等しく整合する。
    但し書きなしに分布だけ出したら、片側の標本から結論を作ることになる。"""
    p = _log(tmp_path, [_row(page_age_s=1.0), _row(page_age_s=900.0)])
    out = R.report(p)
    assert "no denominator" in out


def test_rows_without_the_later_fields_do_not_shrink_the_percentages(tmp_path):
    """**スキーマが途中で変わっている。** `page_age_s` は後から足された欄で、
    ファイル長で割ったら、欄が無かった時代の行の分だけ率が薄まる。"""
    p = _log(tmp_path, [_row(), _row(), _row(page_age_s=10.0)])
    got = R.page_age(R.read(p))
    assert got["rows_with_an_age"] == 1
    assert "1 rows carry it" in R.report(p)


def test_an_empty_log_says_nothing_rather_than_zero(tmp_path):
    assert "no records" in R.report(_log(tmp_path, []))


def test_a_missing_file_reads_as_empty(tmp_path):
    assert "no records" in R.report(str(tmp_path / "never_written.jsonl"))


def test_a_half_written_last_line_does_not_stop_the_report(tmp_path):
    """このファイルは生きたプロセスが追記している。読むのは書いている最中。"""
    p = tmp_path / "send_failures.jsonl"
    with open(str(p), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(_row()) + "\n")
        fh.write('{"ts": "2026-06-13T0')
    assert "1 records" in R.report(str(p))


@pytest.mark.parametrize("bad", [{"tab": "not a dict"}, {"tab": None}])
def test_a_probe_that_is_not_a_dict_is_skipped_rather_than_raising(tmp_path, bad):
    p = _log(tmp_path, [_row(**bad)])
    assert R.target_closed_rate(R.read(p))["rows_with_a_tab_probe"] == 0
    assert R.probe_failure_rate(R.read(p))["all_probes_errored"] == 0
