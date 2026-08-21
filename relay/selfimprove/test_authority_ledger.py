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
    monkeypatch.setenv("MCP_NOTIFY_LANG", "en")   # 文言は運用者の言語で出る。検査は英語で固定。
    calls = _fired(monkeypatch, tmp_path, L.REBLESS, authorization="operator said so")
    assert len(calls) == 1
    assert "re-signed" in calls[0]["title"]
    assert "operator said so" in calls[0]["body"], (
        "自称の authorization を本人に見せないと、"
        "『そんな指示はしていない』と気づける唯一の機会が消える")


def test_a_mismatch_is_the_urgent_one(tmp_path, monkeypatch):
    """再署名はたいてい頼まれた行為。不一致は**頼まれずに判定者が動いた**こと。
    検知は前からあったが、台帳に書くだけで誰にも告げていなかった。"""
    monkeypatch.setenv("MCP_NOTIFY_LANG", "en")
    calls = _fired(monkeypatch, tmp_path, L.BASELINE_MISMATCH, changed={"x.py": {}})
    # 『!』前置はやめた -- 差出人不明のトーストに『!』が付きコマンドの実行を促す形は
    # 詐欺の見た目そのもので、実際に『ウイルス?』と受け取られた。緊急は読み手の言語の
    # 語で伝え、差出人は app_id で名乗る。検査すべきは印ではなく「緊急だと分かること」。
    assert calls[0]["title"].startswith(L._t("needs_attention", "en"))
    assert not calls[0]["title"].startswith("! ")
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
                             "reason": "r"}, "en")
    assert "FAILS" in body and "git revert" in body


def test_no_notification_ends_on_a_warning_with_no_next_step():
    """『気をつけろ』で終わる通知は制御ではなく警報。3種すべてに次の一手を持たせる。"""
    from relay.selfimprove import authority_ledger as AL
    for ev in (AL.BASELINE_MISMATCH, AL.REVOKE, "REBLESS"):
        _t, body = AL._headline({"event": ev, "changed": {"relay/x.py": "h"},
                                 "reason": "r", "authorization": "a"})
        assert ("python -m relay.selfimprove.frozen" in body
                or "git revert" in body), ev


# ---- クリックできる通知（2026-08-21、運用者の指摘より） ---------------------------------------
#
# 「これクリックしても特に何も開かず解除も何もできないぞ」。加えてトースト本文の改行が
# 文字として ¥n と表示されていた -- PowerShell の二重引用符文字列は \n を改行と解釈しない。
# 前者は launch（protocol activation）、後者は本文の base64 受け渡しで直した。
# ここで見張るのは、クリック先のファイルが必ず「次の一手」を持っていること。

def test_the_click_target_says_how_to_undo(tmp_path, monkeypatch):
    from relay.selfimprove import authority_ledger as AL
    monkeypatch.setenv(AL.ENV_PATH, str(tmp_path / "led.jsonl"))
    rec = {"event": AL.REBLESS, "at": "2026-08-21T13:00:00",
           "changed": {"relay/x.py": {}}, "reason": "r", "authorization": "a",
           "actor_claimed": "cli"}
    title, body = AL._headline(rec)
    uri = AL._write_briefing(rec, title, body)
    assert uri.startswith("file:///")
    text = (tmp_path / "selfimprove_last_act.txt").read_text(encoding="utf-8")
    # 行動ブロックの中にあること。本文にも同じコマンドが埋まっているので、
    # 「どこかに含まれる」だけでは専用の行を消しても通ってしまう -- 実際に素通りした。
    after_rule = text.split("-" * 72, 1)[-1]
    assert AL.UNDO_CMD in after_rule, "手順ブロックに取り消しコマンドが無い"
    assert AL.VERIFY_CMD in after_rule
    assert "git revert" in after_rule
    assert "relay/x.py" in text and "a" in text


def test_the_briefing_sits_beside_the_ledger_not_in_the_repo(tmp_path, monkeypatch):
    """台帳と同じ場所に置く -- 片方だけ残る状態を作らない。リポジトリ内に書くと
    clean checkout で消え、追跡すると学習した内容を公開してしまう。"""
    from relay.selfimprove import authority_ledger as AL
    monkeypatch.setenv(AL.ENV_PATH, str(tmp_path / "sub" / "led.jsonl"))
    assert AL.briefing_path().replace("\\", "/").endswith("/sub/selfimprove_last_act.txt")


def test_a_briefing_that_cannot_be_written_does_not_break_the_alert(monkeypatch):
    """手順書が書けないことで、通知そのものや記録が止まってはいけない。"""
    from relay.selfimprove import authority_ledger as AL
    monkeypatch.setattr(AL, "briefing_path", lambda *a, **k: "\0/impossible/x.txt")
    assert AL._write_briefing({"event": "rebless"}, "t", "b") == ""


def test_the_notification_asks_for_a_click_target(monkeypatch, tmp_path):
    """launch を渡し忘れると、直したはずのクリックが黙って死ぬ。"""
    from relay.selfimprove import authority_ledger as AL
    monkeypatch.setenv(AL.ENV_PATH, str(tmp_path / "led.jsonl"))
    seen = {}

    import tools.notify_ops as N
    monkeypatch.setattr(N, "notify_desktop",
                        lambda **kw: seen.update(kw) or "ok")
    AL._notify({"event": AL.REBLESS, "changed": {"relay/x.py": {}}, "reason": "r",
                "authorization": "a", "at": "t"})
    assert seen.get("launch", "").startswith("file:///")
    assert AL.UNDO_CMD in seen.get("body", "")


# ---- 通知の説明文が「いつ」を言えること -----------------------------------------------------------

def test_the_briefing_reads_the_timestamp_key_the_ledger_actually_writes(tmp_path,
                                                                        monkeypatch):
    """`append` は `ts` を書き、`at` を書いたレコードは一度も無い。
    briefing は `at` を読んでいたので空欄になり、
    グレーダーの変更を告げる監査記録が『いつ』を持たないまま運用者に届いた。"""
    from relay.selfimprove import authority_ledger as AL
    p = tmp_path / "authority.jsonl"
    monkeypatch.setattr(AL, "_notify", lambda record: None)
    rec = AL.append(AL.REBLESS, reason="because", actor_claimed="test",
                    authorization="operator said so", path=str(p))
    assert "ts" in rec and "at" not in rec
    when = AL._when(rec)
    assert when and when != "(no timestamp)"
    assert when[:2] == "20", when


def test_a_record_without_a_timestamp_says_so_rather_than_going_blank():
    """空欄は『ここには言うことがない』と読める。
    言えないなら、言えないと書く。"""
    from relay.selfimprove import authority_ledger as AL
    assert AL._when({}, "en") == "(no timestamp)"
    assert AL._when({"ts": None}, "ja") == "(日時なし)"


def test_the_briefing_body_carries_the_time(tmp_path, monkeypatch):
    from relay.selfimprove import authority_ledger as AL
    monkeypatch.setattr(AL, "_notify", lambda record: None)
    p = tmp_path / "authority.jsonl"
    rec = AL.append(AL.REBLESS, reason="r", actor_claimed="t", path=str(p))
    rec = dict(rec, _ledger_path=str(p))
    monkeypatch.setenv("MCP_NOTIFY_LANG", "en")
    AL._write_briefing(rec, "The constitution was re-signed", "body")
    text = open(AL.briefing_path(str(p)), encoding="utf-8").read()
    assert "when" in text
    assert "(no timestamp)" not in text, text[:300]


# ---- 運用者の言語で届くこと ---------------------------------------------------------------------

def test_the_notification_follows_the_operators_language(monkeypatch):
    """日本語環境の利用者の多くは、英語の通知の意味を取れない。
    取れない警告は、検知の費用だけ払って伝達に失敗している。"""
    from relay.selfimprove import authority_ledger as AL
    rec = {"event": AL.REBLESS, "reason": "r", "authorization": "a", "changed": {"x.py": {}}}
    ja_title, ja_body = AL._headline(rec, "ja")
    en_title, en_body = AL._headline(rec, "en")
    assert ja_title != en_title
    assert "承認" in ja_title and "re-signed" in en_title


def test_the_operators_own_words_are_never_translated():
    """reason と authorization は運用者の発言と、誰かが主張した権限の根拠。
    言い換えは実行者による自分の権限の要約であり、
    読み手が自分で判断したい唯一のものを奪う。"""
    from relay.selfimprove import authority_ledger as AL
    rec = {"event": AL.REBLESS, "reason": "keep this exact string",
           "authorization": "これも一字一句", "changed": {"x.py": {}}}
    for lang in ("ja", "en"):
        _, body = AL._headline(rec, lang)
        assert "keep this exact string" in body
        assert "これも一字一句" in body


def test_language_can_be_forced_for_a_deterministic_test(monkeypatch):
    from relay.selfimprove import authority_ledger as AL
    monkeypatch.setenv("MCP_NOTIFY_LANG", "en")
    assert AL.ui_language() == "en"
    monkeypatch.setenv("MCP_NOTIFY_LANG", "ja")
    assert AL.ui_language() == "ja"


def test_an_unknown_locale_falls_back_to_english(monkeypatch):
    from relay.selfimprove import authority_ledger as AL
    monkeypatch.setenv("MCP_NOTIFY_LANG", "kl")
    monkeypatch.setattr(AL, "ui_language", lambda: "kl")
    assert AL._t("rebless_title", "kl") == AL._t("rebless_title", "en")


# ---- マルウェアに見えないこと -------------------------------------------------------------------

def test_an_urgent_alert_does_not_open_with_a_bare_exclamation_mark():
    """差出人の分からないトーストに『!』が付き、コマンドを実行しろと書いてある --
    それは詐欺の見た目そのもので、実際に『ウイルス?』と受け取られた。
    一目で消される警告は、警告が無いより悪い。"""
    import ast
    import inspect
    from relay.selfimprove import authority_ledger as AL
    src = inspect.getsource(AL._notify)
    tree = ast.parse(src.lstrip())
    literals = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "! " not in literals


def test_the_notification_names_its_sender():
    """信じてもらう一番の近道は、見覚えがあること。"""
    from relay.selfimprove import authority_ledger as AL
    import inspect
    assert AL.NOTIFY_APP_ID
    assert "app_id=NOTIFY_APP_ID" in inspect.getsource(AL._notify)


def test_urgency_is_carried_by_a_word_in_the_readers_language():
    from relay.selfimprove import authority_ledger as AL
    assert AL._t("needs_attention", "ja") == "要確認"
    assert AL._t("needs_attention", "en") == "Needs attention"


# ---- 開いた人が「これは何か」を最初に読めること -----------------------------------------------------

def test_the_briefing_opens_by_saying_what_it_is(tmp_path, monkeypatch):
    """勝手に開き、このリポジトリしか使わない語で始まるファイルを見せられて
    『これは何』と訊かれた。それが答えのある場所。"""
    from relay.selfimprove import authority_ledger as AL
    monkeypatch.setenv("MCP_NOTIFY_LANG", "ja")
    monkeypatch.setattr(AL, "_notify", lambda r: None)
    p = tmp_path / "authority.jsonl"
    rec = AL.append(AL.REBLESS, reason="r", actor_claimed="t", path=str(p))
    rec = dict(rec, _ledger_path=str(p))
    AL._write_briefing(rec, *AL._headline(rec))
    text = open(AL.briefing_path(str(p)), encoding="utf-8").read()
    assert "これは何か" in text.split("\n")[3]


def test_the_briefing_does_not_print_the_same_two_fields_twice(tmp_path, monkeypatch):
    """表に出したものを本文でも繰り返すと、読み手は差分を探してしまう。"""
    from relay.selfimprove import authority_ledger as AL
    monkeypatch.setenv("MCP_NOTIFY_LANG", "ja")
    monkeypatch.setattr(AL, "_notify", lambda r: None)
    p = tmp_path / "authority.jsonl"
    rec = AL.append(AL.REBLESS, reason="UNIQUEREASON", actor_claimed="t",
                    authorization="UNIQUEAUTH", path=str(p))
    rec = dict(rec, _ledger_path=str(p))
    AL._write_briefing(rec, *AL._headline(rec))
    text = open(AL.briefing_path(str(p)), encoding="utf-8").read()
    assert text.count("UNIQUEREASON") == 1, text
    assert text.count("UNIQUEAUTH") == 1, text


# ---- 通知の行き先はダッシュボード（2026-08-21、運用者の指摘より） ---------------------------
#
# 「通知をクリックすると txt が出る。それをどうするんだい」。正しい行き先は既にあった --
# 自己改善ダッシュボードの「権限の履歴」に、台帳・凍結セットの自前照合・
# 「直前の再署名を取り消す」ボタンが揃っている。無かったのは導線だけだった。

def test_the_alert_opens_the_dashboard_not_only_a_file(monkeypatch, tmp_path):
    from relay.selfimprove import authority_ledger as AL
    monkeypatch.setenv(AL.ENV_PATH, str(tmp_path / "led.jsonl"))
    opened, sent = [], {}

    import tools.notify_ops as N
    monkeypatch.setattr(N, "open_authority_dashboard", lambda: opened.append(1) or "cockpit")
    monkeypatch.setattr(N, "notify_desktop", lambda **kw: sent.update(kw) or "ok")
    AL._notify({"event": AL.REBLESS, "changed": {"relay/x.py": {}}, "reason": "r",
                "authorization": "a", "at": "t"})
    assert opened == [1], "ダッシュボードを開いていない"
    assert sent.get("launch", "").startswith("file:///"), "UI が無い環境の受け皿が消えている"


def test_a_machine_without_the_ui_still_gets_the_briefing(monkeypatch, tmp_path):
    """サーバだけを動かしているホストでは EXE が無い。そこでは txt が唯一の説明になる。"""
    from relay.selfimprove import authority_ledger as AL
    monkeypatch.setenv(AL.ENV_PATH, str(tmp_path / "led.jsonl"))
    sent = {}

    import tools.notify_ops as N
    monkeypatch.setattr(N, "open_authority_dashboard", lambda: "")
    monkeypatch.setattr(N, "notify_desktop", lambda **kw: sent.update(kw) or "ok")
    AL._notify({"event": AL.REBLESS, "changed": {"relay/x.py": {}}, "reason": "r",
                "authorization": "a", "at": "t"})
    assert AL.UNDO_CMD in sent.get("body", "")
    assert sent.get("launch", "").startswith("file:///")


def test_a_cockpit_that_will_not_start_does_not_break_the_alert(monkeypatch, tmp_path):
    from relay.selfimprove import authority_ledger as AL
    monkeypatch.setenv(AL.ENV_PATH, str(tmp_path / "led.jsonl"))
    sent = {}

    import tools.notify_ops as N

    def boom():
        raise RuntimeError("cockpit exploded")

    monkeypatch.setattr(N, "open_authority_dashboard", boom)
    monkeypatch.setattr(N, "notify_desktop", lambda **kw: sent.update(kw) or "ok")
    AL._notify({"event": AL.REBLESS, "changed": {"relay/x.py": {}}, "reason": "r",
                "authorization": "a", "at": "t"})
    # 記録も通知も、UI の都合で失われてはいけない
    assert sent == {} or "body" in sent


def test_the_switch_the_notification_uses_exists_in_the_ui():
    """Python 側が --authority を渡しても、C# 側が知らなければ普通の窓が開くだけ。
    ビルド成果物ではなくソースで一致を見張る -- EXE は再ビルド待ちのことがある。"""
    import io as _io
    src = _io.open("ui/FleetCockpit.cs", encoding="utf-8").read()
    assert '"--authority"' in src
    assert "SelfImproveDashboardWindow()" in src


# ---- 枝の操作は genome の適用ではない ------------------------------------------------------------

def test_naming_a_branch_is_not_recorded_as_applying_a_genome(tmp_path, monkeypatch):
    """既存のイベント名を流用したので、ラベル作成4件が実活性化7件と
    同じバケツに入った -- 読み手が『稼働ハーネスは変わったか』を
    確かめに行く、まさにその場所で。"""
    from relay.selfimprove import archive as A
    from relay.selfimprove import authority_ledger as AL
    from relay.selfimprove import branches as BR
    monkeypatch.setenv("MCP_SELFIMPROVE_BRANCHES", str(tmp_path / "branches.json"))
    monkeypatch.setattr(AL, "_path", lambda path=None: str(tmp_path / "authority.jsonl"))
    monkeypatch.setattr(AL, "_notify", lambda record: None)
    arc = A.Archive(path=str(tmp_path / "entries.jsonl"))
    gid = arc.add({"components": {"transport": "transport/v2"}}, slice_ids=["e"],
                  pass_at_1=0.5, ci=(0.4, 0.6), gate_verdict="KEEP")
    BR.create("fast", gid, archive=arc)
    BR.delete("fast")
    events = [r.get("event") for r in AL.read(str(tmp_path / "authority.jsonl"))]
    assert AL.BRANCH_CREATE in events and AL.BRANCH_DELETE in events
    assert AL.GENOME_APPLY not in events, "枝の作成が活性化として記録されている"
    assert AL.GENOME_REVERT not in events


def test_branch_events_do_not_interrupt_the_operator():
    """憲法が変わったわけではない。通知を増やせば、本物の通知が読まれなくなる。"""
    from relay.selfimprove import authority_ledger as AL
    assert AL.BRANCH_CREATE not in AL._NOTIFIED
    assert AL.BRANCH_DELETE not in AL._NOTIFIED
    assert AL.BRANCH_CREATE not in AL._URGENT


# ---- 再署名の頻度そのものが言われること -----------------------------------------------------------

def test_the_tool_says_how_often_the_constitution_has_been_re_signed(tmp_path, monkeypatch,
                                                                    capsys):
    """23件が1日に着地した。1件ずつは弁護できて、合計は弁護できない --
    朝食から深夜までに23回改正される憲法は、文書であって制約ではない。
    運用者は1件目を見て『ウイルス?』と訊き、23件目は誰も読まない。"""
    import time
    from relay.selfimprove import authority_ledger as AL
    p = tmp_path / "authority.jsonl"
    monkeypatch.setattr(AL, "_notify", lambda record: None)
    for i in range(AL.RESIGN_NOISY_PER_DAY - 1):
        AL.append(AL.REBLESS, reason="r%d" % i, actor_claimed="t", path=str(p))
    capsys.readouterr()
    AL.append(AL.REBLESS, reason="the one that tips it", actor_claimed="t", path=str(p))
    out = capsys.readouterr().out
    assert "re-signings in the last 24h" in out, out
    assert "sign it once" in out


def test_a_quiet_day_is_not_warned_about(tmp_path, monkeypatch, capsys):
    from relay.selfimprove import authority_ledger as AL
    p = tmp_path / "authority.jsonl"
    monkeypatch.setattr(AL, "_notify", lambda record: None)
    capsys.readouterr()
    AL.append(AL.REBLESS, reason="one", actor_claimed="t", path=str(p))
    assert capsys.readouterr().out == ""


def test_the_rate_warns_and_does_not_block():
    """8回目の不用意な編集と8回目の必要な編集を、道具は見分けられない。
    数えて声に出すことはできる。"""
    import ast
    import inspect
    from relay.selfimprove import authority_ledger as AL
    tree = ast.parse(inspect.getsource(AL._warn_if_resigning_often).lstrip())
    raises = [n for n in ast.walk(tree) if isinstance(n, ast.Raise)]
    assert not raises, "頻度が高いことを理由に止めている"


def test_the_rate_warning_is_not_in_the_delegation_excluded_file():
    """frozen.py は DELEGATION_EXCLUDED -- 委任を執行する機構の再署名は
    委任の外。そこに警告を足せば、『再署名が多すぎる』への修正自体が、
    軽々に再署名してはいけない唯一のファイルの再署名を要求することになる。"""
    import inspect
    from relay.selfimprove import authority_ledger as AL
    from relay.selfimprove import frozen as F
    assert "relay/selfimprove/frozen.py" in F.DELEGATION_EXCLUDED
    assert not hasattr(F, "_warn_if_resigning_often")
    assert hasattr(AL, "_warn_if_resigning_often")


def test_the_count_cannot_be_routed_around_by_another_entry_point():
    """CLI に置くと、別の呼び口から署名すれば数えられない。
    append は再署名が必ず通る場所。"""
    import inspect
    from relay.selfimprove import authority_ledger as AL
    src = inspect.getsource(AL.append)
    assert "_warn_if_resigning_often" in src
    assert "REBLESS" in src[src.index("_warn_if_resigning_often") - 200:]
