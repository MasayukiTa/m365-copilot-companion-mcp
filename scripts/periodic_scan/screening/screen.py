"""新規スクリーニング本体 — 交絡検出 + 層別推定 を束ね、Candidate を出す。

入力は SCHEMA.md の ScreenInput。出力は Candidate（SCHEMA 5）。

強度スコア strength(0..1) は、効果量の大きさ・使える層の数・交絡の少なさを合成する。
これは「事前登録に回すべき強い候補か」の優先度づけであり、採否の判定ではない
（採否は既存判定器が別途行う。判定器・基準・Δmin・補正は変更しない）。

    strength = effect_term * usable_term * (1 - 0.5 * confound_penalty)
      effect_term    = min(1, |pooled_delta| / max(delta_scale, eps))
      usable_term    = min(1, usable_strata / TARGET_USABLE_STRATA)
      confound_penalty = max(imbalance over confounded axes)  (0..1)

delta_scale は呼び元が ScreenInput.delta_min を渡せばそれを基準に、無ければ
観測された層別 delta の分布から内部的に決める（基準の改変ではなく、無次元化のため）。
"""

from __future__ import annotations

from datetime import date as _date
from typing import Any, Dict, List, Optional

from .confound import detect_confounding
from .stratify import estimate_stratified

STRONG_THRESHOLD = 0.6      # strength がこれ以上なら strong=True（事前登録対象）
TARGET_USABLE_STRATA = 3    # この数の使える層があれば usable_term=1
_EPS = 1e-9


def _delta_scale(screen_input: Dict[str, Any], strata: List[Dict[str, Any]]) -> float:
    """効果量を無次元化する基準。delta_min があればそれ、無ければ層別deltaの
    絶対値の中央値（それも0なら1.0）。判定器の基準は変えない — これは強度スコアの
    スケール合わせにのみ使う内部量。"""
    dm = screen_input.get("delta_min")
    try:
        dm = float(dm)
    except (TypeError, ValueError):
        dm = 0.0
    if dm > 0:
        return dm
    vals = sorted(abs(s["delta"]) for s in strata if s.get("weight", 0) > 0)
    if not vals:
        return 1.0
    mid = vals[len(vals) // 2]
    return mid if mid > 0 else 1.0


def _today() -> str:
    return _date.today().isoformat()


def screen_result_item(
    screen_input: Dict[str, Any], today: Optional[str] = None
) -> Dict[str, Any]:
    """ScreenInput -> Candidate。"""
    result_item = screen_input.get("result_item", "")
    conf = detect_confounding(screen_input)
    est = estimate_stratified(screen_input)
    strata = est["strata"]

    scale = _delta_scale(screen_input, strata)
    effect_term = min(1.0, abs(est["pooled_delta"]) / max(scale, _EPS))
    usable_term = min(1.0, est["usable_strata"] / float(TARGET_USABLE_STRATA))
    if conf["confounded"] and conf["confounders"]:
        confound_penalty = max(conf["imbalance"][ax] for ax in conf["confounders"])
    else:
        confound_penalty = 0.0
    strength = effect_term * usable_term * (1.0 - 0.5 * confound_penalty)
    strength = round(max(0.0, min(1.0, strength)), 4)

    day = today or _today()
    missing: List[str] = []
    if est["usable_strata"] < TARGET_USABLE_STRATA:
        missing.append(
            "層別キーで各群 min_lots を満たす層が %d（目標 %d）"
            % (est["usable_strata"], TARGET_USABLE_STRATA)
        )
    if conf["confounded"]:
        missing.append(
            "交絡軸 %s に偏りがあり、層別後も残差交絡の確認が必要"
            % "・".join(conf["confounders"])
        )
    if not missing:
        missing.append("なし（事後解析のため順序の信頼時計は登録済みのものを使う）")

    return {
        "candidate_id": "screen-%s-%s" % (result_item, day.replace("-", "")),
        "result_item": result_item,
        "pooled_delta": est["pooled_delta"],
        "strength": strength,
        "strong": strength >= STRONG_THRESHOLD,
        "confounded": conf["confounded"],
        "confounders": conf["confounders"],
        "strata": strata,
        "observed": {
            "n_total": est["n_total"],
            "usable_strata": est["usable_strata"],
            "imbalance": conf["imbalance"],
        },
        "missing": missing,
    }


__all__ = ["screen_result_item", "STRONG_THRESHOLD", "TARGET_USABLE_STRATA"]
