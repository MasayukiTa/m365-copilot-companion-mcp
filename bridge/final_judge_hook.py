# -*- coding: utf-8 -*-
"""データ解析系の仕事を、最終回答の直前に判定エンジン(validity_judge)へ必ず通す配線。

背景(オーナー指示 2026-10-27): 「データ解析系は、すべて最終でこの判定器を通すハーネスを
入れる」。2026-10-30 の再実験で、解析のあと判定器が自動で呼ばれない事象が確認されたため、
解析の最終段に強制フックを入れる。

このモジュールが持つのは「配線」だけである。統計手法そのもの・閾値・区間の計算・生データは
一切含めない(手法本体はこのリポジトリの外。方針書 docs/research/validity_tool_policy_
20261005.md、および .git/info/exclude の tools/auto/validity_*.py を参照)。judge の実呼び出しは
`judge_fn` に注入する。既定では tools/auto 側の薄いラッパー(非公開)に委譲するが、本モジュール
自体はラッパーが無くても import でき、CI は実エンジンに依存しない。

規則・閾値・既存の登録主張は一切変更しない。manifest 本文には判定・理由コード・ハッシュ・
件数だけを残し、生データ行や社員 ID は書かない。

Windows 専用の記述は持たない(CI は Linux)。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# ハーネスが「データ解析系」と判別する kind。
#   group_compare: 群間比較 / correlation: 相関 / table: 集計表
ANALYSIS_KINDS = frozenset({"group_compare", "correlation", "table"})


@dataclass
class AnalysisTask:
    """解析の最終結果を表す、judge へ渡すための最小の記述。

    生データ行は持たない。代わりに件数(row_count)と指紋(rows_fingerprint, 既に計算済みの
    ハッシュ文字列)だけを持ち、manifest にはそれだけを残す。

    data_source: ADAMS / MCL などの DB 名。None かつ row_count==0 なら、DB 照会にもデータにも
      紐づかない文章整形とみなし非解析扱い。
    claim: 登録・ロックする主張(判定・集計の要点)。生 ID を入れない運用。
    judge_input: judge への入力(省略時は claim_id/kind/n から最小構成を組む)。
    """
    claim_id: str
    kind: str
    data_source: Optional[str] = None
    row_count: int = 0
    rows_fingerprint: str = ""
    claim: dict[str, Any] = field(default_factory=dict)
    judge_input: Optional[dict[str, Any]] = None


def _hash_obj(obj: Any) -> str:
    import hashlib
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def is_data_analysis(task: AnalysisTask) -> bool:
    """この仕事がデータ解析系か判別する。

    判別条件(決定):
      (A) kind が ANALYSIS_KINDS(group_compare / correlation / table)である。
      (B) かつ、データに紐づく。すなわち data_source が設定されているか row_count>0。
    両方を満たすものだけを解析系とする。

    誤判定の例と対策:
      - 誤判定1(空回し): 文章要約や定型文編集を kind='table' として渡すと、本来非解析
        なのに判定器を通してしまう。-> 条件(B)で data_source も row_count も無いものを除外
        (test_is_data_analysis_false_without_data_binding)。
      - 誤判定2(素通り): 解析結果を kind='summary' 等で渡すと、通すべき判定器を飛ばす。
        -> kind が ANALYSIS_KINDS 外なら非解析とし、finalize は skipped_reason を残すので
        refuter が検知できる(test_finalize_skips_judge_for_non_analysis と
        test_refuter_flags_analysis_without_judge_and_without_reason)。呼び出し側は解析結果に
        正しい kind を付ける責務を負う。
    """
    if task.kind not in ANALYSIS_KINDS:
        return False
    return bool(task.data_source) or task.row_count > 0


def _append(manifest_path: Path, entry: dict[str, Any]) -> None:
    entry = dict(entry)
    entry.setdefault("ts", time.time())
    manifest_path = Path(manifest_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")


def _default_judge_fn(task: AnalysisTask) -> dict[str, Any]:
    """既定の judge 呼び出し: 非公開ラッパー tools/auto/validity_harness.py に委譲する。

    ラッパー(手法本体の薄い呼び出し口)はこのリポジトリに無いため import は実行時に行い、
    不在なら例外を送出する。finalize はそれを握り、judge_error として最終回答に載せる
    (黙って省略しない)。
    """
    try:
        from tools.auto import validity_harness as _w  # type: ignore
    except Exception as exc:  # ラッパー不在 or 読込失敗
        raise RuntimeError(
            "validity_judge ラッパー(tools/auto/validity_harness.py)を読み込めません: "
            + f"{type(exc).__name__}: {exc}"
        ) from exc
    judge_input = task.judge_input or {
        "config_id": task.claim_id, "kind": task.kind, "n": task.row_count,
    }
    return _w.run_judge(judge_input)


def finalize(
    task: AnalysisTask,
    manifest_path: Optional[Path] = None,
    judge_fn: Optional[Callable[[AnalysisTask], dict[str, Any]]] = None,
) -> dict[str, Any]:
    """解析結果を最終化する直前のフック。

    解析系なら: manifest に (i)結果とデータのハッシュ(type=hash, seq=1) -> (ii)主張の
    登録ロック(type=lock, seq=2) の順で記録し、(iii)judge を通して verdict/reason_code/
    interval/n を結果に添付する(type=judge, seq=3)。judge が失敗したら judge_error を載せる
    (type=judge_error, seq=3)。非解析なら judge を通さず skipped_reason を残す。

    戻り値には is_analysis / judge / judge_error / skipped_reason を必ず含め、refuter が
    「判定器を通したか、通していなければなぜか」を検査できるようにする。
    """
    judge_fn = judge_fn or _default_judge_fn
    analysis = is_data_analysis(task)
    out: dict[str, Any] = {
        "claim_id": task.claim_id,
        "kind": task.kind,
        "is_analysis": analysis,
        "judge": None,
        "judge_error": None,
        "skipped_reason": None,
    }
    if not analysis:
        out["skipped_reason"] = "not_data_analysis"
        return out

    if manifest_path is None:
        manifest_path = Path("validity_manifest.jsonl")
    manifest_path = Path(manifest_path)

    claim_hash = _hash_obj(task.claim)
    # (i) 結果とデータのハッシュを manifest に記録(生行・ID は書かない。件数と指紋のみ)
    _append(manifest_path, {
        "type": "hash", "seq": 1, "claim_id": task.claim_id, "kind": task.kind,
        "data_source": task.data_source, "row_count": task.row_count,
        "rows_fingerprint": task.rows_fingerprint, "claim_hash": claim_hash,
    })
    # (ii) 主張を登録してロック(順序が記録に残る)
    _append(manifest_path, {
        "type": "lock", "seq": 2, "claim_id": task.claim_id, "claim_hash": claim_hash,
    })
    # (iii) 判定エンジンにかける
    try:
        verdict = judge_fn(task)
        out["judge"] = {
            "verdict": verdict.get("verdict"),
            "reason_code": verdict.get("reason_code"),
            "interval": verdict.get("interval"),
            "n": verdict.get("n"),
            "engine_version": verdict.get("engine_version"),
        }
        _append(manifest_path, {
            "type": "judge", "seq": 3, "claim_id": task.claim_id,
            "verdict": out["judge"]["verdict"],
            "reason_code": out["judge"]["reason_code"],
        })
    except Exception as e:
        out["judge_error"] = f"判定器を通せませんでした: {type(e).__name__}: {e}"
        _append(manifest_path, {
            "type": "judge_error", "seq": 3, "claim_id": task.claim_id,
            "error": str(e)[:500],
        })
    return out


def refuter_check(out: dict[str, Any]) -> list[str]:
    """反証役(refuter)の確認項目。

    「判定器を通したか、通していなければなぜか」を必ず確認する。問題があれば指摘文字列の
    リストを返す。問題が無ければ空リスト。

    合格条件:
      - judge が付いている(判定器を通した)、または
      - judge_error が載っている(通せなかった理由が明示されている)、または
      - skipped_reason が載っている(非解析として正しく飛ばした理由が明示されている)。
    """
    issues: list[str] = []
    if out.get("judge"):
        return issues
    if out.get("judge_error") or out.get("skipped_reason"):
        return issues
    # ここに来るのは、判定も理由も無い場合
    if out.get("is_analysis"):
        issues.append(
            "解析系なのに判定器を通しておらず、通していない理由(judge_error / "
            "skipped_reason)も無い"
        )
    else:
        issues.append(
            "非解析だが skipped_reason が無い(なぜ判定器を飛ばしたかが記録されていない)"
        )
    return issues


def refuter_appendix(out: dict[str, Any]) -> str:
    """refuter / 最終回答に載せる、判定器通過状況の定型文(日本語)。

    judge を通したら判定・理由コード・区間・n を、通していなければその理由を必ず書く。
    """
    j = out.get("judge")
    if j:
        iv = j.get("interval")
        iv_s = (f"[{iv.get('lo')}, {iv.get('hi')}]" if isinstance(iv, dict) else "なし")
        return (
            "【判定器(validity_judge)を通した】"
            f"判定={j.get('verdict')} 理由コード={j.get('reason_code')} "
            f"区間={iv_s} n={j.get('n')} (engine={j.get('engine_version')})"
        )
    if out.get("judge_error"):
        return "【判定器を通せなかった】" + str(out["judge_error"])
    if out.get("skipped_reason") == "not_data_analysis":
        return "【判定器は対象外】この仕事はデータ解析系ではないため判定器を通していない。"
    return "【判定器の通過状況が不明】理由が記録されていない(要修正)。"
