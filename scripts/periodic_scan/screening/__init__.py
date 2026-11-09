"""periodic_scan.screening - 処理 (c) 新規スクリーニングの処理系。

交絡を見抜く設計（曝露の群ごとの層別キー・時期・ラインの偏りを見て、層別キーで層別して
推定する）で、未使用または記録が増えた結果項目をスクリーニングし、強い候補を
事前登録する。入出力スキーマは本パッケージ内 SCHEMA.md に固定。

判定器・基準・Δmin・補正・順序の信頼時計は変更しない。順序の偽装をしない。
"""

from __future__ import annotations

from .confound import detect_confounding, IMBALANCE_THRESHOLD
from .stratify import estimate_stratified, DEFAULT_MIN_LOTS_PER_GROUP
from .screen import screen_result_item, STRONG_THRESHOLD, TARGET_USABLE_STRATA
from .select_targets import select_targets
from .preregister import build_prereg_record, preregister_strong

__all__ = [
    "detect_confounding",
    "IMBALANCE_THRESHOLD",
    "estimate_stratified",
    "DEFAULT_MIN_LOTS_PER_GROUP",
    "screen_result_item",
    "STRONG_THRESHOLD",
    "TARGET_USABLE_STRATA",
    "select_targets",
    "build_prereg_record",
    "preregister_strong",
]
