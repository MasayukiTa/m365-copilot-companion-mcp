# -*- coding: utf-8 -*-
"""UI が書く台帳の1行目に、BOM を置かない。

`.fleet/autofix.jsonl` 521行のうち、**`json.loads` が拒む行が1つ**あった。
破れ書き込みではない — **1行目で、原因は BOM**:

    Unexpected UTF-8 BOM (decode using utf-8-sig): line 1 column 1 (char 0)

.NET の `Encoding.UTF8` は**前置符号を出す**。`File.AppendAllText` はファイルを作るときに
それを書くので、**このコックピットが始めた台帳は例外なく1行目に3バイト余計に持つ**。
行単位の読み手は誰もそれを予期しない。実測で `.fleet/ui_errors.jsonl` も同じだった。

このリポジトリは規則を持っている — **書くときは BOM 無し、読むときは `utf-8-sig`**。
書く側の半分が、C# にだけ無かった。

## なぜ綴りを検査してよいか

今日3回、ソース断言が「欠陥ではなく修正のほうを落とす」のを見た。ここが違うのは、
**綴りそのものが性質だから**: 問題は「`Encoding.UTF8` という API を書き込みに使うこと」で、
言い換えの余地が無い。アンカーは呼び出しの形に置き、本文の広い切り出しはしない。
"""
from __future__ import annotations

import io
import json
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

UI = os.path.join(REPO, "ui")

#: `File.WriteAllText(x, y, Encoding.UTF8)` / `AppendAllText(...)` -- the BOM-emitting form.
#: `new UTF8Encoding(false)` and a shared constant both pass.
_BOM_WRITE = re.compile(r"(?:Write|Append)AllText\([^;]*?,\s*Encoding\.UTF8\s*\)")


def _cs_files():
    return [os.path.join(UI, n) for n in sorted(os.listdir(UI)) if n.endswith(".cs")]


def test_no_ui_writer_emits_a_byte_order_mark():
    """**欠陥そのもの。** 3箇所あった: autofix.jsonl / ui_errors.jsonl / 会話 transcript。"""
    hits = []
    for path in _cs_files():
        src = io.open(path, encoding="utf-8", errors="replace").read()
        for m in _BOM_WRITE.finditer(src):
            line = src[:m.start()].count(chr(10)) + 1
            hits.append("%s:%d" % (os.path.basename(path), line))
    assert not hits, (
        "these write UTF-8 WITH a preamble, so the first line of any file they create "
        "carries a BOM no line reader expects: " + ", ".join(hits))


def test_a_reader_still_copes_with_the_files_already_on_disk(tmp_path):
    """**過去に書かれた行は残る。** 書き手を直しても、既存の1行目は BOM のまま。
    読み手は `utf-8-sig` で開かなければならない — これは実行して確かめる。"""
    p = tmp_path / "autofix.jsonl"
    with io.open(p, "w", encoding="utf-8-sig", newline=chr(10)) as fh:
        fh.write(json.dumps({"ts": 1, "dot": "edge"}) + chr(10))
        fh.write(json.dumps({"ts": 2, "dot": "tunnel"}) + chr(10))

    with io.open(p, encoding="utf-8") as fh:
        with pytest.raises(ValueError):
            json.loads(fh.readline())          # the defect, reproduced

    with io.open(p, encoding="utf-8-sig") as fh:
        rows = [json.loads(l) for l in fh if l.strip()]
    assert [r["dot"] for r in rows] == ["edge", "tunnel"]


@pytest.mark.skipif(not os.path.isdir(os.path.join(REPO, ".fleet")),
                    reason="no live .fleet on this machine")
def test_the_live_ledgers_are_readable_one_way_or_another():
    """**この検査が守っているものを、実物で確かめる。** BOM 付きの古い行があっても、
    utf-8-sig なら全行読めること。読めない行が出たら、それは BOM ではない別の欠陥で、
    そのときに気づきたい（破れ書き込みなら、そう言えるようになる）。"""
    for name in ("autofix.jsonl", "ui_errors.jsonl"):
        p = os.path.join(REPO, ".fleet", name)
        if not os.path.isfile(p):
            continue
        bad = []
        for i, line in enumerate(io.open(p, encoding="utf-8-sig"), 1):
            line = line.strip()
            if not line:
                continue
            try:
                json.loads(line)
            except ValueError as exc:
                bad.append("%s:%d %s" % (name, i, exc))
        assert not bad, bad
