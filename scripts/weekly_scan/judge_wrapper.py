# -*- coding: utf-8 -*-
"""既存 validity 判定器の呼び出しラッパ（薄い層）。

設計の前提（tools/validity_harness.py の冒頭と判定器の実物から確定した事実）:
  - 判定エンジン本体はこの公開リポジトリの外の SECRET フォルダにあり、
    PREREGISTRATION によりバイト一致でロックされている。
    → エンジンのコード・基準・Δmin・補正・claim ファイルを編集・コピーしない。
  - 判定は MCP ツール ``validity_judge(config_id, engine_version)`` が唯一の呼び出し口。
    順序の信頼時計（登録 vs 取得の時刻順序）の記録は、既存の登録済みのものをそのまま使う。
    このラッパは新規登録・順序の偽装を一切しない。

本モジュールが提供するもの:
  1. ``JudgeVerdict``           : judge_result.json の要点を正規化した読み取り専用の構造
  2. ``parse_judge_result()``   : 既存の judge_result.json を ``JudgeVerdict`` に正規化
  3. ``run_judge()``            : validity_judge を呼ぶための呼び出し口（呼び出し関数を注入）

``run_judge`` はツール本体を import しない。呼び出し可能オブジェクト ``judge_callable`` を
引数で受け取り、``judge_callable(config_id=..., engine_version=...)`` を呼ぶだけにする。
こうすることで (1) SECRET エンジンへの依存を持ち込まない、(2) 単体試験でダミーを差せる、
(3) 実行時は MCP ツールや既存ハーネスの呼び出し関数をそのまま渡せる。

surface するフィールドは validity_harness.past_runs と揃える:
id / verdict / finding / reasons / notes / hashes / timestamps のみ。
生の測定行・社員 ID は読まない・返さない。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# 採用（GO）とみなせる verdict 文字列（判定器の出力に合わせる。大小は正規化して比較）。
VERDICT_GO = "GO"
VERDICT_HOLD = "HOLD"
VERDICT_NA = "NA"
VERDICT_ERROR = "ERROR"


@dataclass
class JudgeVerdict:
    """judge_result.json の要点（読み取り専用の正規化ビュー）。

    judge_result.json は入れ子が深く、トップ階層とネストした ``verdict`` 階層の双方に
    同名フィールドが現れる（verdict/reasons/evidence など）。この構造はネストした
    ``verdict`` 階層を優先し、無ければトップ階層で補う。
    """

    claim_id: str
    verdict: str
    claim_supported: bool | None = None
    finding: str | None = None
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    order_ok: bool | None = None
    # 効果量と区間（標準化）。判定器の evidence からそのまま写すだけで、計算も補正もしない。
    estimate_std: float | None = None
    ci_std: list[float] | None = None
    delta_min: float | None = None
    K: int | None = None
    df: int | None = None
    hashes: dict[str, Any] = field(default_factory=dict)
    rules_version: str | None = None

    @property
    def is_go(self) -> bool:
        return str(self.verdict).strip().upper() == VERDICT_GO

    @property
    def is_hold(self) -> bool:
        return str(self.verdict).strip().upper() == VERDICT_HOLD

    @property
    def interval_inside_delta_min(self) -> bool | None:
        """標準化区間が ±delta_min の内側に収まっているか（分類 A の判定材料）。

        判定器の finding（PRACTICAL_NULL 等）を上書きしない。ここでは区間と delta_min が
        両方そろっているときだけ機械的に内外を返し、材料が欠ければ None を返す。
        """
        if self.ci_std is None or self.delta_min is None:
            return None
        if len(self.ci_std) != 2:
            return None
        lo, hi = self.ci_std
        return (lo >= -self.delta_min) and (hi <= self.delta_min)


def _first_present(*vals: Any) -> Any:
    for v in vals:
        if v is not None:
            return v
    return None


def parse_judge_result(result: dict[str, Any] | str | Path) -> JudgeVerdict:
    """judge_result.json（dict / JSON 文字列 / ファイルパス）を JudgeVerdict に正規化する。

    判定器が出した値をそのまま写すだけ。数値の再計算・補正・順序の改変は一切しない。
    """
    if isinstance(result, dict):
        data = result
    elif isinstance(result, Path):
        data = json.loads(result.read_text(encoding="utf-8"))
    else:
        # str: JSON 本文（{ か [ で始まる）ならそのまま解析し、ファイル名として
        # 扱わない。長い JSON 文字列を Path.exists() に渡すと OS が
        # 'File name too long' を投げるため、先に本文判定する。
        s = str(result).strip()
        if s[:1] in "{[":
            data = json.loads(s)
        else:
            p = Path(result)
            try:
                is_file = p.exists()
            except OSError:
                is_file = False
            if is_file:
                data = json.loads(p.read_text(encoding="utf-8"))
            else:
                data = json.loads(s)

    if not isinstance(data, dict):
        raise ValueError("judge result must be an object")

    nested = data.get("verdict") if isinstance(data.get("verdict"), dict) else {}
    order = data.get("order") if isinstance(data.get("order"), dict) else {}
    evidence = nested.get("evidence") if isinstance(nested.get("evidence"), dict) else {}

    # verdict 文字列: トップに文字列があればそれ、無ければネストの verdict フィールド。
    verdict_str = data.get("verdict") if isinstance(data.get("verdict"), str) else None
    if verdict_str is None:
        verdict_str = nested.get("verdict")

    claim_id = _first_present(
        data.get("claim_id"), nested.get("id"), data.get("id")
    )
    if not claim_id:
        raise ValueError("judge result has no claim id")

    reasons = nested.get("reasons") or data.get("reasons") or []
    notes = nested.get("notes") or data.get("notes") or []
    ci_std = _first_present(evidence.get("ci_std"), nested.get("interval", {}).get("ci_std")
                            if isinstance(nested.get("interval"), dict) else None)

    return JudgeVerdict(
        claim_id=str(claim_id),
        verdict=str(verdict_str) if verdict_str is not None else "",
        claim_supported=_first_present(nested.get("claim_supported")),
        finding=_first_present(nested.get("finding"), data.get("finding")),
        reasons=list(reasons),
        notes=list(notes),
        order_ok=order.get("order_ok"),
        estimate_std=evidence.get("estimate_std"),
        ci_std=list(ci_std) if isinstance(ci_std, (list, tuple)) else None,
        delta_min=_first_present(evidence.get("delta_min_std"), evidence.get("delta_min")),
        K=evidence.get("K"),
        df=evidence.get("df"),
        hashes=dict(nested.get("hashes") or {}),
        rules_version=nested.get("rules_version"),
    )


def run_judge(
    config_id: str,
    judge_callable: Callable[..., Any],
    engine_version: str = "",
) -> JudgeVerdict:
    """既存の判定器を呼び、結果を JudgeVerdict に正規化して返す。

    パラメータ:
      config_id      : 既存の登録済み主張の ID（validity_judge に渡す。新規生成しない）。
      judge_callable : ``validity_judge`` 相当の呼び出し可能オブジェクト。
                       ``judge_callable(config_id=config_id, engine_version=engine_version)``
                       として呼ばれ、judge_result.json の dict か JSON 文字列を返すこと。
      engine_version : エンジン版の指定（既定は空＝ハーネス既定に委ねる）。

    このラッパは基準・Δmin・補正・順序の信頼時計を一切変更しない。判定器が返した値を
    そのまま正規化して返すだけ。
    """
    if not config_id or not str(config_id).strip():
        raise ValueError("config_id must be a non-empty registered claim id")
    raw = judge_callable(config_id=config_id, engine_version=engine_version)
    if isinstance(raw, str):
        raw = json.loads(raw)
    return parse_judge_result(raw)


__all__ = [
    "VERDICT_GO",
    "VERDICT_HOLD",
    "VERDICT_NA",
    "VERDICT_ERROR",
    "JudgeVerdict",
    "parse_judge_result",
    "run_judge",
]
