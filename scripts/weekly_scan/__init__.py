# -*- coding: utf-8 -*-
"""Weekly-scan package: 製造データ解析の週次走査。

このパッケージは、取りこぼし防止のために週に1回、台帳(台帳.json)上の
未採用解析(A..E)を走査し、B/C 確認・D 再取得・新規スクリーニング・報告生成を
行う。構成は次の3層:

共通基盤 (foundation):
  * ``ledger_io``    -- 台帳 JSON の読み書き I/O。
  * ``judge_wrapper`` -- 既存の judge_result.json を READ-ONLY で読むラッパ。
  * ``db_lots``      -- 名前付き ODBC 接続で品名層別にロットを数える。

処理 (a)(b) — B/C 確認と D 再取得:
  * ``common``       -- 処理系が依存する純関数・プロトコル。
  * ``bc_confirm``   -- B/C 確認。
  * ``d_reacquire``  -- D 再取得。
  * ``run_bc_d``     -- 台帳上で (a)(b) を逓次実行するランナ。

処理 (c)(d) — 新規スクリーニングと報告生成はサブパッケージ
``screening`` / ``report`` にある。

判定器・基準・Δmin・補正・順序の信頼時計は一切変更せず、順序の偽装もしない。
"""

from __future__ import annotations

# 分類コード（棚卸しの5種）。A のみ当面の対象外、B・C・D は見込みある取りこぼし候補。
CLASS_A = "A"  # 効果なしが確かめられた（区間が基準の内側で、ロット数も十分）
CLASS_B = "B"  # 事後の解析のため登録順序が成立しない（傾向は出ているが順序規則で保留止まり）
CLASS_C = "C"  # ロット数不足（あと何ロットで足りるかを記録）
CLASS_D = "D"  # 取得・入力の誤りの疑い（再取得・やり直しが必要）
CLASS_E = "E"  # 判定不能（構造の問題）

VALID_CLASSES = frozenset({CLASS_A, CLASS_B, CLASS_C, CLASS_D, CLASS_E})

# 再走査の対象にする分類（A/E は当面の対象外）。
RESCAN_CLASSES = frozenset({CLASS_B, CLASS_C, CLASS_D})

__all__ = [
    # foundation
    "ledger_io",
    "judge_wrapper",
    "db_lots",
    # (a)(b)
    "common",
    "bc_confirm",
    "d_reacquire",
    "run_bc_d",
    # (c)(d) subpackages
    "screening",
    "report",
    # class codes
    "CLASS_A",
    "CLASS_B",
    "CLASS_C",
    "CLASS_D",
    "CLASS_E",
    "VALID_CLASSES",
    "RESCAN_CLASSES",
]

__version__ = "0.2.0"
