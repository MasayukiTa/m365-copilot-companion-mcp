"""touch() に渡した mode / goal / source が保存されること。**旧DBでも**。

## 何が起きていたか

`touch(sid, mode="working", goal=...)` は `sess.update(fields)` でdictに載せるが、
`_write_session` は9カラム決め打ちの INSERT で、`sessions` テーブルに mode / goal / source
という列が無かった。**渡した値は黙って消えていた**。読み戻すと全て None。

黙って消えることの帰結は3つ、いずれも無音:

- `resume_eligibility` は `mode != "interrupted"` で弾くので**必ず失敗** →
  `/goal?resume=1` は到達不能コードだった
- 起動時のクラッシュ検出 `latest.get("mode") == "working"` は**常に False**
- したがって、その先の `touch(mode="interrupted")`(中断した goal の印)にも**到達しない**

テストが緑のまま通っていた理由も分かっている: `resume_eligibility` は**手作りの dict** に
対してしか検証されておらず、**ストアを往復させるテストが1つも無かった**。
だからここでは必ず往復させる。
"""
import os
import sqlite3
import tempfile
import time

import pytest


#: 旧DBに置く sid。**ストアの SID_RE を満たす形でなければならない** —
#: 満たさない sid は load() が弾き、移行が効いていても「行が消えた」ように見える。
#: 実際に最初はそれで落ちた(s + 数字10桁 + 16進4桁)。
OLD_SID = "s0828010203abcd"


@pytest.fixture()
def store(monkeypatch):
    """空のディレクトリを与えた session_store。

    環境変数名は**モジュールが持っている定数から取る**。最初に書いたときは名前を
    "MCP_SESSION_DIR" と決め打ちして間違えており、それでもテストは緑だった —
    conftest が用意した別のディレクトリを黙って使っていたからで、この fixture は
    自分が言っていることをしていなかった。名前を直に書くと、また同じ形で剥がれる。
    """
    import importlib

    import bridge.session_store as S
    monkeypatch.setenv(S.STORE_DIR_ENV, tempfile.mkdtemp())
    importlib.reload(S)
    assert S._base_dir() == os.environ[S.STORE_DIR_ENV], "fixture のディレクトリが効いていない"
    return S


def test_mode_and_goal_survive_a_round_trip(store):
    """書いて、読み戻して、値があること。ここが本題。"""
    sid = store.new_session("t")["sid"]
    store.touch(sid, mode="working", goal="count the widgets", source="chat")

    back = store.load(sid)
    assert back.get("mode") == "working", "mode が保存されていない"
    assert back.get("goal") == "count the widgets", "goal が保存されていない"
    assert back.get("source") == "chat", "source が保存されていない"


def test_marking_a_goal_interrupted_keeps_the_goal_text(store):
    """中断の印を付けても goal は残ること。goal が消えたら resume は再開する物を失う。"""
    sid = store.new_session("t")["sid"]
    store.touch(sid, mode="working", goal="count the widgets")
    store.touch(sid, mode="interrupted")

    back = store.load(sid)
    assert back.get("mode") == "interrupted"
    assert back.get("goal") == "count the widgets", (
        "中断の印を付けたら goal が消えた -- 再開できる物が無くなる")


def test_the_resume_gate_can_now_be_satisfied_from_the_store(store, monkeypatch):
    """`/goal?resume=1` の門が、**ストアから読んだ**セッションで通ること。

    手作りの dict では通っていた。通っていなかったのは往復した後で、
    それを確かめるテストが無かったので欠落が緑のまま残った。
    """
    from bridge.copilot_bridge import resume_eligibility

    sid = store.new_session("t")["sid"]
    store.touch(sid, mode="working", goal="count the widgets")
    store.touch(sid, mode="interrupted")

    ok, why = resume_eligibility(store.load(sid))
    assert ok, "往復したセッションで resume の門が通らない: %s" % why


def test_a_database_made_before_these_columns_is_migrated(monkeypatch):
    """**旧スキーマのDBを実際に作って**、移行が効くことを見る。

    新しい空DBで通っても移行を測ったことにはならない — 母集団が測りたい事象を含まない。
    CREATE TABLE IF NOT EXISTS は既存テーブルを変更しないので、ここを外すと
    「新規環境では動くが、実際に使われているDBでは黙って落ち続ける」形になる。
    """
    d = tempfile.mkdtemp()
    import bridge.session_store as S
    monkeypatch.setenv(S.STORE_DIR_ENV, d)

    # 旧スキーマのDBを**ストアより先に**置く。reload を先にすると、その時点で新スキーマの
    # テーブルが作られてしまい、この試験は「新規DB」を測ることになる — 移行を一度も
    # 通らないまま緑になる形。
    path = os.path.join(d, "sessions.sqlite3")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            sid            TEXT PRIMARY KEY,
            title          TEXT NOT NULL DEFAULT '',
            conv_url       TEXT NOT NULL DEFAULT '',
            created_ts     REAL NOT NULL,
            last_active_ts REAL NOT NULL,
            status         TEXT NOT NULL DEFAULT 'active',
            turns          INTEGER NOT NULL DEFAULT 0,
            transcript     TEXT NOT NULL DEFAULT '',
            pending_json   TEXT NOT NULL DEFAULT '[]'
        );
        """
    )
    now = time.time()
    conn.execute("INSERT INTO sessions (sid, title, conv_url, created_ts, last_active_ts) "
                 "VALUES (?, ?, ?, ?, ?)", (OLD_SID, "old", "", now, now))
    conn.commit()
    conn.close()

    import importlib

    importlib.reload(S)
    assert S._db_path() == path, "試験が見ているDBと、ストアが開くDBが違う"

    # 既存の行は失われず、新しい列は使えること
    back = S.load(OLD_SID)
    assert back is not None, "移行で既存の行が失われた"
    assert back.get("title") == "old", "移行で既存の値が失われた"
    assert back.get("mode") == "", "新しい列が読めない"

    S.touch(OLD_SID, mode="interrupted", goal="from an old database")
    again = S.load(OLD_SID)
    assert again.get("mode") == "interrupted"
    assert again.get("goal") == "from an old database"


def test_the_migration_runs_twice_without_complaint(monkeypatch):
    """2回目の起動で ALTER TABLE を撃ち直して落ちないこと。"""
    d = tempfile.mkdtemp()
    import bridge.session_store as S
    monkeypatch.setenv(S.STORE_DIR_ENV, d)
    import importlib

    import bridge.session_store as S
    importlib.reload(S)
    sid = S.new_session("t")["sid"]
    importlib.reload(S)                    # 同じDBに対してもう一度初期化させる
    assert S.load(sid) is not None
