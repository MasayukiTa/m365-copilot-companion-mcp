# -*- coding: utf-8 -*-
"""`headless_creationflags()` が本当に窓を消すことを、**実行して**確かめる。

**ソース断言ではこれを捕まえられない。** 「`creationflags` を渡している」ことと
「窓が出ない」ことは別の主張で、このリポジトリはその区別を何度も払っている。
だから子プロセスを実際に起こして `GetConsoleWindow` / `IsWindowVisible` を読む。

## 親にコンソールがあると、この欠陥は見えない

最初の計測は差が出なかった (2026-09-22): フラグ無しでも `0,0`。親（Bash ツールから
起動した python）が**自分のコンソールを持っていて子がそれを継承**していたので、
そもそも新規割り当てが起きなかった。**条件が違えば欠陥は隠れる。**

`pythonw.exe` を親にして測り直すと再現した。これが本番の地形 —
`scripts/win/register_selfimprove_nightly.ps1` は `wscript.exe` でタスクを起こし、
`wscript` はコンソールを持たない:

    parent_has_console = 0
    フラグ無し          -> has_console=1, visible=1   <- 無人のデスクトップに出る窓
    CREATE_NO_WINDOW    -> has_console=0, visible=0

だからこのテストは **`pythonw.exe` を親にする**。ここを `python.exe` に戻すと、
テストは通り続けたまま何も検査しなくなる。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="console allocation is a Windows mechanism; there is nothing here to assert "
           "elsewhere, and asserting the flag value instead would be the source assertion "
           "this file exists to avoid")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 子が自分のコンソールと、その窓の可視状態を報告する。ハンドルが無ければ窓も無い。
_PROBE = ("import ctypes,sys;h=ctypes.windll.kernel32.GetConsoleWindow();"
          "v=ctypes.windll.user32.IsWindowVisible(h) if h else 0;"
          "sys.stdout.write(str(int(h!=0))+','+str(int(v)))")

#: 親は**コンソールを持たない**プロセスでなければならない (上の docstring)。
_PARENT = r"""
import ctypes, json, os, subprocess, sys
probe = sys.argv[1]
out = {"parent_has_console": int(ctypes.windll.kernel32.GetConsoleWindow() != 0)}
py = sys.executable.replace("pythonw.exe", "python.exe")
for name, flags in (("noflags", 0), ("headless", int(sys.argv[3]))):
    r = subprocess.run([py, "-c", probe], capture_output=True, text=True,
                       creationflags=flags)
    out[name] = (r.stdout or "").strip()
open(sys.argv[2], "w", encoding="utf-8").write(json.dumps(out))
"""


def _pythonw():
    cand = os.path.join(REPO, ".venv", "Scripts", "pythonw.exe")
    if os.path.isfile(cand):
        return cand
    cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if os.path.isfile(cand):
        return cand
    pytest.skip("no pythonw.exe to build a console-less parent with")


def _measure(tmp_path):
    from tools import childproc

    parent_py = tmp_path / "parent.py"
    parent_py.write_text(_PARENT, encoding="utf-8")
    result = tmp_path / "result.json"
    proc = subprocess.run(
        [_pythonw(), str(parent_py), _PROBE, str(result),
         str(childproc.headless_creationflags())],
        capture_output=True, timeout=120)
    assert result.is_file(), (
        "the console-less parent never wrote its result: rc=%s stderr=%r"
        % (proc.returncode, (proc.stderr or b"")[:400]))
    return json.loads(result.read_text(encoding="utf-8"))


def test_the_parent_really_has_no_console(tmp_path):
    """前提そのものの検査。ここが 1 なら、以下の2件は何も証明していない。"""
    assert _measure(tmp_path)["parent_has_console"] == 0


def test_without_the_flag_a_visible_window_appears(tmp_path):
    """**欠陥の側を先に見せる。** これが落ちるようになったら、Windows か venv の形が
    変わったということで、そのときは下のテストも意味を失っている。"""
    assert _measure(tmp_path)["noflags"] == "1,1", \
        "the defect no longer reproduces, so the guard below is no longer guarding anything"


def test_with_the_flag_there_is_no_console_at_all(tmp_path):
    assert _measure(tmp_path)["headless"] == "0,0"


def test_the_flag_does_not_drag_a_process_group_in_with_it():
    """`CREATE_NEW_PROCESS_GROUP` は既定に入れない。Ctrl+C 処理を無効にした群を作る一方、
    `GenerateConsoleCtrlEvent` は送り手と同じコンソールを要求するので、**窓の無い子には
    停止経路として届かない**。そして実測 (2026-09-22): `CTRL_BREAK_EVENT` /
    `CTRL_C_EVENT` / `send_signal` / `GenerateConsoleCtrlEvent` は tracked ファイルに
    **0件**。買うものが無く、信号経路だけ塞ぐフラグは既定にしない。"""
    from tools import childproc

    assert not (childproc.headless_creationflags()
                & getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
