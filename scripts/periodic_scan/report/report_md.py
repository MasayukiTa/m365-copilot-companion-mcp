"""処理 (d) 報告生成 — Markdown 報告。

入力は SCHEMA.md の DetectionRecord（採用 or 強い候補）。出力は md 文字列、
または指定パスへの書き出し。

報告に書く数値は DetectionRecord.numbers / strata に含まれる値のみを使う。
生成時に新たな数値を作らない（この実行の計算出力・読み取りファイル本文のものだけ）。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

_KIND_LABEL = {
    "adopted": "採用（順序の成立した効果あり）",
    "strong_candidate": "強い候補（事前登録済み）",
}


def _fmt_num(v: Any) -> str:
    if isinstance(v, float):
        return ("%.6f" % v).rstrip("0").rstrip(".")
    return str(v)


def _lines_section(title: str, items: List[str]) -> List[str]:
    out = ["## %s" % title, ""]
    if items:
        out += ["- %s" % x for x in items]
    else:
        out.append("- （記載なし）")
    out.append("")
    return out


def render_md(rec: Dict[str, Any]) -> str:
    """DetectionRecord -> Markdown 文字列。"""
    aid = rec.get("analysis_id", "")
    kind = rec.get("kind", "")
    ri = rec.get("result_item", "")
    numbers: Dict[str, Any] = rec.get("numbers", {}) or {}
    strata: List[Dict[str, Any]] = rec.get("strata", []) or []
    proposal: Dict[str, Any] = rec.get("proposal", {}) or {}
    tracking: Dict[str, Any] = rec.get("tracking", {}) or {}

    out: List[str] = []
    out.append("# 週次走査 検出報告: %s" % (ri or aid))
    out.append("")
    out.append("- 解析ID: `%s`" % aid)
    out.append("- 区分: %s" % _KIND_LABEL.get(kind, kind))
    out.append("- 結果項目: %s" % ri)
    out.append("")

    # 数値（本実行で算出・読み取りした値のみ）
    out.append("## 数値（本走査の計算出力）")
    out.append("")
    if numbers:
        for k in sorted(numbers):
            out.append("- %s: %s" % (k, _fmt_num(numbers[k])))
    else:
        out.append("- （数値なし）")
    out.append("")

    out += _lines_section("何を見てそう判断したか", rec.get("what_we_saw", []) or [])
    out += _lines_section("何が足りないか", rec.get("whats_missing", []) or [])

    # 品名層別の内訳（strata の生値のみ）
    if strata:
        out.append("## 品名層別の内訳")
        out.append("")
        out.append("| 品名 | n(treated) | n(control) | mean(treated) | mean(control) | delta |")
        out.append("|---|---|---|---|---|---|")
        for s in strata:
            out.append(
                "| %s | %s | %s | %s | %s | %s |"
                % (
                    s.get("product", ""),
                    s.get("n_treated", ""),
                    s.get("n_control", ""),
                    _fmt_num(s.get("mean_treated", "")),
                    _fmt_num(s.get("mean_control", "")),
                    _fmt_num(s.get("delta", "")),
                )
            )
        out.append("")

    # 改善の提案
    out.append("## 改善の提案")
    out.append("")
    out.append("- どの条件を変えるとよいか: %s" % proposal.get("change", "（記載なし）"))
    out.append("- 確かめる実験の案: %s" % proposal.get("experiment", "（記載なし）"))
    out.append("- 確認の設計: %s" % proposal.get("confirm_design", "（記載なし）"))
    out.append("")

    # 追跡項目
    out.append("## 追跡項目（台帳へ追加）")
    out.append("")
    out.append("- 分類: %s" % tracking.get("class", ""))
    out.append("- 次の契機: %s" % tracking.get("next_trigger", ""))
    out.append("")
    return "\n".join(out)


def write_md(rec: Dict[str, Any], out_dir: str, filename: Optional[str] = None) -> str:
    """DetectionRecord を md に書き出し、書き出したパスを返す。"""
    os.makedirs(out_dir, exist_ok=True)
    name = filename or ("report_%s.md" % (rec.get("analysis_id", "report")))
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_md(rec))
    return path


__all__ = ["render_md", "write_md"]
