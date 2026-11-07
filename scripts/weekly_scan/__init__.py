"""weekly_scan - 製造データ解析の週次走査（共通基盤モジュール）。

このパッケージは「週次走査」の共通基盤だけを提供する:
  - ledger_io   : 台帳 台帳.json の読み書き I/O（解析ID・分類・次の契機・最終走査日）
  - judge_wrapper: 既存の validity 判定器を呼ぶラッパ（基準・Δmin・補正・順序の信頼時計の
                   記録は既存の登録済みのものをそのまま使い、新規生成・偽装をしない）
  - db_lots     : DB 接続とロット計数ユーティリティ（品名で層別して数える）

走査本体（スクリーニング・再取得・報告生成など）は別モジュールが本基盤を import して組む。
本パッケージ自体は判定器・基準・Δmin・補正・順序の信頼時計を一切変更しない。
"""

from __future__ import annotations

# 分類コード（棚卸しの5種）。A のみ当面の対象外、B・C・D は見込みある取りこぼし候補。
CLASS_A = "A"  # 効果なしが確かめられた（区間が基準の内側で、ロット数も十分）
CLASS_B = "B"  # 事後の解析のため登録順序が成立しない（傾向は出ているが順序規則で保留止まり）
CLASS_C = "C"  # ロット数不足（あと何ロットで足りるかを記録）
CLASS_D = "D"  # 取得・入力の誤りの疑い（再取得・やり直しが必要）
CLASS_E = "E"  # 判定不能（構造の問題）

VALID_CLASSES = frozenset({CLASS_A, CLASS_B, CLASS_C, CLASS_D, CLASS_E})

# 再走査の対象にする分類（A/E は当面の対象外。A=効果なし確定, E=構造上判定不能）。
RESCAN_CLASSES = frozenset({CLASS_B, CLASS_C, CLASS_D})

__all__ = [
    "CLASS_A",
    "CLASS_B",
    "CLASS_C",
    "CLASS_D",
    "CLASS_E",
    "VALID_CLASSES",
    "RESCAN_CLASSES",
]
