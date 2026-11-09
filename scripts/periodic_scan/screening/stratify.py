"""層別キーで層別した効果量の推定。

入力は SCHEMA.md の ScreenInput（LotRow 配列を含む）。出力は StrataEstimate。
交絡（層別キーの偏り）に対処するため、層別キー(product)ごとに treated-control の差を取り、
ロット数で重み付けして統合する（層別キーで揃えた比較）。

判定器・基準・Δmin・補正は変更しない。ここは効果量（平均差）の記述的推定のみで、
採否は判定器が別途行う。pooled_delta は「候補の強さ」を測るための材料であって
採用判定ではない。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List

DEFAULT_MIN_LOTS_PER_GROUP = 5


def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def estimate_stratified(screen_input: Dict[str, Any]) -> Dict[str, Any]:
    """ScreenInput -> StrataEstimate。層別キーで層別して pooled_delta を出す。"""
    result_item = screen_input.get("result_item", "")
    rows = screen_input.get("rows", []) or []
    min_lots = int(screen_input.get("min_lots_per_group", DEFAULT_MIN_LOTS_PER_GROUP))

    # 層別キー -> 群 -> 値リスト
    buckets: Dict[str, Dict[str, List[float]]] = defaultdict(
        lambda: {"treated": [], "control": []}
    )
    for r in rows:
        exp = r.get("exposure")
        if exp not in ("treated", "control"):
            continue
        try:
            v = float(r.get("value"))
        except (TypeError, ValueError):
            continue
        buckets[str(r.get("product", "unknown"))][exp].append(v)

    strata: List[Dict[str, Any]] = []
    usable_strata = 0
    weighted_sum = 0.0
    weight_total = 0
    n_total = 0
    for product in sorted(buckets):
        t = buckets[product]["treated"]
        c = buckets[product]["control"]
        n_total += len(t) + len(c)
        mt, mc = _mean(t), _mean(c)
        delta = mt - mc
        # 層の重みは、両群がそろって初めて効果量が意味を持つので min(両群) を使うと
        # 群が極端に偏った層の寄与を自然に下げられる。重み=2*min(nt,nc)（両群ペア数の近似）。
        pair_weight = 2 * min(len(t), len(c))
        usable = len(t) >= min_lots and len(c) >= min_lots
        if usable:
            usable_strata += 1
        strata.append(
            {
                "product": product,
                "n_treated": len(t),
                "n_control": len(c),
                "mean_treated": round(mt, 6),
                "mean_control": round(mc, 6),
                "delta": round(delta, 6),
                "weight": pair_weight,
                "usable": usable,
            }
        )
        if pair_weight > 0:
            weighted_sum += delta * pair_weight
            weight_total += pair_weight

    pooled_delta = round(weighted_sum / weight_total, 6) if weight_total else 0.0
    return {
        "result_item": result_item,
        "strata": strata,
        "pooled_delta": pooled_delta,
        "n_total": n_total,
        "usable_strata": usable_strata,
    }


__all__ = ["estimate_stratified", "DEFAULT_MIN_LOTS_PER_GROUP"]
