"""periodic_scan.report - 処理 (d) 報告生成の処理系。

検出した場合（順序の成立した効果ありの採用、または強い候補）に、何を見て
そう判断したか・何が足りないかを書いた報告（pptx と md）を生成し、改善の提案
（どの条件を変えるとよいか、確かめる実験の案、確認の設計）と追跡項目を台帳に加える。

報告の数値はこの実行の計算出力・読み取りファイル本文のものだけを使う。
"""

from __future__ import annotations

from .report_md import render_md, write_md
from .report_pptx import pptx_available, build_slide_spec, write_pptx
from .ledger_append import detection_to_entry, append_tracking

__all__ = [
    "render_md",
    "write_md",
    "pptx_available",
    "build_slide_spec",
    "write_pptx",
    "detection_to_entry",
    "append_tracking",
]
