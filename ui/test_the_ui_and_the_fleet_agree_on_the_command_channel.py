# -*- coding: utf-8 -*-
"""UI が**実際に書いたもの**を、フリートが**実際に読む**。継ぎ目を1本の実行で渡す。

## なぜこれが要るか

このリポジトリの UI テストは18ファイルあって、**全部 `.cs` のテキストを読むソース断言**。
約21,000行の C# を実行するテストは1本も無かった。断言は「その文字列がファイルにある」
までしか言えないので、**2026-09-22 に実際にこうなっていた**:

  * `ui/test_a_fleet_conversation_can_be_answered.py` が
    `assert 'g["resume_conv"] = c.ConvUrl' in body` で緑
  * `relay/relay_fleet.py` は `resume_conv` を読む
  * 間の `fleet_runner.goals_from_command` が**その欄を運んでいなかった**

両端のテストが緑のまま、機能は当て推量（goal 本文で会話を突き合わせ）で走っていた。
**このテストなら落ちた。** 端ではなく経路を通すから。

## 何を通すか

`ui/FleetCommands.cs` を**本物の csc でコンパイルして実行**し、書かれたファイルを
`relay.fleet_runner.read_commands` と `goals_from_command` に食わせる。
検査するのは C# の文字列ではなく、**チャネルの契約**:

  ファイル名の形（読み手が並べ替えに使う）／`.tmp` を見せないこと／
  BOM 無し UTF-8／JSON の形／**欄が端から端まで残ること**

Linux では skip される。C# のコンパイラが機構そのものなので、ここに代わりの
ソース断言を置くことはしない — それがこのファイルの否定したい形。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

UI = os.path.join(REPO, "ui")
FW = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319"
CSC = os.path.join(FW, "csc.exe")

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not os.path.isfile(CSC),
    reason="the mechanism under test is a compiled C# writer; there is nothing to run "
           "without csc, and asserting on its source text instead is the shape this file "
           "exists to replace")

#: 送り手は本物（ui/FleetCommands.cs）。この harness は Main を足すだけで、
#: 書き込みの実装には一切触らない。
_HARNESS = r"""
using System;
using System.Collections.Generic;

static class Harness
{
    static int Main(string[] argv)
    {
        var item = new Dictionary<string, object>();
        item["text"] = argv[1];
        item["follow_up_to"] = argv[2];
        item["resume_conv"] = argv[3];
        item["priority"] = true;
        var items = new List<object>();
        items.Add(item);
        var patch = new Dictionary<string, object>();
        patch["add_goal"] = items;
        return FleetCommands.Write(argv[0], patch) ? 0 : 1;
    }
}
"""


@pytest.fixture(scope="module")
def sender(tmp_path_factory):
    """The real writer, compiled. Built once; every test sends through it."""
    d = tmp_path_factory.mktemp("sender")
    harness = d / "Harness.cs"
    harness.write_text(_HARNESS, encoding="utf-8")
    exe = str(d / "Send.exe")
    out = subprocess.run(
        [CSC, "/nologo", "/target:exe", "/out:" + exe,
         "/r:" + os.path.join(FW, "System.Web.Extensions.dll"),
         os.path.join(UI, "FleetCommands.cs"), str(harness)],
        capture_output=True, timeout=180)
    assert os.path.isfile(exe), (
        "could not compile the UI's command writer:\n%s\n%s"
        % ((out.stdout or b"").decode("utf-8", "replace"),
           (out.stderr or b"").decode("utf-8", "replace")))
    return exe


def _send(sender, state_dir, text="tidy the docs",
          follow_up_to="the earlier goal", resume_conv="sess:abc-123"):
    r = subprocess.run([sender, str(state_dir), text, follow_up_to, resume_conv],
                       capture_output=True, timeout=60)
    assert r.returncode == 0, "the UI writer reported failure: %r" % (r.stderr,)


def test_the_fleet_reads_what_the_ui_wrote(sender, tmp_path):
    """**経路を1本通す。** C# が書き、Python が読む。"""
    from relay import fleet_runner as FR

    _send(sender, tmp_path)
    cmds = FR.read_commands(str(tmp_path))
    assert len(cmds) == 1, "the fleet saw %d commands, not the one that was sent" % len(cmds)
    assert "add_goal" in cmds[0]


def test_every_field_survives_from_the_window_to_the_worker(sender, tmp_path):
    """**この欄が落ちていたのが今日の欠陥。** 端ではなく経路で検査する。"""
    from relay import fleet_runner as FR

    _send(sender, tmp_path, resume_conv="sess:11111111-2222-3333-4444-555555555555")
    goals = FR.goals_from_command(FR.read_commands(str(tmp_path))[0])
    assert goals, "the command carried no goal by the time the fleet had normalised it"
    g = goals[0]
    assert g["text"] == "tidy the docs"
    assert g["priority"] is True
    assert g["follow_up_to"] == "the earlier goal"
    assert g["resume_conv"] == "sess:11111111-2222-3333-4444-555555555555", \
        "the conversation id the window put on the command did not reach the worker"


def test_nothing_half_written_is_ever_visible(sender, tmp_path):
    """読み手は `.tmp` を飛ばす。書き手がその名前で置いて rename することが前提。"""
    _send(sender, tmp_path)
    d = os.path.join(str(tmp_path), "commands.d")
    names = os.listdir(d)
    assert names and all(n.endswith(".json") for n in names), names


def test_the_name_sorts_in_the_order_the_commands_were_sent(sender, tmp_path):
    """**読み手はファイル名順に処理する。** Windows の時計は約15.6ms刻みなので、
    連続送信は同じナノ秒を読む。順序を保つのは名前の中のカウンタで、桁を固定して
    あるから辞書順が数値順に一致する。ここは実測で一度壊れている
    (0,1,2 を送って 0,2,1 が出た) ので、実際に送って確かめる。"""
    for i in range(6):
        _send(sender, tmp_path, text="goal %d" % i)
    d = os.path.join(str(tmp_path), "commands.d")
    texts = []
    for name in sorted(os.listdir(d)):
        with open(os.path.join(d, name), encoding="utf-8-sig") as fh:
            texts.append(json.load(fh)["add_goal"][0]["text"])
    assert texts == ["goal %d" % i for i in range(6)], texts


def test_the_bytes_are_utf8_without_a_bom(sender, tmp_path):
    """3つあった書き手のうち1つだけ BOM を付けていた。読み手は `utf-8-sig` で
    どちらも飲むが、**他の読み手を待ち構える罠**なので固定する。"""
    _send(sender, tmp_path, text="日本語の goal")
    d = os.path.join(str(tmp_path), "commands.d")
    raw = open(os.path.join(d, os.listdir(d)[0]), "rb").read()
    assert not raw.startswith(b"\xef\xbb\xbf"), "the UI wrote a BOM"
    assert "日本語の goal" in json.loads(raw.decode("utf-8"))["add_goal"][0]["text"]


def test_two_commands_do_not_overwrite_each_other(sender, tmp_path):
    """**これが移行の理由そのもの。** 以前は3つの書き手が1つのファイルを
    read-modify-write していて、後に書いたほうが相手のコマンドを消していた。"""
    _send(sender, tmp_path, text="first")
    _send(sender, tmp_path, text="second")
    from relay import fleet_runner as FR

    got = []
    for c in FR.read_commands(str(tmp_path)):
        got.extend(g["text"] for g in FR.goals_from_command(c))
    assert got == ["first", "second"], got
