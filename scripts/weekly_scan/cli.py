# -*- coding: utf-8 -*-
"""Weekly-scan CLI entry point (runbook が前提とする ``python -m scripts.weekly_scan.cli``)。

このモジュールは、週次走査の四つの処理を一つのコマンドに接続する唯一の人間向け
入口である:

  (a) B/C 確認  / (b) D 再取得       -> :mod:`scripts.weekly_scan.run_bc_d`
  (c) 新規スクリーニング           -> :mod:`scripts.weekly_scan.screening`
  (d) 報告生成・追跡項目追記     -> :mod:`scripts.weekly_scan.report`

判定器・基準・Δmin・補正は変更しない。順序の信頼時計は登録済みのものをそのまま使い、
新規に順序を偽装しない(各処理系と ``common`` が保証)。H: と SharePoint には書かない。

--- 台帳の形 ---
実データの台帳.json は「行の配列」で、各行は ``class`` / ``kshort`` / ``add80`` / ``dir`` /
``ci_std`` 等を持つ(:mod:`run_bc_d` の load_ledger がそのまま読める形)。この CLI は
その行配列をそのまま扱い、走査後に各行の last_scanned / next_trigger / verdict を
書き戻す(ラウンドトリップ)。

--- 試運転 (--dry-run) ---
DB 計数・判定器を no-op プロバイダに差し替え、台帳のラウンドトリップ・走査日更新・
次の契機更新というオーケストレーションのみを検証する。DB も判定器も呼ばないため
 judged=0 / GO=0 が正常。no-op は登録済み順序時計も判定も触らず、偶発的に何かを
「成立」させない(偽装しない)。
"""
from __future__ import annotations

import argparse
import sys
from typing import Any, Mapping, Optional, Sequence

# 共通基盤が import できなければ exit 2 (runbook 契約)。
try:
    from . import run_bc_d
    from .run_bc_d import load_ledger, save_ledger, format_run_log
    from .common import today_utc
    _FOUNDATION_OK = True
except Exception as _exc:  # pragma: no cover - 基盤欠落時のみ
    _FOUNDATION_OK = False
    _FOUNDATION_ERR = _exc


BC_CLASSES = frozenset({"B", "C"})
D_CLASS = "D"


class _NoopLotCounter:
    """DB を呼ばない計数。常に 0 を返す(新ロットは数えていない)ので、
    is_ready_for_confirmation が必ず False になり、判定器まで進まない。"""

    def count_new_lots(self, request: Mapping[str, Any]) -> int:
        return 0


class _NoopDataFetcher:
    def fetch(self, request: Mapping[str, Any]) -> str:
        return ""


class _NoopJudge:
    """試運転では判定器を呼ばない。万一呼ばれたら raise して、no-op のはずが
    判定をまわしたことを表面化する(黙って GO を偽装しない)。"""

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        raise RuntimeError(
            "dry-run judge must not be invoked; readiness gate should have "
            "stopped before the judge"
        )


def _noop_request_loader(row: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """試運転の request_loader。登録済み request を持たない(None)。これにより
    B/C/D の各処理系は 'no_request' 経路で止まり、データ取得・判定に進まない。
    つまり台帳のラウンドトリップ・走査日更新・次の契機更新だけを検証できる。"""
    return None


def _count_classes(rows: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
    bc = sum(1 for r in rows if str(r.get("class", "")) in BC_CLASSES)
    d = sum(1 for r in rows if str(r.get("class", "")) == D_CLASS)
    return bc, d


def _summary_line(
    scanned_on: str,
    *,
    bc_rows: int,
    bc_judged: int,
    bc_go: int,
    d_rows: int,
    d_rejudged: int,
    d_go: int,
    written: bool,
) -> str:
    return (
        "weekly-scan (a)+(b) %s: B/C rows=%d judged=%d GO=%d; "
        "D rows=%d re-judged=%d GO=%d; written=%s."
        % (
            scanned_on, bc_rows, bc_judged, bc_go,
            d_rows, d_rejudged, d_go, "yes" if written else "no",
        )
    )


def _count_outcomes(outcomes) -> tuple[int, int, int, int]:
    """(bc_judged, bc_go, d_rejudged, d_go) を outcome.action から数える。
    judged = 実際に判定器を通した行(bc_confirmed_* / d_redone_*)。"""
    bc_judged = bc_go = d_rejudged = d_go = 0
    for oc in outcomes:
        a = oc.action
        if a.startswith("bc_confirmed"):
            bc_judged += 1
            if oc.verdict == "GO":
                bc_go += 1
        elif a.startswith("d_redone"):
            d_rejudged += 1
            if oc.verdict == "GO":
                d_go += 1
    return bc_judged, bc_go, d_rejudged, d_go


def _build_ports(dry_run: bool, wiring: Optional[str]):
    """(request_loader, lot_counter, data_fetcher, judge) を返す。

    dry_run なら no-op ポート。そうでなければ --wiring モジュールから実ポートを
    解決する(run_bc_d._load_ports を再利用)。実走査で --wiring が無ければエラー。
    """
    if dry_run:
        return (
            _noop_request_loader,
            _NoopLotCounter(),
            _NoopDataFetcher(),
            _NoopJudge(),
        )
    if not wiring:
        raise SystemExit(
            "real scan requires --wiring (module providing request_loader/"
            "lot_counter/data_fetcher/judge); use --dry-run for a no-op trial"
        )
    return run_bc_d._load_ports(wiring)


def main(argv=None) -> int:
    if not _FOUNDATION_OK:
        sys.stderr.write("shared foundation not available: %s\n" % _FOUNDATION_ERR)
        return 2

    ap = argparse.ArgumentParser(
        prog="python -m scripts.weekly_scan.cli",
        description="製造データ解析の週次走査 (a)B/C確認 (b)D再取得",
    )
    ap.add_argument("--ledger", required=True, help="台帳 JSON のパス")
    ap.add_argument(
        "--docs-root", default=None,
        help="解析ディレクトリ・報告の出力先の基点(現状の (a)(b) 走査では任意)",
    )
    ap.add_argument(
        "--wiring", default=None,
        help="実走査時の ODBC+凍結判定器配線モジュールの import パス",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="DB計数・判定器を no-op にし、台帳のラウンドトリップのみ検証",
    )
    ap.add_argument("--run-log", default=None, help="走査ログの書き出し先")
    a = ap.parse_args(argv)

    rows = load_ledger(a.ledger)
    bc_rows, d_rows = _count_classes(rows)

    request_loader, lot_counter, data_fetcher, judge = _build_ports(a.dry_run, a.wiring)
    run = run_bc_d.build_run(
        request_loader=request_loader, lot_counter=lot_counter,
        data_fetcher=data_fetcher, judge=judge,
    )
    scanned_on = today_utc()
    updated, outcomes = run(rows, scanned_on)

    bc_judged, bc_go, d_rejudged, d_go = _count_outcomes(outcomes)

    log = format_run_log(outcomes, scanned_on)
    if a.run_log:
        with open(a.run_log, "w", encoding="utf-8") as fh:
            fh.write(log)

    # 試運転でも実走査でも台帳は書き戻す(ラウンドトリップの検証が試運転の目的)。
    save_ledger(a.ledger, updated)
    written = True

    summary = _summary_line(
        scanned_on, bc_rows=bc_rows, bc_judged=bc_judged, bc_go=bc_go,
        d_rows=d_rows, d_rejudged=d_rejudged, d_go=d_go, written=written,
    )
    sys.stdout.write(summary + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
