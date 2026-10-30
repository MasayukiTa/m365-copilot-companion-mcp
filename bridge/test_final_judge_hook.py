# -*- coding: utf-8 -*-
"""bridge/final_judge_hook.py の試験（TDD: 実装前はすべて失敗する）。

このモジュールは「データ解析系の仕事は、最終回答を出す直前に判定エンジン
(validity_judge)を必ず通す」ための配線だけを持つ。統計手法そのものや生データは
一切含めない（手法本体はこのリポジトリの外。方針書 docs/research/validity_tool_
policy_20261005.md）。judge の呼び出しは注入関数に委ね、CI は実エンジンに依存しない。

検証する不変条件:
  1. is_data_analysis: 解析系(ADAMS/MCL 等の DB 照会→表/相関/群間比較)を判別する。
     誤判定の境界も固定する。
  2. finalize: 解析系なら manifest に (i)結果とデータのハッシュ→(ii)主張の登録ロック
     の順(seq)で記録し、(iii)judge を通して verdict/reason_code/interval/n を添付する。
  3. judge が使えないときは黙って省略せず judge_error を載せる。
  4. refuter_check: 「判定器を通したか、通していなければなぜか」を必ず検査する。
  5. 生データ行・ID を manifest 本文に書かない(ハッシュと件数のみ)。

Windows 専用の記述を入れない(CI は Linux)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge import final_judge_hook as H  # noqa: E402


def _task(kind="group_compare", claim_id="MCL_P2", data_source="MCL",
          rows=None, claim=None):
    return H.AnalysisTask(
        claim_id=claim_id,
        kind=kind,
        data_source=data_source,
        row_count=(len(rows) if rows is not None else 4),
        rows_fingerprint="sha256:" + "0" * 8,
        claim=claim if claim is not None else {"summary": "treat>control"},
    )


# --- 1. 判別条件 ---------------------------------------------------------

@pytest.mark.parametrize("kind", ["group_compare", "correlation", "table"])
def test_is_data_analysis_true_for_db_backed_analysis(kind):
    assert H.is_data_analysis(_task(kind=kind, data_source="ADAMS")) is True


@pytest.mark.parametrize("kind", ["chitchat", "summary", "doc_edit", "", "unknown"])
def test_is_data_analysis_false_for_non_analysis_kind(kind):
    assert H.is_data_analysis(_task(kind=kind, data_source="ADAMS")) is False


def test_is_data_analysis_false_without_data_binding():
    # 解析系の kind でも、DB 照会もデータ行も無い純粋な文章整形は非解析扱い
    t = _task(kind="table", data_source=None, rows=[])
    assert H.is_data_analysis(t) is False


# --- 2/3/5. finalize ------------------------------------------------------

def test_finalize_attaches_verdict_for_analysis(tmp_path):
    def fake_judge(task):
        return {"verdict": "SUPPORTED", "reason_code": "R12",
                "interval": {"lo": 1.2, "hi": 3.4}, "n": 8,
                "engine_version": "validity_judge"}

    mani = tmp_path / "m.jsonl"
    out = H.finalize(_task(data_source="ADAMS"), manifest_path=mani,
                     judge_fn=fake_judge)
    j = out["judge"]
    assert out["is_analysis"] is True
    assert j["verdict"] == "SUPPORTED"
    assert j["reason_code"] == "R12"
    assert j["interval"]["lo"] == 1.2
    assert j["n"] == 8
    assert out["judge_error"] is None


def test_finalize_records_hash_then_lock_in_order(tmp_path):
    mani = tmp_path / "m.jsonl"
    H.finalize(_task(data_source="MCL"), manifest_path=mani,
               judge_fn=lambda t: {"verdict": "INSUFFICIENT", "reason_code": "R10",
                                   "interval": None, "n": 2,
                                   "engine_version": "validity_judge"})
    lines = [json.loads(x) for x in mani.read_text(encoding="utf-8").splitlines()]
    types = [e["type"] for e in lines]
    assert "hash" in types and "lock" in types
    assert types.index("hash") < types.index("lock")
    seqs = [e["seq"] for e in lines if "seq" in e]
    assert seqs == sorted(seqs)


def test_finalize_reports_judge_error_not_silent(tmp_path):
    def boom(task):
        raise RuntimeError("validity_judge 不在")

    mani = tmp_path / "m.jsonl"
    out = H.finalize(_task(data_source="ADAMS"), manifest_path=mani, judge_fn=boom)
    assert out["judge"] is None
    assert out["judge_error"]
    assert "validity_judge 不在" in out["judge_error"]


def test_finalize_skips_judge_for_non_analysis(tmp_path):
    called = {"v": False}

    def spy(task):
        called["v"] = True
        return {}

    mani = tmp_path / "m.jsonl"
    out = H.finalize(_task(kind="summary"), manifest_path=mani, judge_fn=spy)
    assert called["v"] is False
    assert out["judge"] is None
    assert out["skipped_reason"] == "not_data_analysis"


def test_manifest_has_no_raw_rows_or_ids(tmp_path):
    # manifest 本文には生データ行・ID を書かない(ハッシュと件数のみ)
    mani = tmp_path / "m.jsonl"
    secret = "EMP-SECRET-0007"
    t = _task(data_source="MCL")
    t.claim = {"summary": "ok"}  # 生 ID は claim に入れない運用
    H.finalize(t, manifest_path=mani,
               judge_fn=lambda x: {"verdict": "SUPPORTED", "reason_code": "R12",
                                   "interval": None, "n": 4,
                                   "engine_version": "validity_judge"})
    body = mani.read_text(encoding="utf-8")
    assert secret not in body
    # row_count(件数)は載るが、行そのものは載らない
    assert "row_count" in body


# --- 4. refuter -----------------------------------------------------------

def test_refuter_passes_when_judge_present():
    out = {"is_analysis": True, "judge": {"verdict": "SUPPORTED"},
           "judge_error": None, "skipped_reason": None}
    assert H.refuter_check(out) == []


def test_refuter_passes_when_skip_reason_present():
    out = {"is_analysis": False, "judge": None, "judge_error": None,
           "skipped_reason": "not_data_analysis"}
    assert H.refuter_check(out) == []


def test_refuter_passes_when_judge_error_present():
    out = {"is_analysis": True, "judge": None,
           "judge_error": "判定器を通せませんでした: X", "skipped_reason": None}
    assert H.refuter_check(out) == []


def test_refuter_flags_analysis_without_judge_and_without_reason():
    out = {"is_analysis": True, "judge": None, "judge_error": None,
           "skipped_reason": None}
    issues = H.refuter_check(out)
    assert any("判定器" in s for s in issues)
