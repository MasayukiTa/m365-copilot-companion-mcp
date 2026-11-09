# -*- coding: utf-8 -*-
"""週次走査 処理(c) 新規スクリーニング と 処理(d) 報告生成 を束ねる非テスト実行層。

このモジュールは :mod:`scripts.periodic_scan.screening` と
:mod:`scripts.periodic_scan.report` の公開関数に **本番の呼び出し元** を与える。
run_bc_d.py が処理(a)(b)を結線するのと同じ役割を、処理(c)(d)について果たす。

パイプライン:
  (c) スクリーニング
      1. select_targets       -- 未使用/記録が増えた結果項目のみを対象化（取り逃し防止）
      2. screen_result_item   -- 各対象を交絡検出 + 層別推定で Candidate 化
                                 （内部で detect_confounding / estimate_stratified を呼ぶ）
      3. preregister_strong   -- strong な候補を JSONL に事前登録
                                 （内部で build_prereg_record を呼ぶ）
  (d) 報告生成（検出した場合のみ）
      4. render_md / write_md         -- Markdown 報告
      5. pptx_available / build_slide_spec / write_pptx
                                      -- pptx 報告（python-pptx 無環境はスキップ）
      6. detection_to_entry / append_tracking  -- 追跡項目を台帳.json に追加/更新

判定器・基準・Δmin・補正・順序の信頼時計は一切変更しない。順序の偽装をしない。
入力は各対象の ScreenInput（SCHEMA.md）を呼び元が渡す。重い依存は持ち込まない。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .screening import (
    preregister_strong,
    screen_result_item,
    select_targets,
)
from .report import (
    append_tracking,
    build_slide_spec,
    detection_to_entry,
    pptx_available,
    render_md,
    write_md,
    write_pptx,
)

#: 対象 result_item を受け取り、その ScreenInput（SCHEMA.md の ScreenInput）を返す関数。
#: DB アクセスや読み取りは呼び元の実体に閉じ込め、本モジュールは重い依存を import しない。
ScreenInputLoader = Callable[[str], Optional[Mapping[str, Any]]]


def _candidate_to_detection(candidate: Dict[str, Any], kind: str) -> Dict[str, Any]:
    """Candidate(SCHEMA 5) -> DetectionRecord(report が受ける形)。

    報告に載せる数値は本走査で算出した候補の値のみ（新たな数値を作らない）。
    """
    observed = candidate.get("observed", {}) or {}
    numbers = {
        "pooled_delta": candidate.get("pooled_delta", 0.0),
        "strength": candidate.get("strength", 0.0),
        "n_total": observed.get("n_total", 0),
        "usable_strata": observed.get("usable_strata", 0),
    }
    what_we_saw = [
        "層別キーで層別した pooled_delta=%s（使える層 %s）"
        % (candidate.get("pooled_delta", 0.0), observed.get("usable_strata", 0)),
    ]
    if candidate.get("confounded"):
        what_we_saw.append(
            "交絡軸 %s に偏りを検出（層別で対処）"
            % "・".join(candidate.get("confounders", []))
        )
    return {
        "analysis_id": candidate.get("candidate_id", ""),
        "kind": kind,
        "result_item": candidate.get("result_item", ""),
        "numbers": numbers,
        "strata": candidate.get("strata", []),
        "what_we_saw": what_we_saw,
        "whats_missing": candidate.get("missing", []),
        "proposal": {
            "change": "交絡軸をそろえた条件で再取得する",
            "experiment": "層別キー・ライン・時期をそろえた対照群で追試する",
            "confirm_design": "事前登録した主張を登録済みの順序の信頼時計で確認する",
        },
        "tracking": {
            "class": "C",
            "next_trigger": "記録数がさらに増えたとき、または交絡が解消した条件が揃ったとき",
        },
    }


def screen_targets(
    all_items: Sequence[Mapping[str, Any]],
    prior: Mapping[str, int],
    screen_input_loader: ScreenInputLoader,
    prereg_path: str,
    *,
    now_iso: Optional[str] = None,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    """処理(c): 対象選定 -> スクリーニング -> 強い候補の事前登録。

    戻り値: {"candidates": [...], "preregistered": [...], "targets": [...]}
    """
    targets = select_targets(list(all_items), dict(prior or {}))
    candidates: List[Dict[str, Any]] = []
    for t in targets:
        name = t.get("result_item")
        si = screen_input_loader(name) if name else None
        if not si:
            continue
        candidates.append(screen_result_item(dict(si), today=today))
    preregistered = preregister_strong(candidates, prereg_path, now_iso=now_iso)
    return {"targets": targets, "candidates": candidates, "preregistered": preregistered}


def write_detection_reports(
    candidates: Sequence[Mapping[str, Any]],
    out_dir: str,
    ledger_path: str,
    *,
    scanned_on: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """処理(d): 検出（strong な候補）ごとに md/pptx 報告を書き、台帳へ追跡を追加する。

    戻り値: 各検出の {"analysis_id", "md_path", "pptx_path", "pptx_status", "ledger"}。
    """
    reports: List[Dict[str, Any]] = []
    for c in candidates or []:
        if not c.get("strong"):
            continue
        rec = _candidate_to_detection(dict(c), kind="strong_candidate")
        md_path = write_md(rec, out_dir)
        if pptx_available():
            # build_slide_spec を明示的に通し、write_pptx に渡す形を先に検証する。
            build_slide_spec(rec)
        pptx_path, pptx_status = write_pptx(rec, out_dir)
        ledger_result = append_tracking(rec, ledger_path, scanned_on=scanned_on)
        reports.append(
            {
                "analysis_id": rec["analysis_id"],
                "md_path": md_path,
                "pptx_path": pptx_path,
                "pptx_status": pptx_status,
                "ledger": ledger_result,
            }
        )
    return reports


def run_screening_report(
    all_items: Sequence[Mapping[str, Any]],
    prior: Mapping[str, int],
    screen_input_loader: ScreenInputLoader,
    *,
    prereg_path: str,
    report_dir: str,
    ledger_path: str,
    now_iso: Optional[str] = None,
    today: Optional[str] = None,
    scanned_on: Optional[str] = None,
) -> Dict[str, Any]:
    """処理(c)->(d) を一続きに実行する本番 entry point。

    detection_to_entry は append_tracking の内部から呼ばれる。ここでは c の
    候補生成と事前登録、d の報告生成・台帳追記をこの順に結線するだけで、
    判定器・基準・Δmin・補正・順序の信頼時計は一切変更しない。
    """
    screened = screen_targets(
        all_items,
        prior,
        screen_input_loader,
        prereg_path,
        now_iso=now_iso,
        today=today,
    )
    reports = write_detection_reports(
        screened["candidates"],
        report_dir,
        ledger_path,
        scanned_on=scanned_on,
    )
    return {
        "targets": screened["targets"],
        "candidates": screened["candidates"],
        "preregistered": screened["preregistered"],
        "reports": reports,
    }


def _load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _file_screen_input_loader(inputs_dir: str) -> ScreenInputLoader:
    """CLI 用: inputs_dir/<result_item>.json を ScreenInput として読む loader。"""

    def _load(result_item: str) -> Optional[Mapping[str, Any]]:
        path = os.path.join(inputs_dir, "%s.json" % result_item)
        if not os.path.exists(path):
            return None
        return _load_json(path)

    return _load


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI: 台帳状態(JSON)から対象を選び、スクリーニングと報告を実行する。"""
    ap = argparse.ArgumentParser(description="periodic-scan screening (c) + report (d)")
    ap.add_argument("--items", required=True, help="全結果項目と記録数の JSON 配列")
    ap.add_argument("--prior", default="", help="過去のスクリーニング記録数 {item:count} の JSON")
    ap.add_argument("--inputs-dir", required=True, help="<result_item>.json を置いたディレクトリ")
    ap.add_argument("--prereg", required=True, help="事前登録 JSONL の出力先")
    ap.add_argument("--report-dir", required=True, help="md/pptx 報告の出力ディレクトリ")
    ap.add_argument("--ledger", required=True, help="追跡を追記する台帳.json")
    args = ap.parse_args(argv)

    all_items = _load_json(args.items)
    prior = _load_json(args.prior) if args.prior else {}
    loader = _file_screen_input_loader(args.inputs_dir)
    result = run_screening_report(
        all_items,
        prior,
        loader,
        prereg_path=args.prereg,
        report_dir=args.report_dir,
        ledger_path=args.ledger,
    )
    sys.stdout.write(
        json.dumps(
            {
                "targets": len(result["targets"]),
                "candidates": len(result["candidates"]),
                "preregistered": len(result["preregistered"]),
                "reports": len(result["reports"]),
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
