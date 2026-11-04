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
書き戻す(ラウンドトリップ)。A/X/E/GO 行は素通し。

--- 試運転 (--dry-run) ---
DB 計数・判定器を no-op プロバイダに差し替え、台帳のラウンドトリップ・走査日更新・
次の契機更新というオーケストレーションのみを検証する。DB も判定器も呼ばないため
 judged=0 / GO=0 が正常。no-op は登録済み順序時計も判定も触らず、偶発的に何かを
「成立」させない(偽装しない)。

--- 新規探索と報告 (c)(d) ---
``--screen-input <path>`` を渡すと、その JSON(ScreenInput 配列)を読み、各結果項目を
``screen_result_item`` で候補化し、strong な候補を ``preregister_strong`` で事前登録
(JSONL 追記)する。その後 ``--report-out <dir>`` があれば、strong 候補を
 DetectionRecord 化して md 報告を生成し、``--track-ledger <path>`` があれば追跡項目を
その台帳へ追記する。事前登録レコードの order_clock は state="initial" のみで、
順序の信頼時計を新規に偽装しない。--dry-run では事前登録・報告・追記の書き出し先を
一時ディレクトリに逃がし、実ファイル・実台帳には触れない(いずれも判定器は呼ばない)。
(c)(d) の引数が省略されたときは従来どおり (a)(b) のみを走らせる。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from typing import Any, Mapping, Optional, Sequence

# 共通基盤が import できなければ exit 2 (runbook 契約)。
try:
    from . import run_bc_d
    from .run_bc_d import load_ledger, save_ledger, format_run_log
    from .common import today_utc
    from .screening import screen_result_item, preregister_strong
    from .report import write_md, append_tracking
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
    """試運転では判定器を呼ばない。万が一呼ばれたら raise して、no-op のはずが
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
    screened: int = 0,
    strong: int = 0,
    reports: int = 0,
) -> str:
    return (
        "weekly-scan (a)+(b) %s: B/C rows=%d judged=%d GO=%d; "
        "D rows=%d re-judged=%d GO=%d; written=%s. "
        "(c) screened=%d strong=%d; (d) reports=%d."
        % (
            scanned_on, bc_rows, bc_judged, bc_go,
            d_rows, d_rejudged, d_go, "yes" if written else "no",
            screened, strong, reports,
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


def _load_screen_inputs(path: str) -> list[dict[str, Any]]:
    """ScreenInput 配列の JSON を読む。単一オブジェクトなら 1 要素の配列扱い。"""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, Mapping) and isinstance(data.get("inputs"), list):
        data = data["inputs"]
    if isinstance(data, Mapping):
        return [dict(data)]
    if isinstance(data, list):
        return [dict(x) for x in data]
    raise ValueError("screen-input must be a ScreenInput object or array of them")


def _candidate_to_detection(cand: Mapping[str, Any]) -> dict[str, Any]:
    """Candidate(SCHEMA 5) -> DetectionRecord(SCHEMA 7)。数値は候補の生値のみ。

    報告の数値はこの走査の計算出力(screen_result_item の結果)のみを使う。
    新たな数値を作らない。追跡の next_trigger は「確認用ロットが揃ったら判定器に
    通す」で、新規スクリーニングは事後解析なので分類は C(確認待ち)。
    """
    observed = dict(cand.get("observed", {}) or {})
    return {
        "analysis_id": cand.get("candidate_id", ""),
        "kind": "strong_candidate",
        "result_item": cand.get("result_item", ""),
        "what_we_saw": [
            "品名層別の統合効果量 pooled_delta=%s" % cand.get("pooled_delta", 0.0),
            "強度スコア strength=%s (閾値以上の強い候補)" % cand.get("strength", 0.0),
        ],
        "whats_missing": list(cand.get("missing", []) or []),
        "numbers": {
            "pooled_delta": cand.get("pooled_delta", 0.0),
            "strength": cand.get("strength", 0.0),
            "n_total": observed.get("n_total", 0),
            "usable_strata": observed.get("usable_strata", 0),
        },
        "strata": list(cand.get("strata", []) or []),
        "proposal": {
            "change": "品名・ライン・時期の偏りを揃えて確認用ロットを取る",
            "experiment": "各群 min_lots を満たす前向きロットで確認セットを集める",
            "confirm_design": "品名層別で登録済み順序時計のまま判定器に通す",
        },
        "tracking": {
            "next_trigger": "確認用ロットが各群 min_lots に達したら判定器に通す",
            "class": "C",
        },
    }


def _run_screening_and_report(
    *,
    screen_input_path: Optional[str],
    prereg_out: Optional[str],
    report_out: Optional[str],
    track_ledger: Optional[str],
    scanned_on: str,
    dry_run: bool,
) -> tuple[int, int, int]:
    """(c) スクリーニングと (d) 報告を実行し、(screened, strong, reports) を返す。

    screen_input_path が無ければ何もせず (0,0,0)。dry_run は書き出し先を
    一時ディレクトリに逃がし、実ファイル・実台帳に触れない。判定器はどちらでも呼ばない。
    """
    if not screen_input_path:
        return (0, 0, 0)

    inputs = _load_screen_inputs(screen_input_path)
    candidates = [screen_result_item(si, today=scanned_on) for si in inputs]
    strong = [c for c in candidates if c.get("strong")]

    with tempfile.TemporaryDirectory(prefix="wkscan_cd_") as tmp:
        # 事前登録の書き出し先: dry_run なら一時、そうでなければ指定パス。
        prereg_path = prereg_out
        if dry_run or not prereg_path:
            prereg_path = os.path.join(tmp, "prereg.jsonl")
        preregister_strong(strong, prereg_path, now_iso="%sT00:00:00Z" % scanned_on)

        reports = 0
        if report_out is not None:
            out_dir = report_out
            if dry_run:
                out_dir = os.path.join(tmp, "reports")
            for cand in strong:
                rec = _candidate_to_detection(cand)
                write_md(rec, out_dir)
                reports += 1
                # 追跡項目を台帳へ: dry_run なら一時台帳、実走査なら指定台帳。
                ledger_path = track_ledger
                if dry_run or not ledger_path:
                    ledger_path = os.path.join(tmp, "track_ledger.json")
                append_tracking(rec, ledger_path, scanned_on=scanned_on)

    return (len(candidates), len(strong), reports)


def main(argv=None) -> int:
    if not _FOUNDATION_OK:
        sys.stderr.write("shared foundation not available: %s\n" % _FOUNDATION_ERR)
        return 2

    ap = argparse.ArgumentParser(
        prog="python -m scripts.weekly_scan.cli",
        description="製造データ解析の週次走査 (a)B/C確認 (b)D再取得 (c)新規スクリーニング (d)報告",
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
    # (c)(d) の入口(いずれも任意。省略時は従来の (a)(b) のみ)。
    ap.add_argument(
        "--screen-input", default=None,
        help="(c) ScreenInput 配列の JSON パス。渡すと新規スクリーニングを実行",
    )
    ap.add_argument(
        "--prereg-out", default=None,
        help="(c) 事前登録 JSONL の追記先(省略時は書かない。--dry-run では常に一時)",
    )
    ap.add_argument(
        "--report-out", default=None,
        help="(d) 検出報告 md の出力ディレクトリ(省略時は報告を生成しない)",
    )
    ap.add_argument(
        "--track-ledger", default=None,
        help="(d) 追跡項目を追記する台帳 JSON(省略時は追記しない。--dry-run では常に一時)",
    )
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

    # (c) 新規スクリーニング ・ (d) 報告生成・追跡追記。
    screened, strong, reports = _run_screening_and_report(
        screen_input_path=a.screen_input,
        prereg_out=a.prereg_out,
        report_out=a.report_out,
        track_ledger=a.track_ledger,
        scanned_on=scanned_on,
        dry_run=a.dry_run,
    )

    summary = _summary_line(
        scanned_on, bc_rows=bc_rows, bc_judged=bc_judged, bc_go=bc_go,
        d_rows=d_rows, d_rejudged=d_rejudged, d_go=d_go, written=written,
        screened=screened, strong=strong, reports=reports,
    )
    sys.stdout.write(summary + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
