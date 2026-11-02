# -*- coding: utf-8 -*-
"""Tests for the plain-Japanese sentence generator (tools/sec_report.py).

The generator must: (1) emit one block per factor group in the exact requested form
(「AAA では、... のため BBB は言えない。一方で、CCC は ...」), (2) build every clause from a
number on the outcome or a reason code mapped through REASON_PHRASES -- never free-invented,
(3) surface an unmapped reason code verbatim rather than dropping it, (4) carry the concrete
numbers the owner asked for: effect, interval, n(=K), delta_min, family m. Pure: no engine,
no numpy, stdlib only.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys

from tools.screen_estimate_confirm import ConfirmOutcome
from tools import sec_report
from tools.sec_report import (
    REASON_PHRASES,
    cause_clause,
    confirmed_clause,
    render_report,
    sentence_for,
    main,
)


def _adopt(factor="A_true"):
    return ConfirmOutcome(
        factor=factor, screened_effect=0.95, confirmed_effect=0.88,
        verdict="GO", reasons=["A_OK", "B_OK", "C_OK", "D_OK"], claim_supported=True,
        finding="EFFECT", confirmed_ci=[0.44, 1.32], screened_ci=[0.50, 1.40],
        n_lots=20, delta_min=0.3, family_m=3, group_separated=True)


def _straddle(factor="B_confound"):
    return ConfirmOutcome(
        factor=factor, screened_effect=0.12, confirmed_effect=0.05,
        verdict="HOLD", reasons=["C_FAIL_STRADDLE"], claim_supported=False,
        finding=None, confirmed_ci=[-0.4, 0.5], screened_ci=[-0.3, 0.4],
        n_lots=20, delta_min=0.3, family_m=3, group_separated=True)


def _posthoc(factor="A_true"):
    return ConfirmOutcome(
        factor=factor, screened_effect=0.9, confirmed_effect=0.85,
        verdict="HOLD", reasons=["D_FAIL_LOCK_AFTER_DATA"], claim_supported=False,
        finding="EFFECT", confirmed_ci=[0.5, 1.2], screened_ci=[0.5, 1.3],
        n_lots=18, delta_min=0.3, family_m=3, group_separated=False)


def _sparse(factor="C_null"):
    return ConfirmOutcome(
        factor=factor, screened_effect=None, confirmed_effect=None,
        verdict="HOLD", reasons=["C_NOT_COMPUTABLE"], claim_supported=False,
        finding=None, confirmed_ci=None, screened_ci=None,
        n_lots=2, delta_min=0.3, family_m=3, group_separated=False)


# ----------------------------------------------------------------- shape

def test_block_has_the_exact_two_part_form():
    s = sentence_for(_straddle())
    assert s.startswith("B_confound では、")
    assert "のため" in s
    assert "は言えない。" in s
    assert "一方で、" in s
    # exactly one 'cannot' clause and one 'can' clause -> one sentence break '一方で'
    assert s.count("一方で、") == 1


def test_adopt_block_keeps_two_parts_and_states_confirmed_numbers():
    s = sentence_for(_adopt())
    assert "一方で、" in s
    # confirmed effect, interval, K, m, delta_min all present
    assert "0.88" in s and "[0.44, 1.32]" in s
    assert "K=20" in s and "m=3" in s and "0.30" in s
    assert "採用できた" in s
    assert "交絡を層別で分離" in s  # group_separated=True surfaced


# ----------------------------------------------------------------- traceability

def test_cause_uses_mapped_phrase_and_the_numbers():
    o = _straddle()
    cause = cause_clause(o)
    assert REASON_PHRASES["C_FAIL_STRADDLE"] in cause
    # the exact numbers from the outcome, nothing else invented
    assert "効果量 0.05" in cause
    assert "[-0.40, 0.50]" in cause
    assert "K=20" in cause
    assert "delta_min=0.30" in cause
    # the confound separation is named in the cause (group_separated=True on this outcome)
    assert "交絡群は層別で分離済み" in cause


def test_posthoc_names_lock_order_cause():
    o = _posthoc()
    s = sentence_for(o)
    assert REASON_PHRASES["D_FAIL_LOCK_AFTER_DATA"] in s
    assert "事後登録" in s
    # not adopted -> the 'can' clause reports the screen numbers and that adopt was not reached
    assert "採用(adopt)に達していない" in s


def test_sparse_reports_not_computable_and_no_fake_interval():
    o = _sparse()
    s = sentence_for(o)
    assert REASON_PHRASES["C_NOT_COMPUTABLE"] in s
    assert "K=2" in s
    # missing numbers render as fixed placeholders, never fabricated
    assert "区間なし" in s or "該当なし" in s


def test_unmapped_reason_code_is_surfaced_verbatim_not_dropped():
    o = _straddle()
    o.reasons = ["Z_FAIL_SOMETHING_NEW"]
    cause = cause_clause(o)
    assert "理由コード Z_FAIL_SOMETHING_NEW" in cause


def test_ok_codes_contribute_no_cause_phrase():
    o = _adopt()
    cause = cause_clause(o)
    # OK codes map to empty -> no stray 'A_OK' text, but the numeric context remains
    for code in ("A_OK", "B_OK", "C_OK", "D_OK"):
        assert code not in cause
    assert "効果量" in cause


def test_every_number_in_sentence_exists_on_the_outcome():
    """No number appears in the sentence that is not one of the outcome's own values."""
    o = _straddle()
    s = sentence_for(o)
    allowed = {"0.05", "0.12", "-0.40", "0.50", "-0.30", "0.40", "20", "0.30", "3"}
    import re
    nums = set(re.findall(r"-?\d+\.\d+|(?<![\d.])\d+(?![\d.])", s))
    # strip the standalone digits that are part of factor/label tokens by checking membership
    stray = {n for n in nums if n not in allowed}
    assert not stray, f"sentence contains numbers not on the outcome: {stray}"


# ----------------------------------------------------------------- multiple blocks

def test_render_report_one_block_per_group_in_order():
    outcomes = [_adopt("A_true"), _straddle("B_confound"), _sparse("C_null")]
    rep = render_report(outcomes)
    blocks = rep.split("\n\n")
    assert len(blocks) == 3
    assert blocks[0].startswith("A_true では、")
    assert blocks[1].startswith("B_confound では、")
    assert blocks[2].startswith("C_null では、")


# ----------------------------------------------------------------- dict round-trip + CLI

def test_accepts_plain_dicts_from_json():
    import dataclasses
    d = dataclasses.asdict(_straddle())
    s = sentence_for(d)
    assert s.startswith("B_confound では、")


def test_cli_reads_json_and_prints_blocks(tmp_path, capsys):
    import dataclasses
    outcomes = [dataclasses.asdict(_adopt("A_true")), dataclasses.asdict(_straddle("B_confound"))]
    p = tmp_path / "results.json"
    p.write_text(json.dumps(outcomes, ensure_ascii=False), encoding="utf-8")
    rc = main([str(p)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "A_true では、" in out and "B_confound では、" in out
    assert out.count("一方で、") == 2


def test_cli_reads_stdin(monkeypatch, capsys):
    import dataclasses
    payload = json.dumps([dataclasses.asdict(_posthoc("A_true"))], ensure_ascii=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    rc = main(["--stdin"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "A_true では、" in out and "事後登録" in out
