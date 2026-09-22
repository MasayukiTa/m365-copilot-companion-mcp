# -*- coding: utf-8 -*-
"""落ちたことを「サインインしていない」と読むな。

オーナー報告（2026-09-22、画面2枚）: `quickstart` は
`[ OK ] signed in. Continuing.` と言い、直後に `doctor` が
`[FAIL] M365 signed in on the companion Edge  fix: run quickstart.bat again` と言った。
**同じ問いに、2つの判定器が逆の答えを出した。**

## 機構

`ensure_m365_signin.py --check-only` の終了コードは
`0=サインイン済` / `1=サインイン壁がある` / `2=判定不能`。
ところが **Python の traceback も 1 で終わる**。そして `doctor.ps1` は
「0 でも 2 でもない」を全部 FAIL に倒していた。

つまり**予期しない例外は「やり直せ」という確信ある指示に化ける**。
問題ないサインインをやり直させる方向に倒れるので、無害ではない。

## 直し方

終了コードは人間向けに残し、判定は**明示的な一行**にした。
`VERDICT: signed_in | sign_in_needed | cannot_tell`。
**判定行が無い = そこまで到達しなかった = `cannot_tell`**。落ちたことは判定ではない。
"""
from __future__ import annotations

import io
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from tools import childproc  # noqa: E402

SCRIPT = os.path.join(REPO, "scripts", "ensure_m365_signin.py")
DOCTOR = io.open(os.path.join(REPO, "scripts", "doctor.ps1"),
                 encoding="utf-8", errors="replace").read()


def _run(*args):
    """**Not `text=True`.** This file exists to say that a crash is not a verdict, and a
    locale decode is the way to lose the crash AND the verdict together: `text=True` decodes
    the child with cp932 here, so one non-cp932 byte takes the whole of stdout, leaving the
    reader to guess from an exit code -- the exact reading this file forbids the doctor.
    `tools/test_child_output_is_not_decoded_by_luck.py` caught this on CI."""
    r = childproc.run([sys.executable, SCRIPT] + list(args), timeout=120)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def test_the_checker_always_states_a_verdict():
    """**この箱でどの状態であれ、判定行は出る。** 出ない＝到達しなかった、が読み手の規約。"""
    rc, out = _run("--check-only")
    m = re.search(r"VERDICT:\s*(\w+)", out)
    assert m, "no verdict line; the reader would have to guess from an exit code:\n" + out
    assert m.group(1) in ("signed_in", "sign_in_needed", "cannot_tell")


def test_the_verdict_agrees_with_the_exit_code():
    """2つの経路が食い違えば、それ自体が今日の欠陥の再演。"""
    rc, out = _run("--check-only")
    verdict = re.search(r"VERDICT:\s*(\w+)", out).group(1)
    assert {"signed_in": 0, "sign_in_needed": 1, "cannot_tell": 2}[verdict] == rc


def test_an_unreachable_browser_is_cannot_tell_not_a_wall():
    """**ブラウザが答えないのは「サインインしていない」ではない。**
    そう言うと、できないことをやれと指示することになる — `state()` の docstring が
    最初からそう書いている。"""
    rc, out = _run("--check-only", "--port", "9")     # nothing listens there
    assert rc == 2
    assert "VERDICT: cannot_tell" in out


def test_the_doctor_reads_the_verdict_and_not_the_exit_code():
    """PowerShell は実行しないので、**読んでいる対象**を見る。
    終了コードで分岐に戻ったら、この欠陥も戻る。"""
    assert "VERDICT:" in DOCTOR, "the doctor no longer looks for a verdict at all"
    assert "$signinVerdict -eq \"signed_in\"" in DOCTOR
    assert "$signinCode -eq 0" not in DOCTOR, \
        "the FAIL branch is deciding from the exit code again, which a traceback also sets"


def test_a_missing_verdict_is_reported_as_undetermined():
    """判定行が無いときに FAIL へ倒れないこと。倒れると、落ちたときに
    「やり直せ」と言う元の形に戻る。"""
    i = DOCTOR.index("$signinVerdict = \"cannot_tell\"")
    tail = DOCTOR[i:i + 1200]
    assert "could not be determined" in tail
    assert "NOT the same as" in tail, \
        "the message no longer distinguishes a crash from a real sign-in wall"


def test_the_tab_less_reason_is_not_printed_twice():
    """**報告された見た目の欠陥。** 理由が既に文になっているのに、同じ文で包んでいた:
    `... nothing to judge from (no M365 page open, so nothing to judge from (...))`"""
    src = io.open(SCRIPT, encoding="utf-8").read()
    assert "no M365 page is open yet, so there is nothing to judge from (%s)" not in src
