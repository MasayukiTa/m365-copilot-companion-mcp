"""解析側が、モジュールの拒否を要約して消してしまわないこと。

frontier は「候補が足りなければ描かない」を明示的に選んでいる。
その拒否を握りつぶした瞬間、3事象の上に引いた線が結論の顔をして出てくる。
"""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relay.selfimprove import reviewer_allocation as A
from scripts.analyze_lens_corpus import (describe, load, measured_lens_cost,
                                         run, split, warm_memory)

LENSES = ("correctness", "edge", "security")


def _row(i, functional, security, verdicts, cal=False):
    return {"candidate_id": "c%d" % i,
            "bad": {"functional": functional, "security": security},
            "verdicts": dict(zip(LENSES, verdicts)),
            "features": {"kind": "code"}, "calibration": cal}


def _write(tmp_path, rows):
    p = tmp_path / "corpus.jsonl"
    io.open(p, "w", encoding="utf-8", newline=chr(10)).write(
        chr(10).join(json.dumps(r, ensure_ascii=False) for r in rows) + chr(10))
    return p


def test_a_thin_corpus_produces_a_refusal_and_no_frontier(tmp_path, capsys):
    rows = [_row(i, False, A.SECURITY_PASS, (A.REFUTED, A.UPHELD, A.UPHELD))
            for i in range(3)]
    got = run(load(_write(tmp_path, rows)), k=2)
    assert got["frontier"]["frontier"] == []
    assert "collect more" in got["frontier"]["note"]


def test_the_shape_is_reported_before_any_policy_is_scored(tmp_path):
    rows = ([_row(i, False, A.SECURITY_PASS, (A.REFUTED,) * 3) for i in range(4)]
            + [_row(90 + i, True, A.SECURITY_VIOLATION, (A.UPHELD,) * 3, cal=True)
               for i in range(2)]
            + [_row(80 + i, True, A.SECURITY_UNEVALUABLE, (A.UPHELD,) * 3) for i in range(3)])
    shape = describe(load(_write(tmp_path, rows)))
    assert shape["functional_bad"] == 4
    assert shape["security_bad"] == 2
    assert shape["security_unevaluable"] == 3
    assert shape["calibration_rows"] == 2
    # 種をまいた2件はどのレンズも捕捉していないので catchable ではない
    assert shape["catchable_bad"] == 4


def test_a_policy_that_could_not_be_scored_is_named_not_dropped(tmp_path, capsys):
    """空メモリの adaptive は拒否される。黙って落とすと、腕が1本欠けた frontier が
    完全な顔をして出る。"""
    rows = [_row(i, False, A.SECURITY_PASS, (A.REFUTED, A.UPHELD, A.UPHELD))
            for i in range(8)]
    run(load(_write(tmp_path, rows)), k=2)
    out = capsys.readouterr().out
    assert "adaptive" in out and "REFUSED" in out


def test_an_empty_corpus_is_not_reported_as_a_clean_run(tmp_path, capsys):
    from scripts.analyze_lens_corpus import main
    p = tmp_path / "empty.jsonl"
    io.open(p, "w", encoding="utf-8").write("")
    sys.argv = ["analyze", str(p)]
    assert main() == 2
    assert "skips every candidate" in capsys.readouterr().out


def test_seeded_rows_are_reported_separately_from_the_real_ones(tmp_path, capsys):
    from scripts.analyze_lens_corpus import main
    rows = ([_row(i, False, A.SECURITY_PASS, (A.REFUTED, A.UPHELD, A.UPHELD))
             for i in range(8)]
            + [_row(90 + i, True, A.SECURITY_VIOLATION, (A.UPHELD, A.UPHELD, A.REFUTED),
                    cal=True) for i in range(4)])
    sys.argv = ["analyze", str(_write(tmp_path, rows))]
    assert main() == 0
    out = capsys.readouterr().out
    assert "seeded included" in out and "real candidates only" in out, (
        "両方の見方を出さないと、セキュリティ比較が種のみに乗っていることが隠れる")


# ---- 実測コストと保持データ ---------------------------------------------------------------------

def _timed(i, functional, security, verdicts, secs):
    r = _row(i, functional, security, verdicts)
    r["lens_detail"] = {ln: {"verdict": v, "reason": "", "elapsed_s": secs[ln]}
                        for ln, v in zip(LENSES, verdicts)}
    return r


def test_lens_cost_is_measured_and_timed_out_lenses_are_excluded():
    """タイムアウトしたレンズの所要はタイムアウト値であって、レンズの値ではない。
    混ぜると、黙ったレンズほど『高価』になり、frontier がそれを避け始める。"""
    fast = {"correctness": 30.0, "edge": 40.0, "security": 50.0}
    slow = {"correctness": 420.0, "edge": 420.0, "security": 420.0}
    rows = [_timed(1, False, A.SECURITY_PASS, (A.REFUTED, A.UPHELD, A.UPHELD), fast),
            _timed(2, False, A.SECURITY_PASS, (A.REFUTED, A.UPHELD, A.UPHELD), fast),
            _timed(3, True, A.SECURITY_PASS, (A.UNCLEAR, A.UNCLEAR, A.UNCLEAR), slow)]
    cost = measured_lens_cost(rows)
    assert cost == fast, cost


def test_no_timings_is_reported_as_a_modelling_choice_not_a_measurement():
    assert measured_lens_cost([_row(1, False, A.SECURITY_PASS, (A.REFUTED,) * 3)]) is None


def test_the_split_is_stratified_so_the_scarce_bad_rows_reach_both_halves(tmp_path):
    """id 順に切ると、実際に不良候補が全部片側へ寄った。保持側の frontier は
    そのせいだけで拒否になり、分割が結論を決めていた。"""
    rows = ([_row(i, False, A.SECURITY_PASS, (A.REFUTED, A.UPHELD, A.UPHELD))
             for i in range(10)]
            + [_row(100 + i, True, A.SECURITY_PASS, (A.UPHELD,) * 3) for i in range(10)])
    train, test = split(rows)
    def bad(rs):
        return sum(1 for r in rs if not A._truth(r)[0])
    assert bad(train) > 0 and bad(test) > 0, (bad(train), bad(test))
    assert abs(bad(train) - bad(test)) <= 1


def test_warming_twice_does_not_double_the_observations(tmp_path):
    """RefuterMemory は追記する。同じパスへ2回温めると観測数が倍になり、
    adaptive は『解析を再実行した回数』だけ強く見える。"""
    rows = [_row(i, False, A.SECURITY_PASS, (A.REFUTED, A.UPHELD, A.UPHELD))
            for i in range(6)]
    path = tmp_path / "mem.json"
    _m1, first = warm_memory(rows, path)
    _m2, second = warm_memory(rows, path)
    assert first == second, (first, second)


def test_the_held_out_view_does_not_print_a_caveat_that_is_false_for_it(tmp_path, capsys):
    rows = [_row(i, False, A.SECURITY_PASS, (A.REFUTED, A.UPHELD, A.UPHELD))
            for i in range(12)]
    memory, _ = warm_memory(rows[:6], tmp_path / "m.json")
    run(rows[6:], k=2, memory=memory, held_out=True)
    out = capsys.readouterr().out
    assert "does not support -- train/test separation" not in out
    assert "DOES support -- train/test separation" in out
    assert "still does not support -- generalisation" in out
