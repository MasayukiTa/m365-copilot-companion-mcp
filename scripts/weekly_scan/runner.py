# -*- coding: utf-8 -*-
"""週次走査の共通基盤を結線する薄いオーケストレーション層。

このモジュールは共通基盤（ledger_io / judge_wrapper / db_lots）を実際に呼び出して
「台帳を読む → 再走査対象（B/C/D）を取り出す → C はロット数を数えて規定数の到達を見る →
判定器ラッパに通す → 台帳を更新して保存する」という**基盤の結線だけ**を提供する。

ここに含めないもの（別モジュールの責務であり、本パッケージの範囲外）:
  - 具体的なスクリーニング設計（交絡の検出・品名層別の推定ロジックそのもの）
  - D 分類の再取得の実行や、報告（pptx / md）の生成、提案の作文
走査本体はこの runner を import し、注入点（query_callable / judge_callable / fetch 等）に
実体を渡して組み立てる。本モジュールは判定器・基準・Δmin・補正・順序の信頼時計を
一切変更せず、既存の登録済みのものをそのまま使う。

注入設計:
  - DB アクセスは ``query_callable``（odbc_query 相当）を引数で受け取り、本体を import しない。
  - 判定は ``judge_callable``（validity_judge 相当）を引数で受け取り、本体を import しない。
こうして SECRET 判定エンジンへの依存を持ち込まず、単体試験ではダミーを差せる。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# scripts/ を import パスに入れて weekly_scan パッケージとして読む（CLI 実行時）。
_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from weekly_scan import CLASS_C, RESCAN_CLASSES  # noqa: E402
from weekly_scan.db_lots import LotCountResult, count_lots_by_product  # noqa: E402
from weekly_scan.judge_wrapper import JudgeVerdict, run_judge  # noqa: E402
from weekly_scan.ledger_io import Ledger, LedgerEntry, load_ledger, save_ledger  # noqa: E402


@dataclass
class ScanOutcome:
    """1 エントリを 1 回走査した結果（基盤が観測した事実のみ。判定は書き換えない）。"""

    analysis_id: str
    classification: str
    lots_checked: bool = False
    current_lots: int | None = None
    required_lots: int | None = None
    lots_met: bool | None = None
    judged: bool = False
    verdict: str | None = None
    note: str = ""


@dataclass
class ScanReport:
    """走査 1 回の全体結果（結線が触れたエントリの観測の集合）。"""

    scanned_at: str
    outcomes: list[ScanOutcome] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned_at": self.scanned_at,
            "outcomes": [vars(o) for o in self.outcomes],
        }


def _count_for_entry(
    entry: LedgerEntry,
    query_callable: Callable[..., Any],
) -> LotCountResult | None:
    """C 分類のエントリについて、品名で層別してロット数を数える。

    数えるのに必要な列情報は entry.notes の 'lot_count' に置く約束にする（走査本体が
    台帳に書く）。情報が無ければ数えない（None を返す）。ここは結線だけで、SQL は
    db_lots 側がバインド変数で組む。
    """
    spec = entry.notes.get("lot_count") if isinstance(entry.notes, dict) else None
    if not isinstance(spec, dict):
        return None
    try:
        table = spec["table"]
        unit_column = spec["unit_column"]
        product_column = spec["product_column"]
    except KeyError:
        return None
    return count_lots_by_product(
        query_callable,
        table=table,
        unit_column=unit_column,
        product_column=product_column,
        products=spec.get("products"),
        connection=spec.get("connection", "adams"),
        where_sql=spec.get("where_sql"),
        where_params=spec.get("where_params"),
    )


def scan_entry(
    entry: LedgerEntry,
    query_callable: Callable[..., Any] | None = None,
    judge_callable: Callable[..., Any] | None = None,
    scanned_at: str = "",
) -> ScanOutcome:
    """1 エントリを走査する（基盤の結線）。

    - C 分類: query_callable があればロット数を数え、required_lots への到達を記録する。
      到達していて judge_callable と config_id があれば、登録済み主張を判定器に通す。
    - B/D 分類: config_id と judge_callable があれば判定器に通す（順序の信頼時計の記録は
      既存の登録済みのものをそのまま使う。偽装しない）。
    判定器の verdict は写すだけで、基準・Δmin・補正・順序を一切書き換えない。
    """
    out = ScanOutcome(
        analysis_id=entry.analysis_id,
        classification=entry.classification,
        required_lots=entry.required_lots,
    )

    if entry.classification == CLASS_C and query_callable is not None:
        result = _count_for_entry(entry, query_callable)
        if result is not None:
            out.lots_checked = True
            have = result.total_lots
            out.current_lots = have
            entry.current_lots = have
            if entry.required_lots is not None:
                out.lots_met = have >= entry.required_lots

    should_judge = (
        judge_callable is not None
        and entry.config_id
        and entry.classification in RESCAN_CLASSES
        and (out.lots_met is not False)  # C でまだ不足なら判定しない
    )
    if should_judge:
        verdict: JudgeVerdict = run_judge(entry.config_id, judge_callable)
        out.judged = True
        out.verdict = verdict.verdict
        out.note = "order_ok=%s finding=%s" % (verdict.order_ok, verdict.finding)

    if scanned_at:
        entry.last_scanned = scanned_at
    return out


def scan_ledger(
    ledger: Ledger,
    query_callable: Callable[..., Any] | None = None,
    judge_callable: Callable[..., Any] | None = None,
    scanned_at: str = "",
) -> ScanReport:
    """台帳の再走査対象（B/C/D）を順に走査し、観測を集めて返す。

    台帳そのもの（entry.current_lots / last_scanned）はこの関数内で更新される。保存は
    呼び出し側が save_ledger で行う（run_once が最終的に保存する）。A/E は対象外。
    """
    report = ScanReport(scanned_at=scanned_at)
    for entry in ledger.by_classification(*sorted(RESCAN_CLASSES)):
        report.outcomes.append(
            scan_entry(
                entry,
                query_callable=query_callable,
                judge_callable=judge_callable,
                scanned_at=scanned_at,
            )
        )
    return report


def run_once(
    ledger_path: str,
    query_callable: Callable[..., Any] | None = None,
    judge_callable: Callable[..., Any] | None = None,
    scanned_at: str = "",
    save: bool = True,
) -> ScanReport:
    """台帳を読み、再走査対象を 1 回走査し、更新した台帳を保存して結果を返す。

    注入が無ければ（dry-run）、ロット計数も判定もせず、対象の列挙と last_scanned の
    更新だけを行う。これにより基盤の結線が通ることを実データなしで確かめられる。
    """
    ledger = load_ledger(ledger_path)
    report = scan_ledger(
        ledger,
        query_callable=query_callable,
        judge_callable=judge_callable,
        scanned_at=scanned_at,
    )
    if save:
        save_ledger(ledger_path, ledger)
    return report


def main(argv: list[str] | None = None) -> int:
    """CLI 入口（dry-run）。実データの DB・判定器は注入が要るため、ここでは台帳の
    再走査対象を列挙し、last_scanned を更新して保存するところまでを行う。

    実行例:
        python -m weekly_scan.runner --ledger <台帳.json> --scanned-at 2026-11-04
    """
    ap = argparse.ArgumentParser(description="週次走査の共通基盤 dry-run")
    ap.add_argument("--ledger", required=True, help="台帳.json のパス")
    ap.add_argument("--scanned-at", default="", help="走査日（ISO 8601）")
    ap.add_argument("--no-save", action="store_true", help="台帳を保存しない")
    a = ap.parse_args(argv)
    report = run_once(
        a.ledger,
        scanned_at=a.scanned_at,
        save=not a.no_save,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


__all__ = [
    "ScanOutcome",
    "ScanReport",
    "scan_entry",
    "scan_ledger",
    "run_once",
]


if __name__ == "__main__":
    sys.exit(main())
