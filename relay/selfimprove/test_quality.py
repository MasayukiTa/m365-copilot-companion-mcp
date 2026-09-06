"""Unit tests for the persona-leak scorer (quality.py).

Run: python -m relay.selfimprove.test_quality

Hermetic: stdlib only. Fixtures are inline labelled strings (CLEAN/dirty) plus synthetic
history items + transcript jsonl built in a TemporaryDirectory. No real ledger is touched.
"""
import json
import os
import tempfile

from relay.selfimprove import quality as Q


# --------------------------------------------------------------------------------------------------
# Fixtures: CLEAN (淡々と事実ベース) vs dirty (複数クラス発火の persona)
# --------------------------------------------------------------------------------------------------

# 2026-06-28 の 5本クリーン実走の文体: 推奨は述べるが命令調でない・上から目線でない・自我なし。
_CLEAN = [
    # merge vs rebase
    "マージ(merge)を基本に使う。理由(実務上の判断基準): 履歴の事実をそのまま残せて追跡が容易だから。"
    "鉄則: push済み・共有済みの履歴は rebase しない。ローカルの未共有ブランチを整える用途に限り rebase を使う。",
    # async 初心者向け
    "非同期処理は、I/O待ちの間に別の処理を進めるための仕組みです。まず同期版で動かし、ボトルネックが"
    "I/O待ちだと確認できたら async/await に置き換えると効果が出ます。CPU律速の処理には向きません。",
    # コードレビューの要点
    "コードレビューでは、正しさ・可読性・テスト有無の3点を見ると漏れが少ないです。"
    "差分が大きい場合は目的単位で分割し、各コミットが単独で意味を持つ状態を推奨します。",
    # 初心者が最初に学ぶこと
    "最初は、変数・条件分岐・繰り返し・関数の4つを押さえると土台になります。"
    "小さな課題を解きながら進めると定着しやすいです。網羅より反復が効きます。",
    # REST vs GraphQL
    "REST は資源単位で素直、GraphQL は必要なフィールドだけ取得できるのが利点です。"
    "クライアントが多様で過不足のないデータ取得が要件なら GraphQL、単純なCRUD中心なら REST が無難です。",
]

# dirty: coaching + condescension + ego + preface が混じる
_DIRTY = [
    "結論から言うと、まずは基礎を完璧に固めろ。今の理解レベルだと初心者の9割は詰む。"
    "言っておくが、フレームワークから入るやつは伸びない。俺に言わせれば順番が逆だ。",
    "いいか、まずは手を動かせ。はっきり言って、お前の理解度だと設計の話はまだ早い。"
    "俺のやり方では、最初の3ヶ月は写経で十分だ。",
    "そもそも基礎がわかってない。まず大事なのは型を覚えることだ。"
    "初心者の9割がここで挫折するのがオチだぞ。覚えておいてほしいのは、近道はないということだ。",
]


def test_clean_examples_are_not_leak():
    for i, txt in enumerate(_CLEAN):
        res = Q.score_text(txt)
        assert res["persona_leak"] is False, "CLEAN #%d misflagged: signals=%r" % (i, res["signals"])
        assert res["judged_by"] == "heuristic"
        assert 0.0 <= res["score"] <= 1.0
    print("ok test_clean_examples_are_not_leak")


def test_dirty_examples_are_leak():
    for i, txt in enumerate(_DIRTY):
        res = Q.score_text(txt)
        assert res["persona_leak"] is True, "dirty #%d missed: %r" % (i, res)
        # 少なくとも1つ以上のシグナルクラスが発火している
        assert len(res["signals"]) >= 1
        assert res["score"] > 0.0
    print("ok test_dirty_examples_are_leak")


def test_two_distinct_classes_trips_leak():
    # coaching(まずは固めろ) + condescension(初心者の9割) の2クラス -> leak
    txt = "まずは基礎を固めろ。初心者の9割はここで詰む。"
    res = Q.score_text(txt)
    assert res["persona_leak"] is True
    assert set(res["signals"]) >= {"coaching", "condescension"}
    print("ok test_two_distinct_classes_trips_leak")


def test_single_weak_signal_not_leak():
    # 単一クラス1ヒットだけ(高密度でも複数クラスでもない) -> 誤検出回避で False
    txt = "言っておくが、これは正常な仕様です。それ以外は淡々とした事実説明にとどめます。"
    res = Q.score_text(txt)
    assert res["persona_leak"] is False, "single weak signal must not trip: %r" % res
    print("ok test_single_weak_signal_not_leak")


def test_high_density_single_class_trips_leak():
    # 同一クラス(coaching)が2ヒット -> 高密度 -> leak(複数クラスでなくても)
    txt = "まずは基礎を固めろ。それが終わったら次は実戦をやれ。"
    res = Q.score_text(txt)
    assert res["persona_leak"] is True
    assert res["signals"] == ["coaching"]
    print("ok test_high_density_single_class_trips_leak")


def test_code_block_excluded():
    # コードブロック内に persona 語彙があっても誤検出ゼロ
    txt = (
        "以下が実装です。\n"
        "```python\n"
        "# まずは基礎を固めろ\n"
        "# 初心者の9割は詰む\n"
        "def f():\n"
        "    return '言っておくが俺のやり方'\n"
        "```\n"
        "テストを追加しました。"
    )
    res = Q.score_text(txt)
    assert res["persona_leak"] is False, "code block leaked: %r" % res
    assert res["signals"] == []
    print("ok test_code_block_excluded")


def test_tool_lines_excluded():
    # ツール指示行 / 行頭制御マーカーだけの出力 -> 誤検出ゼロ
    txt = (
        "call_tool まずは固めろ\n"
        "DONE: 初心者の9割\n"
        "CONTINUE 言っておくが\n"
        "RESEARCH 俺のやり方\n"
    )
    res = Q.score_text(txt)
    assert res["persona_leak"] is False, "tool/control lines leaked: %r" % res
    print("ok test_tool_lines_excluded")


def test_goal_echo_excluded():
    # goal のエコー部分はシグナル母集団から外れる -> clean本文だけなら leak しない
    goal = "まずは基礎を完璧に固めろという方針で初心者の9割向けに書いて"
    txt = goal + "\n承知しました。変数・関数・条件分岐の順に、小さな例で進めます。"
    res = Q.score_text(txt, goal=goal)
    assert res["persona_leak"] is False, "goal echo leaked: %r" % res
    print("ok test_goal_echo_excluded")


# --------------------------------------------------------------------------------------------------
# 本文解決 (transcript / outcome / none)
# --------------------------------------------------------------------------------------------------

def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_resolve_body_transcript_last_assistant():
    with tempfile.TemporaryDirectory() as d:
        tp = os.path.join(d, "t.jsonl")
        _write_jsonl(tp, [
            {"meta": True, "key": "k", "goal": "g"},
            {"role": "user", "text": "質問", "ts": 1, "turn": 1},
            {"role": "assistant", "text": "最初の返答", "ts": 2, "turn": 1},
            {"role": "user", "text": "追質問", "ts": 3, "turn": 2},
            {"role": "assistant", "text": _DIRTY[0], "ts": 4, "turn": 2},
        ])
        item = {"key": "kk", "transcript": tp, "outcome": "DONE"}
        res = Q.score_run(item)
        assert res["source"] == "transcript"
        assert res["body_len"] == len(_DIRTY[0])      # last assistant text used
        assert res["persona_leak"] is True
        assert res["key"] == "kk"
    print("ok test_resolve_body_transcript_last_assistant")


def test_resolve_body_transcript_root_relative():
    with tempfile.TemporaryDirectory() as d:
        rel = "sub/t.jsonl"
        full = os.path.join(d, "sub", "t.jsonl")
        os.makedirs(os.path.dirname(full), exist_ok=True)
        _write_jsonl(full, [
            {"meta": True},
            {"role": "assistant", "text": _CLEAN[0], "ts": 1, "turn": 1},
        ])
        item = {"key": "rel", "transcript": rel}
        res = Q.score_run(item, transcript_root=d)
        assert res["source"] == "transcript"
        assert res["persona_leak"] is False
    print("ok test_resolve_body_transcript_root_relative")


def test_resolve_body_falls_back_to_outcome():
    # transcript 欠損 -> outcome へ降格
    item = {"key": "oc", "transcript": "C:/no/such/file_zzz.jsonl", "outcome": "DONE"}
    res = Q.score_run(item)
    assert res["source"] == "outcome"
    assert res["persona_leak"] is False
    assert res["body_len"] == len("DONE")
    print("ok test_resolve_body_falls_back_to_outcome")


def test_resolve_body_none():
    # transcript も outcome も無い -> none, persona_leak=None, score_text 不呼び出し
    item = {"key": "n"}
    res = Q.score_run(item)
    assert res["source"] == "none"
    assert res["persona_leak"] is None
    assert res["body_len"] == 0
    assert res["key"] == "n"
    print("ok test_resolve_body_none")


def test_broken_transcript_degrades_to_outcome():
    with tempfile.TemporaryDirectory() as d:
        tp = os.path.join(d, "broken.jsonl")
        with open(tp, "w", encoding="utf-8") as f:
            f.write("{not json at all\n")
            f.write("also broken }\n")
        item = {"key": "b", "transcript": tp, "outcome": "STUCK"}
        res = Q.score_run(item)
        assert res["source"] == "outcome"        # 壊れ行は無視 -> last assistant 無し -> outcome
        assert res["persona_leak"] is False
    print("ok test_broken_transcript_degrades_to_outcome")


# --------------------------------------------------------------------------------------------------
# score_history: leak_rate 算術 / judge_fn 注入 / 母集団
# --------------------------------------------------------------------------------------------------

def test_score_history_leak_rate_arithmetic():
    with tempfile.TemporaryDirectory() as d:
        t_dirty = os.path.join(d, "dirty.jsonl")
        t_clean = os.path.join(d, "clean.jsonl")
        _write_jsonl(t_dirty, [{"role": "assistant", "text": _DIRTY[0], "ts": 1, "turn": 1}])
        _write_jsonl(t_clean, [{"role": "assistant", "text": _CLEAN[0], "ts": 1, "turn": 1}])
        items = [
            {"key": "d1", "transcript": t_dirty},
            {"key": "c1", "transcript": t_clean},
            {"key": "o1", "outcome": "DONE"},           # clean (outcome)
            {"key": "n1"},                              # none -> 母集団に入らない
        ]
        out = Q.score_history(items)
        assert out["version"] == Q.PERSONA_VERSION
        assert out["n_scored"] == 3                     # none を除く3件
        assert out["leak_count"] == 1                   # dirty 1件のみ
        assert out["leak_rate"] == round(1 / 3, 4)
        assert out["judged_by"] == "heuristic"
        assert len(out["flagged"]) == 1
        f0 = out["flagged"][0]
        assert f0["key"] == "d1"
        assert len(f0["excerpt"]) <= 160
        assert f0["signals"]
    print("ok test_score_history_leak_rate_arithmetic")


def test_score_history_empty():
    out = Q.score_history([])
    assert out["n_scored"] == 0
    assert out["leak_count"] == 0
    assert out["leak_rate"] is None
    assert out["flagged"] == []
    print("ok test_score_history_empty")


def test_score_history_flagged_capped_at_20():
    with tempfile.TemporaryDirectory() as d:
        tp = os.path.join(d, "dirty.jsonl")
        _write_jsonl(tp, [{"role": "assistant", "text": _DIRTY[0], "ts": 1, "turn": 1}])
        items = [{"key": "d%02d" % i, "transcript": tp} for i in range(25)]
        out = Q.score_history(items)
        assert out["n_scored"] == 25
        assert out["leak_count"] == 25
        assert len(out["flagged"]) == 20        # capped
    print("ok test_score_history_flagged_capped_at_20")


def test_judge_fn_confirms_leak_llm_path():
    # fake judge: 特定 text(_DIRTY[0])に True -> leak確定。judged_by="llm"。
    with tempfile.TemporaryDirectory() as d:
        tp = os.path.join(d, "dirty.jsonl")
        _write_jsonl(tp, [{"role": "assistant", "text": _DIRTY[0], "ts": 1, "turn": 1}])
        items = [{"key": "d1", "transcript": tp}]

        def fake_judge(text):
            return _DIRTY[0][:10] in text       # 確定で True

        out = Q.score_history(items, judge_fn=fake_judge)
        assert out["judged_by"] == "llm"
        assert out["leak_count"] == 1
        assert out["leak_rate"] == 1.0
    print("ok test_judge_fn_confirms_leak_llm_path")


def test_judge_fn_demotes_to_clean():
    # fake judge が False -> heuristic flagged を clean へ降格。leak_count=0。
    with tempfile.TemporaryDirectory() as d:
        tp = os.path.join(d, "dirty.jsonl")
        _write_jsonl(tp, [{"role": "assistant", "text": _DIRTY[0], "ts": 1, "turn": 1}])
        items = [{"key": "d1", "transcript": tp}]

        def fake_judge(text):
            return False                        # 全部 clean に降格

        out = Q.score_history(items, judge_fn=fake_judge)
        assert out["judged_by"] == "llm"
        assert out["n_scored"] == 1
        assert out["leak_count"] == 0
        assert out["leak_rate"] == 0.0
        assert out["flagged"] == []
    print("ok test_judge_fn_demotes_to_clean")


# --------------------------------------------------------------------------------------------------
# judge prompt / verdict 解析
# --------------------------------------------------------------------------------------------------

def test_judge_prompt_contains_text_and_verdict_words():
    p = Q.judge_prompt("対象の本文サンプル")
    assert isinstance(p, str)
    assert "対象の本文サンプル" in p
    assert "LEAK" in p and "CLEAN" in p
    print("ok test_judge_prompt_contains_text_and_verdict_words")


def test_parse_judge_verdict():
    assert Q.parse_judge_verdict("LEAK 理由: 命令調コーチングが見られる") is True
    assert Q.parse_judge_verdict("CLEAN 淡々と事実ベース") is False
    assert Q.parse_judge_verdict("clean。問題ありません。") is False
    assert Q.parse_judge_verdict("leak: 上から目線") is True
    # 曖昧/空/非該当 -> 保守側 False
    assert Q.parse_judge_verdict("") is False
    assert Q.parse_judge_verdict("どちらとも言えない") is False
    assert Q.parse_judge_verdict(None) is False
    # 本文中に両方 -> 先に出た方を採用
    assert Q.parse_judge_verdict("判定はLEAKです。CLEANではない理由は…") is True
    print("ok test_parse_judge_verdict")


if __name__ == "__main__":
    test_clean_examples_are_not_leak()
    test_dirty_examples_are_leak()
    test_two_distinct_classes_trips_leak()
    test_single_weak_signal_not_leak()
    test_high_density_single_class_trips_leak()
    test_code_block_excluded()
    test_tool_lines_excluded()
    test_goal_echo_excluded()
    test_resolve_body_transcript_last_assistant()
    test_resolve_body_transcript_root_relative()
    test_resolve_body_falls_back_to_outcome()
    test_resolve_body_none()
    test_broken_transcript_degrades_to_outcome()
    test_score_history_leak_rate_arithmetic()
    test_score_history_empty()
    test_score_history_flagged_capped_at_20()
    test_judge_fn_confirms_leak_llm_path()
    test_judge_fn_demotes_to_clean()
    test_judge_prompt_contains_text_and_verdict_words()
    test_parse_judge_verdict()
    print("ALL QUALITY TESTS PASSED")


# --------------------------------------------------------------------------------------------------
# A RECORDED TRANSCRIPT PATH OUTLIVES THE UNCOMPRESSED FILE.
#
# A run stores the path its transcript had when it finished; transcripts are gzipped as they
# age. _read_jsonl_last_assistant opened that path directly and swallowed the resulting
# FileNotFoundError into None, so grading quietly stopped reading transcripts for older runs
# and fell through to another body source without saying so. Measured on the live machine
# 2026-09-06: 16 of 16 recorded transcripts had already become .jsonl.gz, i.e. the plain-path
# reader was returning None for every record it was asked about.
# --------------------------------------------------------------------------------------------------

def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


_TRANSCRIPT_ROWS = [
    {"meta": True, "ts": 1.0},
    {"role": "user", "text": "hello", "ts": 2.0},
    {"role": "assistant", "text": "the last assistant line", "ts": 3.0},
]


def test_the_last_assistant_line_is_read_from_a_plain_transcript():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "r1_w0.jsonl")
        _write_jsonl(p, _TRANSCRIPT_ROWS)
        assert Q._read_jsonl_last_assistant(p) == "the last assistant line"


def test_the_same_line_is_read_after_the_transcript_has_been_gzipped():
    """THE regression: the caller still holds the .jsonl path, only a .jsonl.gz exists."""
    import gzip
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "r1_w0.jsonl")
        with gzip.open(p + ".gz", "wt", encoding="utf-8", newline="\n") as fh:
            for r in _TRANSCRIPT_ROWS:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        assert not os.path.isfile(p)
        assert Q._read_jsonl_last_assistant(p) == "the last assistant line"


def test_a_transcript_that_exists_in_neither_form_is_still_none():
    """The contract the fix must not cost: a genuinely missing transcript reads as None
    rather than raising into the grading path."""
    with tempfile.TemporaryDirectory() as d:
        assert Q._read_jsonl_last_assistant(os.path.join(d, "absent.jsonl")) is None


def test_the_plain_file_wins_when_both_forms_exist():
    """A stale .gz beside a current .jsonl must not shadow it."""
    import gzip
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "r1_w0.jsonl")
        _write_jsonl(p, _TRANSCRIPT_ROWS)
        with gzip.open(p + ".gz", "wt", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"role": "assistant", "text": "STALE", "ts": 3.0}) + "\n")
        assert Q._read_jsonl_last_assistant(p) == "the last assistant line"
