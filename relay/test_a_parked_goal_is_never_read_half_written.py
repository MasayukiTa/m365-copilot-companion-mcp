# -*- coding: utf-8 -*-
"""駐機した goal は、途中まで書かれた状態で読まれてはならない。

`for_fleet/<jid>.txt` は4箇所から**素の `open(..., "w")`** で書かれていた。
読み手 `_deliver_waiting_goals` は「中身が空なら消す」という検査を持っているが、
**それは書き始める前の状態しか捕まえない。** 書き始めて終わっていないファイルを読むと
goal の**先頭だけ**が返り、空ではないので配送される。

**途中で切れた goal は、無い goal より悪い。** オペレータが書いたものに見えるから。

`write_command` が同じ問題を `.tmp` → `os.replace` で解いている。読み手は最初から
`.txt` 以外を飛ばすので、書き込み中の writer は**ただで不可視**になる。
"""
from __future__ import annotations

import os
import sys



import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

LONG_GOAL = ("tidy the documentation and then " * 400).strip()


@pytest.fixture()
def router(tmp_path, monkeypatch):
    import relay.task_router as T

    monkeypatch.setattr(T, "TASKS", str(tmp_path / "tasks"))
    monkeypatch.setattr(T, "FLEET_STATE_DIR", str(tmp_path))
    T.ensure_dirs()
    return T


def _for_fleet_dir(router):
    return os.path.join(router.TASKS, "for_fleet")


def test_the_name_a_reader_watches_appears_only_with_the_whole_goal(router, monkeypatch):
    """**性質を直接検査する。競争で当てにいかない。**

    最初に書いたのはこれの競争版だった: 書きながら別スレッドでディレクトリを舐め、
    先頭だけのファイルを拾おうとする。**素の書き込みに差し替えても一度も捕まらなかった**
    — 45回観測して破れ 0 件。12.8KB がバッファごと一度に落ちるので、窓に入らない。
    **通ることは検出することではない。** 落とせないテストは、無いより悪い。

    だから観測ではなく機構を押さえる。`os.replace` が呼ばれる瞬間に、
    (a) 一時ファイルには**完全な goal** が入っていて、(b) 読み手が見る名前は
    **まだ存在しない**。素の書き込みなら `os.replace` 自体が呼ばれないので落ちる。
    """
    calls = []
    real_replace = os.replace

    def spy(src, dst):
        with open(src, encoding="utf-8") as fh:
            calls.append({"tmp": fh.read(), "dst_existed": os.path.exists(dst), "dst": dst})
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    assert router._write_for_fleet("j1", LONG_GOAL) is True

    assert calls, ("the goal reached its final name without an atomic rename, so a reader "
                   "can see it half written")
    assert calls[0]["tmp"] == LONG_GOAL, "the rename happened before the goal was complete"
    assert not calls[0]["dst_existed"], "something was already at the name readers watch"
    assert calls[0]["dst"].endswith("j1.txt")


def test_the_goal_lands_intact(router):
    assert router._write_for_fleet("j1", LONG_GOAL) is True
    with open(os.path.join(_for_fleet_dir(router), "j1.txt"), encoding="utf-8") as fh:
        assert fh.read() == LONG_GOAL


def test_nothing_is_left_behind_under_a_name_the_reader_takes(router):
    """`.tmp` は読み手が飛ばす拡張子。成功後にそれが残らないこと。"""
    router._write_for_fleet("j2", LONG_GOAL)
    names = os.listdir(_for_fleet_dir(router))
    assert names == ["j2.txt"], names


def test_every_writer_goes_through_it(router):
    """**書き手は4箇所あった。** 1つでも素の書き込みが残ると、そこだけ破れる。
    ソースを読む検査だが、これは「経路が存在するか」の問いで、実行時の値ではない
    — そして直前の3件が実行時の保証を持っている。"""
    import io

    src = io.open(os.path.join(REPO, "relay", "task_router.py"), encoding="utf-8").read()
    assert 'for_fleet", "%s.txt" % jid), "w"' not in src, \
        "a for_fleet writer bypasses _write_for_fleet and can be read half-written"
