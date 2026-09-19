# -*- coding: utf-8 -*-
"""拒否の理由を台帳自身が言えるようにする。「分けられない」を分けられるようにした記録。

**なぜこれが要ったか。** 2026-09-19 に `.fleet/lock_refusals.jsonl` の543件（`session` を
持つ行）を実測して、docstring が名指ししていた仮説を2つとも潰した:

    client_ip が変わったセッション            0 / 447
    承認済み・TTL内なのに拒否された            0 / 543
    拒否の後に承認された                       67（設計どおりの初回拒否）
    承認の記録が無い                          476

**残った476は、拒否側のデータだけでは分割できない。** `state[ip]["sessions"]` は上限512で
最新の touch しか持たないので、「一度も承認されていない」と「承認された後に追い出された／
期限切れ」が同じ痕跡（＝無し）になる。台帳をもっと読んでも出ない — 計器が足りない。

だから解錠側が追記専用の記録を持つ。**新しいファイルではなく同じ台帳に** `event="granted"`
を足した: `_append_log` は既に `event` を刻んでいるので、結合キーも conftest の2つ目の
リダイレクトも2つ目の刈り取り規則も要らない。

三分割は2つを重ねるだけで落ちてくる:

    拒否の前に grant 行が無い       -> never_authorized
    grant 行が TTL 以内             -> EVICTED（本来なら通っていた）
    grant 行が TTL より古い         -> aged_out

**`before_the_record` は飾りではなく、この作業の要点そのもの。** 記録が始まる前の拒否を
`never_authorized` に数えたら、計器の不在を計測結果として報告することになる。それは
このリポジトリが何度も踏んでいる「成功の形をした失敗」で、まさに今回それを潰しに来た。
実測: 導入直後の live 台帳で 543/543 が `before_the_record`、`since` は null。
"""
from __future__ import annotations

import json

import pytest

from tools import lock_state as L


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """実ファイルは絶対に触らせない。この隔離を落としたテストが運用台帳を汚した前科がある
    （`relay/fleet_toolset.py::SHADOW_LOG` の219→220）。"""
    p = tmp_path / "lock_refusals.jsonl"
    monkeypatch.setattr(L, "_LOG_FILE", p)
    monkeypatch.setattr(L, "_STATE_FILE", tmp_path / "lock_state.json")
    return p


def _write(path, rows):
    with open(str(path), "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _granted(ts, ip="1.2.3.4", sess="S", via="token"):
    return {"ts": ts, "event": "granted", "client_ip": ip, "session": sess, "via": via}


def _refused(ts, ip="1.2.3.4", sess="S"):
    return {"ts": ts, "event": "refused", "client_ip": ip, "session": sess, "detail": "x"}


# ─────────────────────────────────────────────────────────── 書き手

def test_a_grant_is_written_as_its_own_event_in_the_same_file(ledger):
    """同じ台帳・別の event。これが「2つ目のファイルを作らない」という判断の実体。"""
    L.record_granted("10.0.0.1", "sess-a", via="password", ts=100.0)
    rows = L._rows(ledger)
    assert len(rows) == 1
    assert rows[0] == {"ts": 100.0, "event": "granted", "client_ip": "10.0.0.1",
                       "session": "sess-a", "via": "password"}


def test_a_grant_without_a_session_records_nothing(ledger):
    """セッションの無い解錠について書ける事実はひとつも無い。空行を足せば
    `never_authorized` の母数だけが狂う。"""
    L.record_granted("10.0.0.1", "", via="password")
    assert L._rows(ledger) == []


def test_recording_a_grant_never_raises_even_when_the_file_cannot_be_written(tmp_path, monkeypatch):
    """台帳はリクエストを落としてはならない。`record_locked` と同じ契約。"""
    monkeypatch.setattr(L, "_LOG_FILE", tmp_path / "no" / "such" / "dir" / "x.jsonl")
    monkeypatch.setattr(L, "_append_log", lambda *_a, **_k: (_ for _ in ()).throw(IOError("disk")))
    L.record_granted("10.0.0.1", "sess-a")   # must not raise


def test_a_grant_does_not_overwrite_the_last_refusal_state(ledger, tmp_path):
    """`record_locked` は `_STATE_FILE` も書く。grant がそれを塗り替えたら
    「直前に拒否されたか」の読み手が黙って嘘になる。"""
    L.record_locked("10.0.0.1", "refused once")
    before = L.read_state()
    L.record_granted("10.0.0.1", "sess-a")
    assert L.read_state() == before


# ─────────────────────────────────────────────────────────── 読み手：三分割

def test_a_refusal_inside_the_ttl_of_a_grant_is_EVICTED(ledger):
    """本命。TTL内に承認記録があるのに拒否された＝ `_session_authorized` が True を
    返していたはずで、表から落ちた以外に説明が無い。上限512に対する実測値になる。"""
    _write(ledger, [_granted(1000.0), _refused(1100.0)])
    r = L.explain_refusals(ledger, ttl_s=1800.0, warmup_s=0)
    assert (r["evicted"], r["aged_out"], r["never_authorized"]) == (1, 0, 0)


def test_a_refusal_past_the_ttl_is_AGED_OUT(ledger):
    _write(ledger, [_granted(1000.0), _refused(1000.0 + 1800.0)])
    r = L.explain_refusals(ledger, ttl_s=1800.0, warmup_s=0)
    assert (r["evicted"], r["aged_out"], r["never_authorized"]) == (0, 1, 0)


def test_a_refusal_with_no_earlier_grant_is_NEVER_AUTHORIZED(ledger):
    """別セッションの grant は説明にならない。"""
    _write(ledger, [_granted(1000.0, sess="other"), _refused(1100.0, sess="S")])
    r = L.explain_refusals(ledger, ttl_s=1800.0, warmup_s=0)
    assert (r["evicted"], r["aged_out"], r["never_authorized"]) == (0, 0, 1)


def test_the_same_session_under_a_different_ip_does_not_explain_it(ledger):
    """承認は identity の中に記録される（`state[ip]["sessions"]`、別表ではない）。
    読み手が (ip, session) ではなく session だけで結合したら、その設計を無言で覆す。"""
    _write(ledger, [_granted(1000.0, ip="10.0.0.1"), _refused(1100.0, ip="10.0.0.2")])
    r = L.explain_refusals(ledger, ttl_s=1800.0, warmup_s=0)
    assert (r["evicted"], r["never_authorized"]) == (0, 1)


def test_a_grant_AFTER_the_refusal_does_not_explain_it(ledger):
    """設計どおりの初回拒否（実測67件）はこの形。後の承認で前の拒否を説明したら、
    因果が逆になる。

    **台帳には先に別セッションの grant を置く。** 拒否より後の grant は、それしか無ければ
    定義上「台帳で最初の grant」であり、その拒否は記録が存在しない時刻に起きている。
    `warmup_s=0` にしても変わらない — 暖機の話ではなく、記録の開始そのものだから。
    因果の性質を見たいなら、記録が既に在る台帳を作らなければならない。最初の版はこれを
    取り違えて、分類器のバグの顔をした失敗を出した。"""
    _write(ledger, [_granted(0.0, sess="already-recording"),
                    _refused(1000.0), _granted(1100.0)])
    r = L.explain_refusals(ledger, ttl_s=1800.0, warmup_s=0)
    assert (r["never_authorized"], r["evicted"], r["aged_out"]) == (1, 0, 0)


# ─────────────────────────────────────────────────────────── 暖機：分類とは別の規則

def test_a_refusal_within_one_TTL_of_the_first_grant_is_not_judged(ledger):
    """**ここは「この結合の弱点」ではなく、弱点を報告しないための欄。**

    記録開始の5分前に承認されたセッションは、そこから25分間ずっと有効なまま。その25分の
    間の拒否には grant 行が無いが、それはセッションの性質ではなく計器の都合。
    `never_authorized` に数えたら、計器の起動を「発見」として報告することになり、しかも
    それはちょうど今調べている母集団の上に落ちる。

    **この分岐は、テストを書いていて分類の期待が外れたときに見つかった。** 最初の版は
    grant 1件・refusal 1件の台帳で因果と暖機の両方を一度に踏んでいて、落ち方は分類器の
    バグの顔をしていた。`warmup_s` が `ttl_s` と別の引数なのはそれが理由。
    """
    _write(ledger, [_granted(1000.0, sess="other"), _refused(1500.0, sess="S")])
    warm = L.explain_refusals(ledger, ttl_s=1800.0)              # 既定 = 1 TTL
    assert warm["before_the_record"] == 1 and warm["never_authorized"] == 0

    cold = L.explain_refusals(ledger, ttl_s=1800.0, warmup_s=0)
    assert cold["before_the_record"] == 0 and cold["never_authorized"] == 1


def test_past_the_warmup_an_absent_grant_is_a_fact_about_the_session(ledger):
    """暖機が終われば、grant 行が無いことはセッションについての事実になる。
    更新の絞りは TTL の1/4なので、まだ有効な承認は1 TTL の間に必ず1回は記録される。"""
    _write(ledger, [_granted(0.0, sess="other"), _refused(5000.0, sess="S")])
    r = L.explain_refusals(ledger, ttl_s=1800.0)
    assert r["never_authorized"] == 1 and r["before_the_record"] == 0


def test_the_nearest_earlier_grant_is_the_one_that_counts(ledger):
    """更新のたびに行が増えるので、最初の grant で判定すると全部 aged_out になる。

    **最初の版は `0 / 1000 / 拒否1100` で、これを判別できていなかった** — 最古の grant で
    測っても 1100 は TTL 内なので、`prior[-1]` を `prior[0]` に変えても緑のままだった
    （変異で確認）。最古を TTL の外に置いて初めて、2つの読み方が別の答えを出す。"""
    _write(ledger, [_granted(0.0), _granted(5000.0), _refused(5100.0)])
    r = L.explain_refusals(ledger, ttl_s=1800.0, warmup_s=0)
    assert r["evicted"] == 1 and r["aged_out"] == 0


# ─────────────────────────────────────────────────────────── 計器の不在を計測値にしない

def test_refusals_older_than_the_record_are_counted_SEPARATELY(ledger):
    """**この作業の要点。** grant 行が存在する前の拒否は、承認されていなかったのではなく
    *分からない*。`never_authorized` に混ぜた瞬間、計器の不在が測定結果の顔をする。"""
    _write(ledger, [_refused(500.0), _granted(1000.0), _refused(1100.0)])
    r = L.explain_refusals(ledger, ttl_s=1800.0, warmup_s=0)
    assert r["before_the_record"] == 1
    assert r["never_authorized"] == 0
    assert r["evicted"] == 1
    assert r["since"] == 1000.0


def test_with_no_grants_at_all_every_refusal_is_before_the_record(ledger):
    """導入直後の live 台帳がこれ（543/543、since=null）。ゼロが3つ並ぶのは
    「何も起きていない」ではなく「まだ何も測れていない」。"""
    _write(ledger, [_refused(500.0), _refused(600.0)])
    r = L.explain_refusals(ledger, ttl_s=1800.0, warmup_s=0)
    assert r["before_the_record"] == 2
    assert r["never_authorized"] == 0 and r["evicted"] == 0 and r["aged_out"] == 0
    assert r["since"] is None


def test_refusals_without_a_session_are_not_counted_at_all(ledger):
    """セッションを持たない拒否について、この結合は何も言えない。母数に入れたら
    分母が嘘になる。"""
    _write(ledger, [_granted(1000.0),
                    {"ts": 1100.0, "event": "refused", "client_ip": "1.2.3.4"}])
    r = L.explain_refusals(ledger, ttl_s=1800.0, warmup_s=0)
    assert r["refusals_with_session"] == 0


def test_grant_rows_are_not_themselves_counted_as_refusals(ledger):
    _write(ledger, [_granted(1000.0), _granted(1200.0)])
    r = L.explain_refusals(ledger, ttl_s=1800.0, warmup_s=0)
    assert r["refusals_with_session"] == 0


# ─────────────────────────────────────────────────────────── 壊れた入力

def test_a_corrupt_line_does_not_stop_the_other_rows(ledger):
    """1行で落ちる台帳は、残り6000行について何も答えない。"""
    with open(str(ledger), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(_granted(1000.0)) + "\n")
        fh.write("{not json at all\n")
        fh.write("\n")
        fh.write(json.dumps(_refused(1100.0)) + "\n")
    r = L.explain_refusals(ledger, ttl_s=1800.0, warmup_s=0)
    assert r["evicted"] == 1


def test_a_missing_ledger_reads_as_empty_rather_than_raising(tmp_path):
    r = L.explain_refusals(tmp_path / "nope.jsonl")
    assert r["refusals_with_session"] == 0 and r["since"] is None


def test_a_row_with_a_non_numeric_ts_is_skipped(ledger):
    _write(ledger, [dict(_granted(1000.0), ts="soon"), _refused(1100.0)])
    r = L.explain_refusals(ledger, ttl_s=1800.0, warmup_s=0)
    assert r["before_the_record"] == 1     # the only grant was unusable, so nothing is known


# ─────────────────────────────────────────────────────────── 配線

def test_the_unlock_boundary_actually_records_grants():
    """**ソース断言だが、ここでしか置けない。** 実際の grant は `tools/security.py` の
    2箇所（トークン経路と password 経路）で起きる。あの2行が消えたら台帳は永遠に
    `before_the_record` を返し続け、しかもテストは全部緑のまま — 今日まさにそれを
    潰しに来たのに、同じ形で再発する。security.py は frozen なので import して
    呼ぶ実行時テストは再署名を要求する。ここは配線が在ることだけを見る。"""
    import inspect

    from tools import security as S

    assert "record_granted" in inspect.getsource(S._maybe_touch_session), \
        "the throttled refresh path no longer records the grant"
    assert "record_granted" in inspect.getsource(S.unlock), \
        "a successful password unlock no longer records the grant"


def test_the_cli_exposes_the_reader():
    """読まれない報告は報告ではない — `token_gap` が26日読まれなかったのと同じ轍。"""
    import inspect

    src = inspect.getsource(L._cli)
    assert '"explain"' in src and "explain_refusals()" in src
