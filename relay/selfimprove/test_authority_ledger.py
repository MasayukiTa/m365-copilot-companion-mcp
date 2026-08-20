"""台帳が、書けることと書けないことの両方について正直であること。

この台帳の危険は「壊れること」ではなく「実際より強いものとして読まれること」にある。
外部レビューの判定はこの系全体に及ぶ — 制約する機構が制約される側と同じ特権領域で
動く以上、承認装置はすべて助言に留まる。台帳自身もその例外ではない。
だからテストは、連鎖が繋がることと同じ重さで、**連鎖が見抜けないもの**を固定する。
"""
import io
import json

import pytest

from relay.selfimprove import authority_ledger as L


def _p(tmp_path):
    return str(tmp_path / "ledger.jsonl")


# ---- 書けること ---------------------------------------------------------------------------

def test_the_first_write_lays_down_genesis_carrying_the_contract():
    """4つの文はファイルと一緒に旅する。モジュールにしか無いと、台帳だけ渡された人が
    それを読まない。"""
    import tempfile
    path = tempfile.mkdtemp() + "/l.jsonl"
    L.append(L.REBLESS, reason="r", actor_claimed="a", path=path)
    rows = L.read(path)
    assert rows[0]["event"] == L.GENESIS and rows[0]["seq"] == 0
    assert rows[0]["contract"] == list(L.CONTRACT)
    assert "authorises nothing" in " ".join(rows[0]["contract"])
    assert "self-reported" in " ".join(rows[0]["contract"])


def test_a_chain_of_records_verifies(tmp_path):
    p = _p(tmp_path)
    for i in range(3):
        L.append(L.GENOME_APPLY, reason="try %d" % i, actor_claimed="loop", path=p)
    ok, problems = L.verify(p)
    assert ok, problems
    seq, digest = L.tail(p)
    assert seq == 3 and len(digest) == 64


def test_an_edited_record_breaks_the_chain(tmp_path):
    p = _p(tmp_path)
    L.append(L.REBLESS, reason="first", actor_claimed="operator", path=p)
    L.append(L.GENOME_APPLY, reason="second", actor_claimed="loop", path=p)
    rows = L.read(p)
    rows[1]["reason"] = "something else"
    io.open(p, "w", encoding="utf-8", newline="\n").write(
        "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")) for r in rows) + "\n")
    ok, problems = L.verify(p)
    assert not ok
    assert any("hash does not match" in x for x in problems)


# ---- 書けないこと（ここが本題） ---------------------------------------------------------------

def test_a_truncated_ledger_still_verifies_clean(tmp_path):
    """末尾を切り詰められた台帳は、連鎖として無傷に見える。
    これは欠陥ではなく限界で、対処は「この系では買えない」と結論した。
    verify() の OK を『何も失われていない』と読ませないために、ここで固定する。"""
    p = _p(tmp_path)
    for i in range(4):
        L.append(L.GENOME_APPLY, reason="r%d" % i, actor_claimed="loop", path=p)
    rows = L.read(p)
    io.open(p, "w", encoding="utf-8", newline="\n").write(
        "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")) for r in rows[:-2]) + "\n")
    ok, problems = L.verify(p)
    assert ok, problems          # 検知できない
    # genesis を含めて 5 行、末尾2行を落として seq 0..2。
    assert L.tail(p)[0] == 2     # 末尾だけが証拠


def test_verify_says_so_in_its_own_docstring():
    doc = L.verify.__doc__ or ""
    assert "cannot see" in doc.lower()
    assert "tail" in doc.lower()


def test_an_empty_ledger_is_not_reported_as_healthy_silence(tmp_path):
    ok, problems = L.verify(_p(tmp_path))
    assert ok
    assert any("indistinguishable" in x for x in problems), (
        "空の台帳と一度も使われていない台帳は区別できない。OK だけ返すと、"
        "『記録が無い＝何も起きていない』と読まれる")


# ---- 正直でない記録を拒むこと -----------------------------------------------------------------

def test_a_record_without_a_reason_is_refused(tmp_path):
    with pytest.raises(L.AuthorityLedgerError) as exc:
        L.append(L.REBLESS, reason="   ", actor_claimed="operator", path=_p(tmp_path))
    assert "why" in str(exc.value)


def test_a_record_without_an_actor_is_refused(tmp_path):
    with pytest.raises(L.AuthorityLedgerError):
        L.append(L.REBLESS, reason="r", actor_claimed="", path=_p(tmp_path))


def test_an_unknown_event_is_refused(tmp_path):
    with pytest.raises(L.AuthorityLedgerError):
        L.append("promoted_myself", reason="r", actor_claimed="loop", path=_p(tmp_path))


def test_the_actor_field_is_named_for_what_it_is(tmp_path):
    """`actor` と名付けると、フィールド名自体が「検証済み」を含意する。"""
    p = _p(tmp_path)
    rec = L.append(L.REBLESS, reason="r", actor_claimed="operator", path=p)
    assert "actor_claimed" in rec and "actor" not in rec


def test_authorization_defaults_to_self_initiated_rather_than_blank(tmp_path):
    """空欄は『指示があったが書き忘れた』とも読める。既定は主張の明示にする。"""
    rec = L.append(L.GENOME_APPLY, reason="r", actor_claimed="loop", path=_p(tmp_path))
    assert rec["authorization"] == L.SELF_INITIATED


def test_authorization_is_stored_verbatim(tmp_path):
    """要約は、行為者による自分の権限の解釈。読み手が自分で判断できなくなる。"""
    words = "全て進めてもろて。残件を。"
    rec = L.append(L.REBLESS, reason="r", actor_claimed="agent",
                   authorization=words, path=_p(tmp_path))
    assert rec["authorization"] == words


def test_the_digest_excludes_the_hash_field_itself(tmp_path):
    p = _p(tmp_path)
    rec = L.append(L.REBLESS, reason="r", actor_claimed="operator", path=p)
    assert rec["hash"] == L._digest(rec)
    assert rec["hash"] == L._digest({k: v for k, v in rec.items() if k != "hash"})


def test_the_tail_line_is_printable_for_the_operator(tmp_path):
    p = _p(tmp_path)
    assert "empty" in L.describe_tail(p)
    L.append(L.REBLESS, reason="r", actor_claimed="operator", path=p)
    line = L.describe_tail(p)
    assert "seq=1" in line and "tail=" in line


# ---- apply.py が実際に記録すること ---------------------------------------------------------------

def test_applying_a_genome_leaves_a_record(tmp_path, monkeypatch):
    """genome の適用は「走っているスキャフォルドが何であるか」を変える。
    これまで残るのは store ファイルだけで、いつ・なぜ適用されたかは何も残らなかった。"""
    from relay.selfimprove import apply as AP
    p = _p(tmp_path)
    monkeypatch.setenv(L.ENV_PATH, p)
    store = str(tmp_path / "genome.json")
    AP.apply_genome({"knobs": {}, "cards": {"a": "x"}, "note": "why this genome"}, store)
    rows = [r for r in L.read(p) if r.get("event") == L.GENOME_APPLY]
    assert len(rows) == 1
    assert rows[0]["reason"] == "why this genome"
    assert store in rows[0]["changed"]
    assert rows[0]["changed"][store]["after"], "適用後のダイジェストが空"


def test_reverting_leaves_its_own_record(tmp_path, monkeypatch):
    from relay.selfimprove import apply as AP
    p = _p(tmp_path)
    monkeypatch.setenv(L.ENV_PATH, p)
    store = str(tmp_path / "genome.json")
    AP.apply_genome({"knobs": {}, "cards": {}, "note": "first"}, store)
    AP.apply_genome({"knobs": {}, "cards": {}, "note": "second"}, store)
    assert AP.revert(store) is True
    assert [r["event"] for r in L.read(p)][-1] == L.GENOME_REVERT
    assert L.verify(p)[0]


def test_a_broken_ledger_never_breaks_an_apply(tmp_path, monkeypatch):
    """記録のための機構が、記録される作業を落としてはいけない。
    書けないホームディレクトリ1つで自己改善ループが止まるのは、
    記録の利益に対して高すぎる新しい故障モード。"""
    from relay.selfimprove import apply as AP
    monkeypatch.setenv(L.ENV_PATH, str(tmp_path / "nope" / "x" / "l.jsonl"))
    monkeypatch.setattr(L, "append",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk on fire")))
    store = str(tmp_path / "genome.json")
    got = AP.apply_genome({"knobs": {}, "cards": {}, "note": "n"}, store)
    assert got["note"] == "n"
    assert __import__("os").path.isfile(store)


def test_the_path_is_resolved_at_call_time_not_bound_at_import(tmp_path, monkeypatch):
    """`path=DEFAULT_PATH` を既定引数にすると import 時に束縛され、後から
    差し替えられない。frozen.py が同じ罠でコメントを残している。
    ここでは実害が2つあった: テストが operator の本物の台帳へ書くこと、
    そして最初の配線確認が黙って空を返したこと。"""
    p = _p(tmp_path)
    monkeypatch.setenv(L.ENV_PATH, p)
    L.append(L.REBLESS, reason="r", actor_claimed="test")
    assert L.read(p), "環境変数での差し替えが効いていない"
