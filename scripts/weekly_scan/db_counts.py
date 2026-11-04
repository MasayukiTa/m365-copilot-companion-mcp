"""DB 接続とロット計数のユーティリティ(品名で層別して数える)。

既存の `tools.odbc_ops` の規約に合わせる:
  - 名前付き接続は .env の `MCP_DB_<NAME>=<ODBC 接続文字列>` で定義され、名前は
    小文字で参照する(例: ADAMS -> "adams")。本モジュールは接続文字列や秘密を
    一切保持せず、名前だけを受け取り、実際の問い合わせは注入された runner
    (既定で tools.odbc_ops.odbc_query)に委譲する。
  - odbc_query は GATED(unlock 必須)。本モジュールはその戻り値テキストを
    解釈するだけで、認可やしきい値には一切触れない。

本モジュールは判定器・基準・Δmin・補正・順序時計を変更しない。ロットを数える
だけで、効果の有無は判定しない。
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional

#: 既定の DB 接続名(.env の MCP_DB_ADAMS=... を小文字で参照)。main.py / .env の
#: 規約に合わせ、呼び出し側が明示しない限りこの名前を使う。
DEFAULT_CONNECTION = "adams"

#: odbc_query が結果を返せなかったときの戻り値テキストの接頭辞(tools.odbc_ops)。
_ERROR_PREFIX = "[odbc_query error:"
#: GATED で未 unlock のときに odbc_query が返す拒否テキストに含まれる語。
_LOCKED_MARKERS = ("unlock", "locked", "refused")


QueryRunner = Callable[..., str]


class DbCountError(RuntimeError):
    """DB 問い合わせが結果表を返さなかった(エラー・未 unlock・空など)。

    取りこぼし防止の原則上、数えられないことを「ロット 0」や「達した」と
    取り違えてはいけないので、例外として上げて呼び出し側に判断させる。
    """


def _default_runner() -> QueryRunner:
    """既定の実行器 tools.odbc_ops.odbc_query を遅延 import で返す。

    import 時点では pyodbc や .env を必要としない(単体試験で DI 差し替え可能)。
    """
    from tools.odbc_ops import odbc_query

    return odbc_query


def _looks_like_refusal(text: str) -> bool:
    """odbc_query の戻り値が結果表ではなく拒否/エラーかを判定する。"""
    if not text:
        return True
    head = text.lstrip()
    if head.startswith(_ERROR_PREFIX):
        return True
    low = head.lower()
    # GATED 未 unlock の定型文(短い 1〜2 行)にマーカーが含まれる場合のみ拒否扱い。
    if len(head) <= 400 and any(m in low for m in _LOCKED_MARKERS):
        return True
    return False


def _parse_single_count(text: str) -> int:
    """COUNT 1 行 1 列の結果表テキストから整数を取り出す。

    tools.odbc_ops._format_table はヘッダ行・区切り・値行のテキスト表を返す。
    数値の桁区切りカンマが入り得るので除いてから解釈する。最初に現れる整数を
    返す(COUNT(*) の単一値を想定)。
    """
    if _looks_like_refusal(text):
        raise DbCountError(f"DB 問い合わせが結果を返しませんでした: {text.strip()[:200]}")
    for tok in re.findall(r"-?\d[\d,]*", text):
        return int(tok.replace(",", ""))
    raise DbCountError(f"結果表から件数を読み取れませんでした: {text.strip()[:200]}")


def count_lots(
    query: str,
    *,
    connection: str = DEFAULT_CONNECTION,
    params: Optional[List[object]] = None,
    runner: Optional[QueryRunner] = None,
) -> int:
    """COUNT 系の SQL を 1 本投げて整数のロット数を返す。

    query は呼び出し側が与える(COUNT(DISTINCT ロットNO) 等)。本関数はしきい値
    判断をせず、ただ数える。結果が得られなければ DbCountError を上げる
    (「数えられなかった」を「0」や「達した」と取り違えないため)。
    """
    run = runner or _default_runner()
    text = run(connection, query, params=params, max_rows=2)
    return _parse_single_count(text)


def count_lots_by_hinmei(
    query: str,
    *,
    connection: str = DEFAULT_CONNECTION,
    params: Optional[List[object]] = None,
    hinmei_column: str = "品名",
    count_column: Optional[str] = None,
    runner: Optional[QueryRunner] = None,
) -> Dict[str, int]:
    """品名で層別したロット数を返す({品名: ロット数})。

    query は「品名の列」と「ロット数の列」を含む 2 列(以上)の集計結果を返す
    SELECT であることを前提とする(例:
      SELECT 品名, COUNT(DISTINCT ロットNO) AS lots
      FROM ... GROUP BY 品名)。
    本関数は結果表テキストを解釈して品名ごとの整数を組み立てるだけで、どの品名が
    「規定数に達したか」の判断はしない(呼び出し側の責務)。

    Args:
        hinmei_column: 品名が入っている列の見出し名。
        count_column: ロット数が入っている列の見出し名。None の場合は品名列
            以外で最初に現れる数値列を使う。
    """
    run = runner or _default_runner()
    text = run(connection, query, params=params, max_rows=None)
    if _looks_like_refusal(text):
        raise DbCountError(f"DB 問い合わせが結果を返しませんでした: {text.strip()[:200]}")
    return _parse_grouped_counts(
        text, hinmei_column=hinmei_column, count_column=count_column
    )


def _parse_grouped_counts(
    text: str,
    *,
    hinmei_column: str,
    count_column: Optional[str],
) -> Dict[str, int]:
    """2 列以上のテキスト表を {品名: 件数} に変換する。

    tools.odbc_ops._format_table の出力は、1 行目が列見出し、2 行目が区切り
    (ハイフン等)、以降が値行で、列は 2 個以上のスペースで区切られる。見出しの
    一致で品名列・件数列の位置を決める。見出しが一致しない場合は、品名列=先頭、
    件数列=最初の数値列にフォールバックする。
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise DbCountError("結果表が空です。")
    # 区切り行(ハイフンのみ等)を除いたうえで、見出し行を最初の行とみなす。
    header_cells = _split_cells(lines[0])
    body_start = 1
    if len(lines) > 1 and set(lines[1].strip()) <= set("-+| "):
        body_start = 2

    def _find_col(name: str) -> Optional[int]:
        for i, c in enumerate(header_cells):
            if c.strip() == name:
                return i
        return None

    h_idx = _find_col(hinmei_column)
    c_idx = _find_col(count_column) if count_column else None
    if h_idx is None:
        h_idx = 0

    out: Dict[str, int] = {}
    for raw in lines[body_start:]:
        cells = _split_cells(raw)
        if not cells or len(cells) <= h_idx:
            continue
        name = cells[h_idx].strip()
        if not name:
            continue
        value: Optional[int] = None
        if c_idx is not None and len(cells) > c_idx:
            value = _maybe_int(cells[c_idx])
        if value is None:
            # 品名列以外で最初に現れる整数を件数とみなす。
            for j, cell in enumerate(cells):
                if j == h_idx:
                    continue
                value = _maybe_int(cell)
                if value is not None:
                    break
        if value is None:
            raise DbCountError(f"件数列を解釈できません: {raw.strip()[:120]}")
        out[name] = out.get(name, 0) + value
    if not out:
        raise DbCountError("品名ごとの件数を 1 件も読み取れませんでした。")
    return out


def _split_cells(line: str) -> List[str]:
    """テキスト表の 1 行を列に割る。2 個以上の空白か ' | ' を区切りとみなす。"""
    s = line.strip().strip("|")
    if "|" in s:
        return [c.strip() for c in s.split("|")]
    return [c for c in re.split(r"\s{2,}", s) if c != ""]


def _maybe_int(cell: str) -> Optional[int]:
    m = re.fullmatch(r"-?\d[\d,]*", cell.strip())
    if not m:
        return None
    return int(cell.strip().replace(",", ""))


def lots_shortfall(current: int, required: int) -> int:
    """規定数まであと何ロット足りないかを返す(達していれば 0、超過も 0)。"""
    if required < 0:
        raise ValueError("required は 0 以上である必要があります。")
    return max(0, required - current)
