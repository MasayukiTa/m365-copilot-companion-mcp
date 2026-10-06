# -*- coding: utf-8 -*-
"""プロファイルが別なら、サインインも別。9222 に入れても 9223 には入っていない。

## オーナーの問い（2026-09-23）

> 疑っているのが9222と9223?で建てるのでその片方しか入れていない？とか。妄想かもしれないが

妄想ではない。**構造上そうなる。**Edge は `--user-data-dir` を1プロセスに排他ロックするので、
companion(:9222)・bridge(:9223)・eval(:9224) を同時に動かすには**別プロファイルが必須**。
別プロファイル = 別 Cookie ジャー = **別サインイン**。
`scripts/start_companion_edge.ps1` の `-Profile` の説明が既にそう書いている。

## 何が抜けていたか

サインイン判定は `:9222` だけを見ていた。doctor の
`[ OK ] Bridge Edge running (:9223 history/scrape)` は
**プロセスが CDP に応答する**と言っているだけで、Copilot に届くとは言っていない。
壁の前で座っている bridge は、どの行にも出なかった。

## 掃き忘れの再発を止める

ポート一覧は `relay.edge_recover.MANAGED_EDGE_PROFILES` から取る。**ここにも doctor にも
コピーを置かない。**その定数の docstring は同じ取りこぼしが**4回**起きたと記録している
（rehide / 仮想デスクトップ移動 / タスクバー / :9224 の eval Edge）。
ここで literal を書けば5回目だった。
"""
from __future__ import annotations

import io
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import ensure_m365_signin as m  # noqa: E402
from relay import edge_recover  # noqa: E402

SRC = io.open(os.path.join(REPO, "scripts", "ensure_m365_signin.py"), encoding="utf-8").read()
DOCTOR = io.open(os.path.join(REPO, "scripts", "doctor.ps1"),
                 encoding="utf-8", errors="replace").read()

#: The doctor's own parser, copied here ONLY as the thing under test.
_PROFILE_LINE = re.compile(
    r'^\s*PROFILE:\s+(\d+)\s+(\S+)\s+(\w+)(\s+\[primary\])?\s*\((.*)\)\s*$')


def _report(monkeypatch, answers):
    """Run the all-profiles report with `answers` = {port: (ready, why)}."""
    monkeypatch.setattr(m, "state", lambda port: answers.get(port, (None, "not stubbed")))
    out = []
    monkeypatch.setattr("builtins.print", lambda *a: out.append(" ".join(str(x) for x in a)))
    m._report_every_profile(9222)
    return out


def test_every_managed_profile_gets_its_own_line(monkeypatch):
    """**片方だけ見るのをやめる、が全部。**列挙は定数からで、行は1プロファイルにつき1本。"""
    ports = sorted(edge_recover.MANAGED_EDGE_PROFILES)
    assert len(ports) >= 2, "there is only one managed profile; this test has nothing to say"
    lines = _report(monkeypatch, {p: (None, "stub") for p in ports})
    seen = [int(_PROFILE_LINE.match(l).group(1)) for l in lines if _PROFILE_LINE.match(l)]
    assert seen == ports, (
        "the report does not cover every managed Edge profile: %r vs %r" % (seen, ports))


def test_a_signed_in_companion_does_not_speak_for_the_bridge(monkeypatch):
    """**この問いそのもの。**:9222 が signed_in でも :9223 の壁は壁として出ること。
    ここが緑のまま通ると、オーナーが見た画面に戻る。"""
    answers = {p: (None, "no M365 page open") for p in edge_recover.MANAGED_EDGE_PROFILES}
    answers[9222] = (True, "an M365 page is open")
    answers[9223] = (False, "a sign-in page is open: https://login.microsoftonline.com/x")
    lines = _report(monkeypatch, answers)
    by_port = {int(mm.group(1)): mm for mm in (_PROFILE_LINE.match(l) for l in lines) if mm}
    assert by_port[9222].group(3) == "signed_in"
    assert by_port[9223].group(3) == "sign_in_needed", (
        "the bridge profile's sign-in wall is being reported as the companion's state")


def test_the_primary_is_marked_so_the_doctor_does_not_double_report(monkeypatch):
    """doctor は :9222 の行を別に出している。印が無ければ同じことを2回言う。"""
    lines = _report(monkeypatch, {p: (True, "ok") for p in edge_recover.MANAGED_EDGE_PROFILES})
    marked = [l for l in lines if "[primary]" in l]
    assert len(marked) == 1 and " 9222 " in marked[0], marked


def test_the_port_list_is_not_copied_into_either_reader():
    """**5回目を起こさない。**`MANAGED_EDGE_PROFILES` の docstring が記録している取りこぼしは、
    毎回「プロファイルを列挙する場所にリテラルを書いた」ことで起きている。"""
    assert "MANAGED_EDGE_PROFILES" in SRC, \
        "the checker no longer takes its port list from the one place that has it"
    # AND THE DOCTOR RENDERS WHAT IT IS TOLD. "no port number appears in doctor.ps1" was the
    # first version of this and it was wrong in the way source assertions usually are: it
    # matched :9223 in the Bridge Edge row (a different, legitimate check) and in this very
    # feature's own explanatory comment. What matters is not the spelling; it is that the
    # sign-in rows come from a loop over the checker's PROFILE lines.
    assert "PROFILE:" in DOCTOR and "foreach ($line in ($signinOut" in DOCTOR, \
        "the doctor is no longer building its sign-in rows from the checker's report, so the " \
        "port list has moved back into PowerShell"
    assert "--check-only --all" in DOCTOR, \
        "the doctor asks about one profile again, which is the gap the owner found"


def test_a_port_nobody_answers_is_not_called_a_sign_in_problem(monkeypatch):
    """**None は False ではない。**答えないブラウザは「サインインしていない」ではないし、
    doctor もその行を出さない（出すと、できないことをやれと言うことになる）。"""
    answers = {p: (None, "no Edge is answering on :%d" % p)
               for p in edge_recover.MANAGED_EDGE_PROFILES}
    lines = _report(monkeypatch, answers)
    for l in lines:
        mm = _PROFILE_LINE.match(l)
        if mm:
            assert mm.group(3) == "cannot_tell", l
    assert '$pVerdict -eq "cannot_tell") { continue }' in DOCTOR, \
        "the doctor would now render a row for a browser that is not running"


#: The three entry points a person actually runs. SOURCE ASSERTIONS, and they are worth what
#: source assertions are worth: they protect the WIRING, not the behaviour. PowerShell and cmd
#: do not run on CI's Linux host, so the behaviour was checked by hand on this machine
#: (quickstart's wrapper printed the three-row table; doctor parsed PROFILE lines and skipped
#: the primary and the cannot_tells). What these catch is somebody deleting the call.
_WRAPPER = io.open(os.path.join(REPO, "scripts", "ensure_m365_signin.ps1"),
                   encoding="utf-8", errors="replace").read()
_START_ALL = io.open(os.path.join(REPO, "scripts", "start_all.ps1"),
                     encoding="utf-8", errors="replace").read()
_QUICKSTART = io.open(os.path.join(REPO, "quickstart.bat"),
                      encoding="utf-8", errors="replace").read()


def test_quickstart_reports_every_profile():
    """オーナーの指示（2026-09-23）: **必ず quickstart の中に入れる。**
    quickstart はラッパー経由でサインインを走らせるので、表はそこに出す。"""
    assert "ensure_m365_signin.ps1" in _QUICKSTART, \
        "quickstart no longer runs the sign-in step at all"
    assert "--check-only --all" in _WRAPPER, \
        "the step quickstart runs no longer asks about the other browser profiles"
    assert "Sign-in state per browser profile" in _WRAPPER, \
        "the per-profile table is gathered and never printed"


def test_start_all_reports_every_profile_on_both_paths():
    """**start_all でも検査されるべし。**起動は背景(-NoUi)と手動の2経路あって、
    片方だけ直すのが今日ずっと繰り返している欠陥の形。"""
    assert _START_ALL.count("--check-only --all") == 2, (
        "start_all has two sign-in paths (-NoUi and interactive); both must ask about every "
        "profile, and only %d do" % _START_ALL.count("--check-only --all"))
    assert _START_ALL.count("Report-OtherProfileSignIns") == 3, (
        "expected the reporter to be defined once and called on both paths")


def test_start_all_reports_but_does_not_surface_a_second_browser():
    """**報告であって、勝手に窓を開けることではない。**起動時に2つ目のブラウザを前面に出すのは、
    この直上のコメントが 751 MB 払って学んだことそのもの。"""
    i = _START_ALL.index("function Report-OtherProfileSignIns")
    body = _START_ALL[i:_START_ALL.index("\ntry {", i)]      # the function, not what follows it
    # NOT a search for "-Foreground": that substring lives inside -ForegroundColor, and the
    # first version of this assertion failed on the reporter's own Write-Host. What it means to
    # check is that the reporter never LAUNCHES anything.
    for tok in ("Start-Process", "start_companion_edge", "ensure_m365_signin.ps1", "surface("):
        assert tok not in body, (
            "the startup reporter invokes %r; it is a line of text, not a second sign-in flow, "
            "and opening a browser here is what cost 751 MB and a window nobody asked for" % tok)


def test_the_reason_does_not_call_every_browser_the_companion():
    """:9223 について「companion Edge が応答しない」と言えば、読み手は無関係なプロセスを見に行く。"""
    assert "companion Edge is not answering" not in SRC
    ready, why = m.state(9)          # nothing listens on port 9
    assert ready is None and "9" in why and "companion" not in why, why
