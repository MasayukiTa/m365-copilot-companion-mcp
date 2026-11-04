# -*- coding: utf-8 -*-
"""DB 接続とロット計数ユーティリティ（品名で層別して数える）。

用途: 週次走査で「C 分類（ロット数不足）の解析が、確認用ロットの蓄積で規定数に達したか」を
判定するために、製造 DB（接続名 ``adams`` / db=mcl）から不良ロット数を品名で層別して数える。

設計の前提:
  - DB 接続の実体（サーバ・資格情報・接続名）は .env 側に置かれ、このモジュールは接続
    文字列を一切保持・出力しない。実際の接続名・資格情報の参照は、呼び出し側が渡す
    ``query_callable``（既存の ODBC 問い合わせツール相当）に委ねる。既定の接続名は
    ``DEFAULT_CONNECTION``（下記）で与えるが、呼び出し側が ``connection`` 引数で上書きできる。
  - 生の測定行・社員 ID は読まない・返さない。返すのは層（品名）ごとの件数だけ（データ最小化）。
  - SQL の値は必ずバインド変数（?）で渡し、SQL 文字列に値を埋め込まない
    （既存 ``odbc_query(connection, query, params=...)`` の規約に合わせる）。

``odbc_query`` ツール本体は import しない。呼び出し可能オブジェクト ``query_callable`` を
引数で受け取り、``query_callable(connection=..., query=..., params=[...])`` を呼ぶだけにする。
実行時は MCP ツール ``odbc_query`` を渡し、単体試験ではダミーを差す。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

# 既定の ODBC 接続名。製造 DB（ADAMS / db=mcl）を指す想定だが、接続の実体（サーバ名・
# ポート・資格情報・環境変数名）は .env 側と呼び出し側の query_callable が持ち、本モジュール
# は一切保持・出力しない。呼び出し側は count_lots_by_product(connection=...) で上書きできる。
DEFAULT_CONNECTION = "adams"

# 層別キー（品名）に許す文字。SQL はバインド変数で渡すので値自体の注入は起きないが、
# 「品名として想定外の値を黙って数える」ことを防ぐため、識別子の形だけ軽く検証する。
_PRODUCT_RE = re.compile(r"^[0-9A-Za-z\u3040-\u30ff\u4e00-\u9fff_\-]+$")


@dataclass
class LotCount:
    """1 層（品名）の計数結果。"""

    product: str
    lots: int

    def __post_init__(self) -> None:
        if self.lots < 0:
            raise ValueError("lots must be >= 0")


@dataclass
class LotCountResult:
    """品名で層別したロット計数の全体。生データは含めない。"""

    table: str
    unit_column: str
    product_column: str
    per_product: list[LotCount] = field(default_factory=list)

    @property
    def total_lots(self) -> int:
        return sum(c.lots for c in self.per_product)

    def get(self, product: str) -> int:
        for c in self.per_product:
            if c.product == product:
                return c.lots
        return 0

    def meets_required(self, required_lots: int, product: str | None = None) -> bool:
        """必要ロット数に達したか。product 指定時はその層、無指定なら合計で判定する。"""
        have = self.get(product) if product is not None else self.total_lots
        return have >= required_lots


def _validate_identifier(name: str, kind: str) -> str:
    """テーブル名・列名を識別子として検証する（値ではないのでバインドできないため）。

    識別子は呼び出し側（台帳の config）が決めるものでユーザ入力ではないが、SQL に直接
    差し込む以上、英数と一部記号・ドット（schema.table）だけに限って安全側に倒す。
    """
    if not name or not re.match(r"^[0-9A-Za-z_\.\[\]]+$", name):
        raise ValueError("unsafe %s identifier: %r" % (kind, name))
    return name


def count_lots_by_product(
    query_callable: Callable[..., Any],
    table: str,
    unit_column: str,
    product_column: str,
    products: list[str] | None = None,
    connection: str = DEFAULT_CONNECTION,
    where_sql: str | None = None,
    where_params: list[Any] | None = None,
    result_parser: Callable[[Any], list[tuple[str, int]]] | None = None,
) -> LotCountResult:
    """品名で層別して、相異なるロット数を数える。

    生成する SQL（概念上）:
        SELECT <product_column> AS product, COUNT(DISTINCT <unit_column>) AS lots
        FROM <table>
        [WHERE <where_sql> [AND <product_column> IN (?, ?, ...)]]
        GROUP BY <product_column>

    パラメータ:
      query_callable : ``odbc_query`` 相当。``(connection, query, params)`` を受けて
                       行の反復可能（または result_parser で解釈できる形）を返す。
      table          : 対象テーブル/ビュー（例 'MCL.dbo.V_furyo'）。識別子として検証する。
      unit_column    : ロットを表す列（COUNT DISTINCT の対象。例 'lot'）。
      product_column : 層別キー＝品名の列。
      products       : 数える品名の限定リスト（None なら全品名を GROUP BY で返す）。
      connection     : ODBC 接続名（既定 'adams'）。
      where_sql      : 追加の WHERE 句（値は必ず ? で渡し where_params に入れる）。
      where_params   : where_sql のバインド値。
      result_parser  : query_callable の戻り値を [(product, lots), ...] に変換する関数。

    返り値: LotCountResult（層ごとの件数のみ。生の行は保持しない）。
    """
    _validate_identifier(table, "table")
    _validate_identifier(unit_column, "unit_column")
    _validate_identifier(product_column, "product_column")

    clauses: list[str] = []
    params: list[Any] = []
    if where_sql:
        clauses.append("(%s)" % where_sql)
        params.extend(where_params or [])

    if products:
        for p in products:
            if not _PRODUCT_RE.match(str(p)):
                raise ValueError("unsafe product value: %r" % p)
        placeholders = ", ".join("?" for _ in products)
        clauses.append("%s IN (%s)" % (product_column, placeholders))
        params.extend(products)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    query = (
        "SELECT %s AS product, COUNT(DISTINCT %s) AS lots FROM %s%s GROUP BY %s"
        % (product_column, unit_column, table, where, product_column)
    )

    raw = query_callable(connection=connection, query=query, params=params)

    if result_parser is not None:
        rows = result_parser(raw)
    else:
        rows = _default_parse_rows(raw)

    per_product = [LotCount(product=str(prod), lots=int(n)) for prod, n in rows]
    per_product.sort(key=lambda c: c.product)
    return LotCountResult(
        table=table,
        unit_column=unit_column,
        product_column=product_column,
        per_product=per_product,
    )


def _default_parse_rows(raw: Any) -> list[tuple[str, int]]:
    """query_callable の戻り値を [(product, lots)] に変換する既定ロジック。

    受け付ける形:
      - [(product, lots), ...]            （タプル/リストの反復）
      - [{'product': .., 'lots': ..}, ..] （dict の反復）
    テキスト表しか返さない呼び出し口の場合は、呼び出し側が result_parser を渡すこと。
    """
    out: list[tuple[str, int]] = []
    if raw is None:
        return out
    try:
        iterator = iter(raw)
    except TypeError as e:
        raise ValueError(
            "cannot parse query result; pass a result_parser for this query_callable"
        ) from e
    for row in iterator:
        if isinstance(row, dict):
            if "product" in row and "lots" in row:
                out.append((row["product"], row["lots"]))
                continue
            raise ValueError("row dict missing 'product'/'lots': %r" % (row,))
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            out.append((row[0], row[1]))
            continue
        raise ValueError("unrecognised row shape: %r" % (row,))
    return out


__all__ = [
    "DEFAULT_CONNECTION",
    "LotCount",
    "LotCountResult",
    "count_lots_by_product",
]
