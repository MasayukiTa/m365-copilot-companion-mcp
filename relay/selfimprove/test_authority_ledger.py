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


# ---- 恒久委任下では「聞かなかった」が合図にならないので、事後に届かせる -------------------------------

def _fired(monkeypatch, tmp_path, event, **kw):
    import tools.notify_ops as N
    calls = []
    monkeypatch.setenv(L.ENV_PATH, _p(tmp_path))
    monkeypatch.setattr(N, "notify_desktop", lambda **k: calls.append(k) or "ok")
    L.append(event, reason=kw.pop("reason", "r"), actor_claimed="a", **kw)
    return calls


def test_a_resigning_reaches_the_operator(tmp_path, monkeypatch):
    """委任したので事前には聞かない。届かなければ、ダッシュボードを開くまで誰も知らない。"""
    calls = _fired(monkeypatch, tmp_path, L.REBLESS, authorization="operator said so")
    assert len(calls) == 1
    assert "re-signed" in calls[0]["title"]
    assert "operator said so" in calls[0]["body"], (
        "自称の authorization を本人に見せないと、"
        "『そんな指示はしていない』と気づける唯一の機会が消える")


def test_a_mismatch_is_the_urgent_one(tmp_path, monkeypatch):
    """再署名はたいてい頼まれた行為。不一致は**頼まれずに判定者が動いた**こと。
    検知は前からあったが、台帳に書くだけで誰にも告げていなかった。"""
    calls = _fired(monkeypatch, tmp_path, L.BASELINE_MISMATCH, changed={"x.py": {}})
    assert calls[0]["title"].startswith("! ")
    assert "Nobody approved" in calls[0]["body"]


def test_routine_genome_activity_does_not_notify(tmp_path, monkeypatch):
    """genome の適用は日常。全部鳴らすと、鳴っても読まれなくなる。"""
    assert _fired(monkeypatch, tmp_path, L.GENOME_APPLY) == []


def test_the_notification_call_passes_only_arguments_that_exist():
    """存在しない引数を渡すと TypeError が except に飲まれ、通知は黙って永久に不発になる。
    実際に一度 urgency= を渡しかけた。

    本文の文字列検索では駄目で、最初の版はコメント中の "urgency" を拾って落ちた。
    ast で**呼び出しのキーワード**を見る。notify_desktop 自身は装飾されていて
    署名内省が *args/**kwargs を返すので、実引数名は元関数から取る。
    """
    import ast
    from tools import notify_ops as N

    # SOURCE, NOT THE LIVE OBJECT. Under pytest something wraps notify_desktop, so
    # inspect.signature reports (*args, **kwargs) and the check would pass against anything.
    # The definition in the file is what the call has to agree with.
    ndef = ast.parse(open(N.__file__, encoding="utf-8").read())
    allowed = None
    for node in ast.walk(ndef):
        if isinstance(node, ast.FunctionDef) and node.name == "notify_desktop":
            allowed = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
    assert allowed and "title" in allowed and "urgency" not in allowed, allowed

    tree = ast.parse(open(L.__file__, encoding="utf-8").read())
    seen = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (getattr(node.func, "id", None)
                                           or getattr(node.func, "attr", None)) == "notify_desktop":
            seen.append({kw.arg for kw in node.keywords})
    assert seen, "notify_desktop の呼び出しが見つからない"
    for kwargs in seen:
        extra = kwargs - allowed
        assert not extra, "notify_desktop に無い引数を渡している: %s" % sorted(extra)


def test_a_failing_notifier_never_blocks_the_record(tmp_path, monkeypatch):
    import tools.notify_ops as N
    monkeypatch.setenv(L.ENV_PATH, _p(tmp_path))
    monkeypatch.setattr(N, "notify_desktop",
                        lambda **k: (_ for _ in ()).throw(OSError("no toast here")))
    rec = L.append(L.REBLESS, reason="r", actor_claimed="a")
    assert rec["seq"] == 1 and L.verify(_p(tmp_path))[0]


# ---- 通知は行動可能でなければならない（2026-08-21、運用者の指摘より） -------------------------
#
# 旧文面は「あなたがそう言っていないなら、そう言ったことをここでは何も検証していません」で
# 終わっていた。何をすればよいかが1行も無い。運用者の言葉では
# 「通知にもなんかくるけどそれどうしろっていうんだよぉになる」。
# 取り消し方は最初から存在していた -- 通知が知らせていなかっただけ。

def test_a_re_signing_tells_you_how_to_withdraw_it():
    from relay.selfimprove import authority_ledger as AL
    _t, body = AL._headline({"event": "REBLESS", "changed": {"relay/x.py": "h"},
                             "reason": "r", "authorization": "a"})
    assert AL.UNDO_CMD in body
    assert "--revoke" in body


def test_an_unapproved_change_tells_you_how_to_look_at_it():
    from relay.selfimprove import authority_ledger as AL
    _t, body = AL._headline({"event": AL.BASELINE_MISMATCH, "changed": {"relay/x.py": "h"}})
    assert AL.VERIFY_CMD in body
    assert "git revert" in body


def test_a_withdrawal_says_what_state_you_are_now_in():
    """--revoke の直後は凍結チェックが落ちる。それが目的だと書いていないと、
    取り消した人は『壊した』と思って元に戻す。"""
    from relay.selfimprove import authority_ledger as AL
    _t, body = AL._headline({"event": AL.REVOKE, "changed": {"relay/x.py": "h"},
                             "reason": "r"})
    assert "FAILS" in body and "git revert" in body


def test_no_notification_ends_on_a_warning_with_no_next_step():
    """『気をつけろ』で終わる通知は制御ではなく警報。3種すべてに次の一手を持たせる。"""
    from relay.selfimprove import authority_ledger as AL
    for ev in (AL.BASELINE_MISMATCH, AL.REVOKE, "REBLESS"):
        _t, body = AL._headline({"event": ev, "changed": {"relay/x.py": "h"},
                                 "reason": "r", "authorization": "a"})
        assert ("python -m relay.selfimprove.frozen" in body
                or "git revert" in body), ev
