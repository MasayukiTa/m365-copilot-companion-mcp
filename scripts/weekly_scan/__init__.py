"""週次走査(weekly_scan)の共通基盤パッケージ。

製造データ解析の「週次走査」の仕組みの共通基盤モジュールを提供する。

提供するもの:
  - ledger_io:   台帳(台帳.json)の読み書き I/O。解析ID・分類・次の契機・
                 最終走査日のスキーマを扱う。
  - judge_wrap:  既存判定器(tools.judge_backend.bridge_judge)の呼び出しラッパ。
                 判定器・基準・Δmin・補正・順序の信頼時計の記録は既存の
                 登録済みのものをそのまま使い、新規生成・偽装をしない。
  - db_counts:   DB 接続(tools.odbc_ops の MCP_DB_<NAME> 規約)と、品名で
                 層別したロット計数ユーティリティ。

このパッケージは判定器のしきい値・基準・Δmin・補正・順序時計を一切変更しない。
それらは判定器(engine_claim_*.json / judge_result.json)の登録済みの値をそのまま
読み出して使う。
"""

from __future__ import annotations

__all__ = [
    "ledger_io",
    "judge_wrap",
    "db_counts",
]
