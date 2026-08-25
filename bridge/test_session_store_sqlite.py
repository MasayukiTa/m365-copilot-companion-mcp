"""SQLite 版 session_store のうち、ファイル版には無かった性質。

移行で履歴を落とさないこと、境界のある読み出しができること、そして JSONL 互換が
残っていること。最初の2つは「過去の記憶が飛ぶ」を直すための当のもので、
移行で飛ばしたら同じ苦情が原因を替えて再発するだけになる。
"""
import io
import json
import os
import sqlite3

import pytest

from bridge import session_store as ss


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "_base_dir", lambda: str(tmp_path))
    ss._IMPORTED.discard(os.path.abspath(str(tmp_path)))
    yield tmp_path
    ss._IMPORTED.discard(os.path.abspath(str(tmp_path)))


def _legacy(box, sid, title, turns, last_active=1000.0):
    """ファイル版が書いていた形をそのまま置く。"""
    h = ss._sid_filename(sid, "")
    io.open(box / (h + ".json"), "w", encoding="utf-8").write(json.dumps({
        "sid": sid, "title": title, "conv_url": "", "created_ts": 1.0,
        "last_active_ts": last_active, "status": "active", "turns": len(turns),
        "transcript": "sessions/" + h + ".jsonl", "pending": []}))
    with io.open(box / (h + ".jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"meta": True, "sid": sid, "title": title, "ts": 1.0}) + "\n")
        for i, (role, text) in enumerate(turns, 1):
            fh.write(json.dumps({"turn": i, "role": role, "text": text, "ts": 1.0 + i}) + "\n")


def test_existing_file_sessions_are_imported_with_every_turn(box):
    """実データ544セッション・1128ターンで欠落ゼロを確認済み。ここはその性質の固定。"""
    _legacy(box, "s0101010101aaaa", "old one", [("user", "q1"), ("assistant", "a1")])
    _legacy(box, "s0101010102bbbb", "old two", [("user", "q2")])

    got = {s["sid"]: s for s in ss.list_sessions()}
    assert set(got) == {"s0101010101aaaa", "s0101010102bbbb"}
    assert got["s0101010101aaaa"]["title"] == "old one"
    assert [t["text"] for t in ss.all_turns("s0101010101aaaa")] == ["q1", "a1"]
    assert [t["role"] for t in ss.all_turns("s0101010101aaaa")] == ["user", "assistant"]


def test_import_runs_once_and_does_not_duplicate(box):
    """毎回スキャンし直せば、起動のたびにターンが二重になる。"""
    _legacy(box, "s0101010103cccc", "t", [("user", "q")])
    assert len(ss.all_turns("s0101010103cccc")) == 1
    ss._IMPORTED.discard(os.path.abspath(str(box)))     # 別プロセスの起動を模す
    assert len(ss.list_sessions()) == 1
    assert len(ss.all_turns("s0101010103cccc")) == 1, "再取り込みで重複した"


def test_one_corrupt_file_does_not_hide_the_others(box):
    """壊れた1件で取り込み全体を落とせば、その後ろの全セッションが消える。"""
    _legacy(box, "s0101010104dddd", "good", [("user", "q")])
    io.open(box / "deadbeef.json", "w", encoding="utf-8").write("{ not json")
    assert [s["sid"] for s in ss.list_sessions()] == ["s0101010104dddd"]


def test_recent_turns_is_bounded_and_oldest_first(box):
    """リサイクル後に文脈を戻すための境界付き読み出し。
    ファイル版ではこれが『全文を読む』だったので、誰も使わなかった。"""
    sess = ss.new_session(title="x")
    sid = sess["sid"]
    for i in range(50):
        ss.append_turn(sid, "user" if i % 2 == 0 else "assistant", "t%02d" % i)
    last5 = ss.recent_turns(sid, 5)
    assert [t["text"] for t in last5] == ["t45", "t46", "t47", "t48", "t49"]
    assert len(ss.all_turns(sid)) == 50


def test_search_finds_a_turn_without_reading_every_transcript(box):
    sess = ss.new_session(title="x")
    ss.append_turn(sess["sid"], "user", "the needle is here")
    ss.append_turn(sess["sid"], "assistant", "unrelated")
    hits = ss.search_turns(needle="needle")
    assert len(hits) == 1 and hits[0]["sid"] == sess["sid"]


def test_a_percent_sign_in_a_search_is_not_a_wildcard(box):
    """'100%' の検索が全件に一致してはいけない。"""
    sess = ss.new_session(title="x")
    ss.append_turn(sess["sid"], "user", "coverage is 100% now")
    ss.append_turn(sess["sid"], "assistant", "nothing to do with it")
    assert len(ss.search_turns(needle="100%")) == 1
    # "%" を探すのは『% という文字を含む行』を探すこと。100% の行だけが当たる。
    # ワイルドカードとして通っていれば、無関係な行まで2件返る。
    hits = ss.search_turns(needle="%")
    assert len(hits) == 1 and "100%" in hits[0]["text"], hits


def test_the_jsonl_export_is_still_written_for_the_cockpit(box):
    """cockpit は .fleet/conversations.json 経由でこのファイルを読む。
    落とせば、誰も見に行かないところで表示が壊れる。"""
    sess = ss.new_session(title="x")
    ss.append_turn(sess["sid"], "user", "hello")
    path = ss._transcript_path(sess["sid"])
    assert os.path.isfile(path)
    lines = [json.loads(l) for l in io.open(path, encoding="utf-8") if l.strip()]
    assert lines[0].get("meta") is True
    assert lines[1]["role"] == "user" and lines[1]["text"] == "hello"


def test_the_table_is_the_source_of_truth_not_the_file(box):
    """JSONL を消してもターンは残ること。逆なら移行した意味がない。"""
    sess = ss.new_session(title="x")
    ss.append_turn(sess["sid"], "user", "kept")
    os.remove(ss._transcript_path(sess["sid"]))
    assert [t["text"] for t in ss.all_turns(sess["sid"])] == ["kept"]


def test_it_uses_wal_so_a_reader_never_blocks_a_turn(box):
    ss.new_session(title="x")
    conn = sqlite3.connect(ss._db_path())
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()
