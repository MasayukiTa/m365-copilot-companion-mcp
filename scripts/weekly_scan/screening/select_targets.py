"""スクリーニング対象の選定 — 未使用、または記録が増えた結果項目のみを返す。

条件「一度 GO でないと判定したものを取り逃さない」を満たすため、過去に一度
スクリーニングした項目でも、記録数が増えていれば再び対象にする。

入力:
  all_items: List[{"result_item": str, "record_count": int}]   現在の全結果項目と記録数
  prior:     Dict[str, int]  過去にスクリーニング済みの {result_item: その時の記録数}
             （共通基盤の台帳/状態JSONから呼び元が渡す。未知項目は prior に無い）

出力:
  List[{"result_item": str, "record_count": int, "reason": "new"|"grew",
        "prior_count": int}]   対象になった項目のみ
"""

from __future__ import annotations

from typing import Any, Dict, List


def select_targets(
    all_items: List[Dict[str, Any]], prior: Dict[str, int]
) -> List[Dict[str, Any]]:
    prior = prior or {}
    out: List[Dict[str, Any]] = []
    for it in all_items or []:
        name = it.get("result_item")
        if not name:
            continue
        try:
            cur = int(it.get("record_count", 0))
        except (TypeError, ValueError):
            cur = 0
        if name not in prior:
            out.append(
                {
                    "result_item": name,
                    "record_count": cur,
                    "reason": "new",
                    "prior_count": 0,
                }
            )
            continue
        pc = int(prior[name])
        if cur > pc:
            out.append(
                {
                    "result_item": name,
                    "record_count": cur,
                    "reason": "grew",
                    "prior_count": pc,
                }
            )
        # cur <= pc の既知項目は、新しい記録が無いので今回は対象外（取り逃しではない）。
    return out


__all__ = ["select_targets"]
