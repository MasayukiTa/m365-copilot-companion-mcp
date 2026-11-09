"""交絡検出 — 曝露の群ごとの層別キー・時期・ラインの偏りを数値化する。

入力は SCHEMA.md の LotRow 配列。出力は ConfoundReport。
判定器・基準・Δmin・補正は一切参照・変更しない（これは記述統計のみ）。

偏り(imbalance) の定義:
  あるカテゴリ軸（層別キー/ライン/時期）について、各カテゴリ値での treated 比率
  p_k = treated_k / (treated_k + control_k) を、全体の treated 比率 p0 からの
  ロット重み付き絶対偏差で測る。完全に群が分離していれば 1、全カテゴリで群比率が
  全体と同じなら 0。

    imbalance = sum_k w_k * |p_k - p0|  /  max(p0, 1-p0)
    w_k = (treated_k + control_k) / N

  分母 max(p0,1-p0) で正規化し、群サイズが極端に偏っていても 0..1 に収める。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List

# 交絡ありと判定する偏りの閾値（記述的な設計閾値。判定器の基準ではない）。
IMBALANCE_THRESHOLD = 0.34

_PERIOD_LEN = 7  # "YYYY-MM" を時期キーに使う（年月）


def _period_key(date: str) -> str:
    return (date or "")[:_PERIOD_LEN] or "unknown"


def _counts_by(rows: List[Dict[str, Any]], axis_key) -> Dict[str, Dict[str, int]]:
    """axis_key(row) -> カテゴリ値。各カテゴリ値での {treated, control} 件数を返す。"""
    out: Dict[str, Dict[str, int]] = defaultdict(lambda: {"treated": 0, "control": 0})
    for r in rows:
        exp = r.get("exposure")
        if exp not in ("treated", "control"):
            continue
        out[axis_key(r)][exp] += 1
    return {k: dict(v) for k, v in out.items()}


def _imbalance(counts: Dict[str, Dict[str, int]]) -> float:
    """ConfoundReport.imbalance の1軸ぶんを計算して 0..1 を返す。"""
    total_t = sum(c["treated"] for c in counts.values())
    total_c = sum(c["control"] for c in counts.values())
    n = total_t + total_c
    if n == 0:
        return 0.0
    p0 = total_t / n
    denom = max(p0, 1.0 - p0)
    if denom == 0:
        return 0.0
    acc = 0.0
    for c in counts.values():
        nk = c["treated"] + c["control"]
        if nk == 0:
            continue
        pk = c["treated"] / nk
        acc += (nk / n) * abs(pk - p0)
    val = acc / denom
    # 数値誤差で 1 をわずかに超えることがあるのでクリップ
    return max(0.0, min(1.0, val))


def detect_confounding(screen_input: Dict[str, Any]) -> Dict[str, Any]:
    """ScreenInput -> ConfoundReport。

    曝露の群ごとの層別キー・時期・ラインの偏りを見る。
    """
    result_item = screen_input.get("result_item", "")
    rows = screen_input.get("rows", []) or []

    by_product = _counts_by(rows, lambda r: str(r.get("product", "unknown")))
    by_line = _counts_by(rows, lambda r: str(r.get("line", "unknown")))
    by_period = _counts_by(rows, lambda r: _period_key(str(r.get("date", ""))))

    imbalance = {
        "product": round(_imbalance(by_product), 4),
        "line": round(_imbalance(by_line), 4),
        "period": round(_imbalance(by_period), 4),
    }
    confounders = sorted(
        axis for axis, v in imbalance.items() if v >= IMBALANCE_THRESHOLD
    )
    return {
        "result_item": result_item,
        "by_product": by_product,
        "by_line": by_line,
        "by_period": by_period,
        "imbalance": imbalance,
        "confounded": bool(confounders),
        "confounders": confounders,
    }


__all__ = ["detect_confounding", "IMBALANCE_THRESHOLD"]
