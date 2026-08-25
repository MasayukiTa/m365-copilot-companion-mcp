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


# ── どこまで膨らみ、どう戻すか ────────────────────────────────────────────────

def test_nothing_is_deleted_unless_a_limit_is_asked_for(box):
    """この保存層は『履歴が消える』を直すために在る。
    既定で消し始めれば、同じ損失が予定表に載るだけになる。"""
    sess = ss.new_session(title="keep me")
    ss.append_turn(sess["sid"], "user", "hello")
    out = ss.prune()
    assert out["removed_sessions"] == 0
    assert len(ss.all_turns(sess["sid"])) == 1


def test_age_limit_removes_whole_old_sessions_only(box):
    """半分だけ残った会話は、無いより悪い -- 完全な顔をして中身が抜けている。
    リサイクル後に文脈を戻す処理が、middle を欠いた版を黙って食わせることになる。"""
    old = ss.new_session(title="old")
    ss.append_turn(old["sid"], "user", "ancient")
    ss.touch(old["sid"], last_active_ts=1000.0)          # はるか昔
    fresh = ss.new_session(title="fresh")
    ss.append_turn(fresh["sid"], "user", "recent")

    out = ss.prune(max_age_days=30)
    assert out["removed_sessions"] == 1 and out["sids"] == [old["sid"]]
    assert ss.load(old["sid"]) is None
    assert ss.all_turns(old["sid"]) == []
    assert len(ss.all_turns(fresh["sid"])) == 1, "新しいほうまで消している"


def test_the_transcript_export_goes_with_the_rows(box):
    """行を消して JSONL を残せば、cockpit が『もう無い会話』を表示し続ける。"""
    sess = ss.new_session(title="x")
    ss.append_turn(sess["sid"], "user", "bye")
    path = ss._transcript_path(sess["sid"])
    assert os.path.isfile(path)
    ss.touch(sess["sid"], last_active_ts=1000.0)
    ss.prune(max_age_days=1)
    assert not os.path.isfile(path)


def test_a_size_limit_removes_oldest_first_and_reports_if_still_over(box):
    """サイズは『ファイルの性質』で、削除しても即座には縮まない。
    1件消すごとに測り直す実装は必ず消しすぎるか、消さなすぎる。"""
    sids = []
    for i in range(5):
        s = ss.new_session(title="s%d" % i)
        ss.append_turn(s["sid"], "user", "x" * 2000)
        ss.touch(s["sid"], last_active_ts=1000.0 + i)
        sids.append(s["sid"])

    out = ss.prune(max_mb=0.0001)          # 事実上ゼロ -- 全部が対象になる
    assert out["removed_sessions"] >= 1
    # 消えたのは古い側から
    survivors = {s["sid"] for s in ss.list_sessions()}
    assert sids[0] not in survivors, "最も古いものが残っている"
    assert "still_over" in out


def test_stats_report_the_file_not_just_the_rows(box):
    """WAL を数えなければ、実際にディスクを食っている量を報告できない。"""
    sess = ss.new_session(title="x")
    ss.append_turn(sess["sid"], "user", "y" * 500)
    st = ss.store_stats()
    assert st["sessions"] == 1 and st["turns"] == 1
    assert st["bytes"] > 0 and st["mb"] >= 0
    assert st["text_bytes"] >= 500
    assert st["oldest_age_days"] is not None


def test_the_database_can_hand_space_back(box):
    """削除しても縮まないのが SQLite の既定。作成時に incremental を立てていなければ、
    剪定しても運用者のディスクは1バイトも空かない。"""
    conn = sqlite3.connect(ss._db_path()) if os.path.exists(ss._db_path()) else None
    if conn is None:
        ss.new_session(title="x")
        conn = sqlite3.connect(ss._db_path())
    try:
        mode = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
    finally:
        conn.close()
    assert mode == 2, "auto_vacuum が incremental(2) でない -- 剪定しても縮まない"


def test_compact_runs_and_leaves_the_data_intact(box):
    sess = ss.new_session(title="x")
    ss.append_turn(sess["sid"], "user", "still here")
    ss.compact()
    assert [t["text"] for t in ss.all_turns(sess["sid"])] == ["still here"]


def test_pruning_actually_returns_the_disk(box):
    """`PRAGMA incremental_vacuum` は結果を消費しないと1ページで止まる。

    Python の sqlite3 はカーソルを返して遅延実行するので、execute だけでは
    291 の空きページのうち 4KB 一枚しか回収されなかった。539セッションを消して
    1.18MB が 1.17MB にしかならず、機構ごと壊れているように見えた。
    消費すれば同じ削除で 36KB まで落ちる。"""
    for i in range(40):
        s = ss.new_session(title="s%d" % i)
        ss.append_turn(s["sid"], "user", "x" * 4000)
        ss.touch(s["sid"], last_active_ts=1000.0 + i)
    before = ss.store_stats()["bytes"]
    assert before > 100_000, "前提が崩れている(データが小さすぎる)"

    ss.prune(max_age_days=1)               # 全部が古い
    after = ss.store_stats()["bytes"]
    assert ss.store_stats()["sessions"] == 0
    assert after < before * 0.5, (
        "剪定してもディスクが戻っていない: %d -> %d (PRAGMA の結果を消費しているか)"
        % (before, after))


def test_stats_are_not_zero_on_the_very_first_call(box):
    """新しい端末での初回。DB を作る前にサイズを測れば 0.00 MB と報告してしまい、
    上限による剪定が永久に発火しない。"""
    st = ss.store_stats()
    assert st["bytes"] > 0, "初回にファイルサイズを 0 と報告している"


# ── fleet の会話も同じデータベースに入るか ──────────────────────────────────

def test_a_fleet_transcript_line_lands_in_the_table(box):
    """fleet はこの保存層を一度も使っていなかった。session_store は bridge のもので、
    fleet の会話は state ディレクトリの JSONL にしか存在しなかった -- つまり
    「チャットはローカルの SQL にある」は、会話を持つ2つのうち忙しい方には嘘だった。"""
    ok = ss.record_fleet_turn("run1_w0", {"turn": 1, "role": "user", "text": "do it",
                                          "ts": 100.0}, name="w0", goal="a goal")
    assert ok
    rows = ss.fleet_turns("run1_w0")
    assert len(rows) == 1
    assert rows[0]["role"] == "user" and rows[0]["text"] == "do it"
    assert rows[0]["name"] == "w0" and rows[0]["goal"] == "a goal"


def test_meta_and_metric_lines_are_kept_and_labelled(box):
    """role の無い行(meta / guid)を捨てると、『始まったが何も産まなかった会話』と
    『そもそも始まらなかった会話』が区別できなくなる。metric は数値なので extra に。"""
    ss.record_fleet_turn("run1_w1", {"meta": True, "key": "run1_w1", "ts": 1.0})
    ss.record_fleet_turn("run1_w1", {"guid": "abc", "ts": 2.0})
    ss.record_fleet_turn("run1_w1", {"turn": 1, "role": "metric", "name": "rss_mb",
                                     "value": 12.5, "ts": 3.0})
    roles = [r["role"] for r in ss.fleet_turns("run1_w1")]
    assert set(roles) == {"meta", "guid", "metric"}
    metric = [r for r in ss.fleet_turns("run1_w1") if r["role"] == "metric"][0]
    assert metric["extra"]["name"] == "rss_mb" and metric["extra"]["value"] == 12.5


def test_recording_a_fleet_turn_never_raises(box, monkeypatch):
    """fleet はログの不調で止まってはいけない。DB がロックされていても同じ。"""
    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(ss, "_db", boom)
    assert ss.record_fleet_turn("k", {"role": "user", "text": "x"}) is False


def test_the_fleet_path_does_not_pay_for_the_bridge_migration(box):
    """取り込みは実データで11.8秒かかる。fleet のターン1本がそれを払ってはいけない。"""
    import inspect
    src = inspect.getsource(ss.record_fleet_turn)
    assert "_db(import_files=False)" in src, "fleet の書き込みが移行スキャンを走らせている"


def test_an_interrupted_write_does_not_let_the_next_turn_overwrite_history(box):
    """ターン番号をセッション行のカウンタから作ると、中断のあと履歴を上書きする。

    カウンタはターン行とは別の文で、あとから書かれる。その2文の間でプロセスが死ねば
    -- watchdog のリセット、スリープ、窓を閉じる、fleet では日常的に起きる --
    カウンタはテーブルより1つ遅れる。次の追記が同じ番号を再利用し、
    INSERT OR REPLACE が本物のターンを黙って潰す。

    履歴が失われるのを直すための保存層が、中断1回につき1ターンずつ消していた。
    kill を挟む stress で、1回目は通り2回目で落ちて見つかった -- 噛むかどうかは
    kill がどこに落ちるかで決まる。ここでは同じ状態を決定的に作る。"""
    sess = ss.new_session(title="x")
    sid = sess["sid"]
    ss.append_turn(sid, "user", "first")
    ss.append_turn(sid, "assistant", "second")
    assert len(ss.all_turns(sid)) == 2

    # 中断でカウンタだけが取り残された状態
    conn = sqlite3.connect(ss._db_path())
    try:
        conn.execute("UPDATE sessions SET turns = 1 WHERE sid = ?", (sid,))
        conn.commit()
    finally:
        conn.close()

    ss.append_turn(sid, "user", "third")
    texts = [t["text"] for t in ss.all_turns(sid)]
    assert texts == ["first", "second", "third"], (
        "中断後の追記が既存ターンを上書きした: %s" % texts)


def test_the_store_location_can_be_moved_by_environment(monkeypatch, tmp_path):
    """環境変数で差し替えられること。属性の monkeypatch では足りない。

    書き手はこのプロセスだけではない -- fleet はワーカーを、stress はプロセスを生む。
    それぞれが自分の import を持つので、差し替えた属性は境界を越えない。越えなかった
    結果、試験1回で本番の保存層に 138 行が入り、`wdead` や `wconsent` といったキーが
    本物の会話の隣に並んだ。conftest はこの変数でスイート全体を逃がしている。"""
    monkeypatch.setenv(ss.STORE_DIR_ENV, str(tmp_path))
    assert ss._base_dir() == str(tmp_path)
    monkeypatch.delenv(ss.STORE_DIR_ENV)
    assert ss._base_dir() == ss.SESS_DIR, "変数が無いときに既定へ戻っていない"


def test_the_suite_is_not_writing_into_the_operators_store():
    """conftest の隔離が外れたら、次の全体試験が本番の会話に混ざる。

    fixture 無しで直接見る -- この試験が守りたいのは『既定の状態で走ったとき』だから。"""
    assert os.environ.get(ss.STORE_DIR_ENV), (
        "session store が隔離されていない -- 試験が運用者の保存層へ書く")
    assert ss._base_dir() != ss.SESS_DIR


# ── 保持設定 ────────────────────────────────────────────────────────────────

def test_a_settings_file_without_the_keys_deletes_nothing(box, monkeypatch, tmp_path):
    """これらの鍵が生まれる前に書かれた settings.txt には、当然どちらも無い。
    欠落を『0日』と読めば、新規インストールの初回起動で全履歴が消える。
    履歴消失を止めるための機能が、履歴を消す側になってはいけない。"""
    monkeypatch.setattr(ss, "_settings_path", lambda: str(tmp_path / "settings.txt"))
    (tmp_path / "settings.txt").write_text("maxtabs=3\nzoom=1.0\n", encoding="utf-8")
    assert ss.read_retention() == (None, None)

    sess = ss.new_session(title="x")
    ss.append_turn(sess["sid"], "user", "keep")
    ss.touch(sess["sid"], last_active_ts=1000.0)
    assert ss.apply_retention() is None
    assert len(ss.all_turns(sess["sid"])) == 1


def test_zero_means_off_not_immediately(box, monkeypatch, tmp_path):
    """0 は『0日保持』ではなく『無効』。ここを取り違えると一撃で全部消える。"""
    monkeypatch.setattr(ss, "_settings_path", lambda: str(tmp_path / "settings.txt"))
    (tmp_path / "settings.txt").write_text(
        "session_retention_days=0\nsession_max_mb=0\n", encoding="utf-8")
    assert ss.read_retention() == (None, None)


def test_a_configured_age_is_applied(box, monkeypatch, tmp_path):
    monkeypatch.setattr(ss, "_settings_path", lambda: str(tmp_path / "settings.txt"))
    (tmp_path / "settings.txt").write_text("session_retention_days=30\n", encoding="utf-8")
    assert ss.read_retention() == (30.0, None)

    old = ss.new_session(title="old")
    ss.append_turn(old["sid"], "user", "ancient")
    ss.touch(old["sid"], last_active_ts=1000.0)
    fresh = ss.new_session(title="fresh")
    ss.append_turn(fresh["sid"], "user", "recent")

    out = ss.apply_retention()
    assert out and out["removed_sessions"] == 1
    assert ss.load(old["sid"]) is None and ss.load(fresh["sid"]) is not None


def test_a_garbled_value_is_ignored_rather_than_guessed(box, monkeypatch, tmp_path):
    """人が編集するファイル。数字でない行を 0 や 1 と読むより、無視するほうが安全。"""
    monkeypatch.setattr(ss, "_settings_path", lambda: str(tmp_path / "settings.txt"))
    (tmp_path / "settings.txt").write_text(
        "session_retention_days=thirty\nsession_max_mb=\n", encoding="utf-8")
    assert ss.read_retention() == (None, None)


def test_a_missing_settings_file_is_not_an_error(box, monkeypatch, tmp_path):
    monkeypatch.setattr(ss, "_settings_path", lambda: str(tmp_path / "nope.txt"))
    assert ss.read_retention() == (None, None)
    assert ss.apply_retention() is None


def test_the_bridge_prunes_at_startup_not_on_a_timer():
    """走行中に発火する剪定は、いま書いている会話を消しうる。
    起動時に1回だけにする。今消すのと1時間後に消すのの差は、その危険に見合わない。"""
    import pathlib as _pl
    src = (_pl.Path(__file__).resolve().parents[1] / "bridge" / "copilot_bridge.py"
           ).read_text(encoding="utf-8", errors="replace")
    assert "S.apply_retention()" in src, "起動時に保持を適用していない"
    i = src.index("S.apply_retention()")
    # 実際の呼び出しを見る。最初の出現はコメント("mirrors the old srv.serve_forever()")で、
    # そちらを取ると本物より手前に見えて判定が逆になる。
    j = src.rindex("    srv.serve_forever()")
    assert i < j, "サーバ開始後に剪定している"
    # 失敗しても起動を止めないこと
    assert "leaving the store alone" in src
