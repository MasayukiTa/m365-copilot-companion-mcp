# -*- coding: utf-8 -*-
"""「判定器が拒否した」と「判定器が居なかった」を、同じ行の形のまま数えていた。

`tools/code_exec.py::_record_judgement` は 2026-08-31 から、判定したコマンドごとに1行
書いている。2026-09-20 時点で **11,732行 / 11.4MB / 相異なるコマンド10,371**。
読むのは `tools/ledger_health.py` だけで、それも「**欄**が埋まっているか」を見ており、
**行が何を言っているかは誰も見ていない**。

## 生の件数は事実を隠す

    would_block（shadow）        11,263 / 11,263   (100%)
    decision = REQUIRE_HUMAN     11,265 / 11,732   ( 96%)

これだけ読めば「何でも拒否する審査層」。**違う。** 理由で割ると:

    「judge に到達できなかった」      10,361  (88.3%)
    「judge が設定されていない」         904
    コマンドについての実際の評決          350

**10,361行は、層が自分の不在を記録している。** そして fail-closed している — それは正しく、
`code_exec` のコメントが要求しているとおり（「FAILURE IS NOT PERMISSION, INCLUDING MY OWN
FAILURE」）。**間違っていたのは、誰にも見えなかったことだけ。** 転送エラーと熟慮された拒否は
同じ行の形をしていて、合算すると「判定器が厳しい」が「判定器が居ない」の顔をする。

実測で分けた結果: **判定器が実際に答えたのは 467件（4.0%）**。そのうち BLOCK 350 / ALLOW 117
— 答えるときはどちらにも倒れていない。そして **`REQUIRE_HUMAN` は「答えた」側から完全に消える**。
あれは評決ではなく、fail-closed の既定値だった。
"""
from __future__ import annotations

import json

from tools import judge_report as R


def _log(tmp_path, rows):
    p = tmp_path / "judge.jsonl"
    with open(str(p), "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return str(p)


def _verdict(decision="BLOCK_AND_RETRY", reason="no", **kw):
    base = {"ts": 1.0, "mode": "shadow", "decision": decision, "reason": reason,
            "would_block": decision != "ALLOW", "cmd_sha16": "a" * 16,
            "human_approved": None}
    base.update(kw)
    return base


def _unreachable(**kw):
    return _verdict(decision="REQUIRE_HUMAN",
                    reason="the judge could not be reached (JudgeTransportError: the "
                           "connection was closed)", **kw)


# ───────────────────────── 混ぜないこと

def test_an_unreachable_judge_is_not_an_assessment(tmp_path):
    """**この述語がモジュールの全部。** `decision` がどれだけ断定的に見えても、
    到達できなかった行はコマンドについての判断ではない。"""
    rows = R.read(_log(tmp_path, [_verdict(), _unreachable()]))
    assert len(R.assessed(rows)) == 1
    assert len(R.unavailable(rows)) == 1


def test_an_unconfigured_judge_is_also_not_an_assessment(tmp_path):
    rows = R.read(_log(tmp_path, [_verdict(
        decision="REQUIRE_HUMAN",
        reason="no judge is configured, so this command has not been assessed")]))
    assert R.assessed(rows) == []


def test_the_marker_matches_as_a_substring_not_as_the_whole_reason(tmp_path):
    """**転送エラーは末尾に例外自身の文言を連れてくる。** 完全一致で見ていたら、
    失敗の種類ごとに別物として数え、しかもそれぞれを評決として数えることになる。"""
    rows = R.read(_log(tmp_path, [
        _verdict(decision="REQUIRE_HUMAN",
                 reason="the judge could not be reached (JudgeTransportError: no MCP "
                        "request context: nothing to ask)"),
        _unreachable()]))
    assert R.assessed(rows) == []
    assert len(R.unavailable(rows)) == 2


def test_the_report_never_totals_the_two(tmp_path):
    """合算した率は「層は何回 no と言ったか」に答える — それは2つの別物が1つの名前を
    着ている問い。"""
    out = R.report(_log(tmp_path, [_verdict(), _unreachable(), _unreachable()]))
    assert "a judge answered: 1" in out
    assert "no judge could be asked: 2" in out
    assert "not verdicts about the commands" in out


def test_a_real_verdict_keeps_its_decision(tmp_path):
    rows = R.read(_log(tmp_path, [_verdict(decision="ALLOW", reason="ok", would_block=False),
                                  _verdict(decision="BLOCK_AND_RETRY")]))
    assert R.by_decision(R.assessed(rows)) == {"ALLOW": 1, "BLOCK_AND_RETRY": 1}


# ───────────────────────── enforce を入れられるか、という立っている問い

def test_only_enforce_rows_are_asked_whether_a_person_released_them(tmp_path):
    """**shadow では `human_approved` は構造上つねに None。** `_ask_operator` は enforce で
    しか呼ばれないので、全件の None を数えても何も言っていない。問いが実際に出された
    ところだけを見る。"""
    rows = R.read(_log(tmp_path, [
        _verdict(mode="shadow"),                                   # 人には訊いていない
        _verdict(mode="enforce", would_block=True),                # 訊いて、返事なし
        _verdict(mode="enforce", would_block=True, human_approved=True),   # 人が解放
        _verdict(mode="enforce", decision="ALLOW", would_block=False)]))
    got = R.enforce_blocks(rows)
    assert got["enforce_rows"] == 3
    assert got["blocked"] == 2
    assert got["released_by_a_person"] == 1


def test_a_block_with_no_assessment_behind_it_is_counted_separately(tmp_path):
    """**enforce を広げてよいかを決める数字。** 判断の結果ではなく転送の失敗で拒否された
    コマンドは、コマンドについて何も言っていない。"""
    rows = R.read(_log(tmp_path, [
        _verdict(mode="enforce", would_block=True),                       # 本物の評決
        _unreachable(mode="enforce", would_block=True)]))                 # 不在
    got = R.enforce_blocks(rows)
    assert got["blocked"] == 2 and got["blocked_without_an_assessment"] == 1


# ───────────────────────── 壊れた入力

def test_a_row_with_no_reason_counts_as_assessed(tmp_path):
    """理由が無い行を「不在」に寄せたら、記録の欠けが層の不在として報告される。
    どちらに倒すかは選ぶしかなく、**不在の主張のほうを厳しく**した。"""
    rows = R.read(_log(tmp_path, [{"decision": "ALLOW", "mode": "shadow"}]))
    assert len(R.assessed(rows)) == 1


def test_a_half_written_last_line_does_not_stop_the_report(tmp_path):
    p = tmp_path / "judge.jsonl"
    with open(str(p), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(_verdict()) + "\n")
        fh.write('{"ts": 2.0, "mod')
    assert "11732" not in R.report(str(p))
    assert "1 records" in R.report(str(p))


def test_an_empty_or_missing_log_says_nothing_rather_than_healthy(tmp_path):
    assert "no records" in R.report(_log(tmp_path, []))
    assert "no records" in R.report(str(tmp_path / "never_written.jsonl"))
