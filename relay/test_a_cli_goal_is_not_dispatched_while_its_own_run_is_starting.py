# -*- coding: utf-8 -*-
"""CLI で投入した goal が、**自分の実行と同時にルータからも配送される**のを止める。

## 「見せる」ために「配れる場所」へ置いていた

`fleet_runner._record_cli_submission` は、CLI の goal を分かった瞬間に
`tasks/pending/` へ1件ずつ書く。理由は正しい — 画面に出ない投入は、拒否されたのか
そもそも届かなかったのか区別がつかない。

**だが `tasks/pending/` は表示面ではなく `task_router` の受信箱で、`dispatch_once` は
そこの `.json` を全部 claim する。** 書いてから `_clear_cli_submission` までの間
（goal の読み込み、Edge の起動、開始処理のすべて）ルータは数秒ごとに回っている。

リポジトリに実物が残っている: `cli1789703602_14000_0.delivered.json` —
同じ id の `cli...json` を書いた当の run の隣に、ルータが配送した記録がある。

## 足りなかったのは「まだ自分のもの」と「見捨てられた」の区別

pid で分ける。**見捨てられた側は動き続けなければならない** — 起動に失敗した run の
goal を拾うのは、そもそもこれを記録した目的そのもの。
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


@pytest.fixture()
def router(tmp_path, monkeypatch):
    import relay.task_router as T

    # `TASKS` is the one the directory helpers read (`_p`, `ensure_dirs`); `FLEET_STATE_DIR`
    # is a different constant and patching only that leaves every write pointing at the real
    # .fleet. relay/test_fleet_handoff.py patches TASKS for the same reason.
    monkeypatch.setattr(T, "TASKS", str(tmp_path / "tasks"))
    monkeypatch.setattr(T, "FLEET_STATE_DIR", str(tmp_path))
    T.ensure_dirs()
    return T


def _pending(router, jid, **extra):
    rec = {"id": jid, "type": "fleet_goal", "payload": {"goal": "tidy the docs"},
           "created": time.time()}
    rec.update(extra)
    p = os.path.join(router.TASKS, "pending", "%s.json" % jid)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    return p


def _still_pending(router, jid):
    return os.path.isfile(
        os.path.join(router.TASKS, "pending", "%s.json" % jid))


def test_a_live_owner_keeps_its_entry(router):
    """**欠陥そのもの。** 自分の pid を名乗る生きたプロセスの entry は claim しない。"""
    _pending(router, "cli_live", owner_pid=os.getpid())
    router.dispatch_once()
    assert _still_pending(router, "cli_live"), \
        "the router claimed a goal whose own run is still starting up -- it will be executed twice"


def test_an_abandoned_entry_is_still_picked_up(router, monkeypatch):
    """**こちらが落ちたら記録した意味が消える。** 起動に失敗した run の goal は拾う。"""
    monkeypatch.setattr(router, "_pid_alive", lambda pid: False)
    _pending(router, "cli_dead", owner_pid=999999)
    router.dispatch_once()
    assert not _still_pending(router, "cli_dead"), \
        "an entry whose owner is gone was left stranded, which is what recording it was for"


def test_an_entry_with_no_owner_is_unaffected(router):
    """`fleet_submit` の投入は owner を名乗らない。従来どおり即座に claim される。"""
    _pending(router, "sub_nobody")
    router.dispatch_once()
    assert not _still_pending(router, "sub_nobody")


def test_the_pid_is_not_believed_forever(router, monkeypatch):
    """**pid は durable な識別子ではない。** Windows は再利用する。猶予を過ぎたら、
    pid が何と言おうと見捨てられた扱いにする — でなければ、無関係なプロセスが番号を
    引き継いだだけで entry が永久に残る。"""
    monkeypatch.setattr(router, "_pid_alive", lambda pid: True)
    _pending(router, "cli_ancient", owner_pid=os.getpid(),
             created=time.time() - router.OWNER_PID_GRACE_S - 60)
    router.dispatch_once()
    assert not _still_pending(router, "cli_ancient")


def test_an_unparseable_entry_is_not_stranded_by_this(router):
    """読めない entry を「所有されている」と扱うと、**黙って永久に残る**。
    claim まで落として、rename に決めさせる（parse エラーは done-record になる）。"""
    p = os.path.join(router.TASKS, "pending", "broken.json")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    router.dispatch_once()
    assert not os.path.isfile(p)


def test_the_liveness_query_is_asked_once_per_pass(router, monkeypatch):
    """**この検査そのものが、入れた側の回帰になりかけた。**

    `_pid_alive` は `tasklist` を起動する — この箱で**実測 316ms**。そして CLI の記録は
    **goal 1件につき entry 1件**で、全部が同じプロセスのもの。entry ごとに聞くと、
    10件の run で**1パス 3.2秒**かかる。`--poll-s` の既定は 2.0 なので、ルータは
    tasklist の中で一生を過ごすことになっていた。

    1パス1問い合わせに畳む。キャッシュはパスと一緒に捨てられる辞書なので、
    古くなりようがない。
    """
    calls = []
    monkeypatch.setattr(router, "_pid_alive", lambda pid: calls.append(pid) or True)
    for i in range(8):
        _pending(router, "cli_%d" % i, owner_pid=os.getpid())
    router.dispatch_once()
    assert len(calls) == 1, "asked the OS %d times for one process" % len(calls)
    assert all(_still_pending(router, "cli_%d" % i) for i in range(8))


def test_the_writer_and_the_reader_agree_on_the_field_name(tmp_path):
    """**書き手と読み手が別モジュールにいる。** 片方だけ名前を変えても、上の検査は
    自分で書いた entry しか見ていないので通ってしまう。実際に書かせて確かめる。"""
    from relay import fleet_runner as FR
    import relay.task_router as T

    paths = FR._record_cli_submission(str(tmp_path), ["tidy the docs"], ["fleet_runner.py"])
    assert paths, "the CLI submission recorded nothing"
    with open(paths[0], encoding="utf-8") as fh:
        rec = json.load(fh)
    assert rec.get("owner_pid") == os.getpid()
    assert T._still_owned(paths[0]) is True
