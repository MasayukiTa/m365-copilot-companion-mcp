"""既存判定器(validity_judge)の呼び出しラッパ。

既存の `tools.judge_backend.bridge_judge(request_json)` を呼んで 1 件の判定を得て、
judge_result.json のスキーマを解釈して扱いやすい形(JudgeOutcome)で返す。

守ること(この会話の契約):
  - 判定器・基準・Δmin・補正・順序の信頼時計の記録は、既存の登録済みのもの
    (engine_claim_*.json / judge_request.json / 判定器側のしきい値)をそのまま使う。
  - 本ラッパは新しい claim・新しい delta_min・新しい順序時刻を生成しない。
    登録済みの judge_request をそのまま判定器へ渡すだけ。
  - 順序の偽装をしない。order_ok や registered_at は判定器の出力を読み取るだけで、
    書き換えない。

取りこぼし防止の原則:
  判定器が結果を返せない(transport 失敗・エンジン不在)場合は、GO でない扱いに
  倒さず JudgeTransportError を上げて呼び出し側に委ねる。これは既存の command_judge
  が transport 失敗を allow にしないのと同じ考え方。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

#: 判定器の戻り値 JSON を文字列で受け、判定結果 JSON を文字列で返す関数の型。
JudgeFn = Callable[[str], str]


class JudgeTransportError(RuntimeError):
    """判定器を呼べなかった/結果を解釈できなかった(未判定)。

    これを GO でない(採用しない)と取り違えてはいけない。取りこぼし防止のため、
    呼び出し側へそのまま伝える。
    """


@dataclass
class JudgeOutcome:
    """judge_result.json から読み取った、採否判断に必要な事実だけ。

    すべて判定器の出力の読み取りであり、本ラッパは数値を作らない。
    """

    verdict: str                      # 例: "HOLD" / "GO" / "NO" (判定器の verdict)
    order_ok: Optional[bool]          # 登録→ロックの順序(判定器の order.order_ok)
    reasons: List[str] = field(default_factory=list)
    k: Optional[int] = None           # 独立ロット数 K(evidence.K)
    ci_std: Optional[List[float]] = None   # 標準化区間(evidence.ci_std)
    estimate_std: Optional[float] = None   # 標準化推定(evidence.estimate_std)
    delta_min_std: Optional[float] = None  # 登録済み Δmin(evidence.delta_min_std)
    raw: Dict[str, Any] = field(default_factory=dict)  # 元の result 全体

    @property
    def is_go(self) -> bool:
        """判定器が採用(GO)と言ったか。verdict 文字列をそのまま見る。"""
        return str(self.verdict).strip().upper() == "GO"

    @property
    def interval_above_delta_min(self) -> Optional[bool]:
        """標準化区間の下限が Δmin を上回り、区間が 0 を含まないか。

        効果の「傾向」を把握するための読み取り(判定そのものではない)。情報が
        足りなければ None。
        """
        if self.ci_std is None or len(self.ci_std) != 2 or self.delta_min_std is None:
            return None
        lo, hi = self.ci_std[0], self.ci_std[1]
        return lo > 0 and lo > self.delta_min_std and hi > 0


def _default_judge_fn() -> JudgeFn:
    """既定の判定器 tools.judge_backend.bridge_judge を遅延 import で返す。"""
    from tools.judge_backend import bridge_judge

    return bridge_judge


def run_judge(
    request_json: str,
    *,
    judge_fn: Optional[JudgeFn] = None,
) -> JudgeOutcome:
    """登録済みの judge_request(JSON 文字列)を判定器に通し、結果を読み取る。

    request_json は既に登録済みの judge_request.json の本文をそのまま渡す。本関数は
    その中身を書き換えない(Δmin・順序時刻・補正に触れない)。

    Raises:
        JudgeTransportError: 判定器が呼べない/結果が JSON でない/result が無い。
    """
    fn = judge_fn or _default_judge_fn()
    try:
        result_text = fn(request_json)
    except Exception as exc:  # transport 失敗等
        raise JudgeTransportError(f"判定器を呼べませんでした: {exc}") from exc
    return parse_judge_result(result_text)


def parse_judge_result(result_text: str) -> JudgeOutcome:
    """judge_result.json の本文(文字列 or dict)を JudgeOutcome に読み取る。

    judge_result.json は次の形(w67 の実物で確認):
      {"verdict": {"verdict": "HOLD", "reasons": [...],
                    "evidence": {"K":..., "ci_std":[...], "estimate_std":...,
                                 "delta_min_std":...}},
       "order": {"order_ok": false, ...}}
    トップに verdict が文字列で入る簡易形も許容する。
    """
    if isinstance(result_text, dict):
        data: Any = result_text
    else:
        if not result_text or not str(result_text).strip():
            raise JudgeTransportError("判定器が空の結果を返しました。")
        try:
            data = json.loads(result_text)
        except (ValueError, TypeError) as exc:
            raise JudgeTransportError(
                f"判定結果を JSON として読めませんでした: {str(result_text)[:200]}"
            ) from exc
    if not isinstance(data, dict):
        raise JudgeTransportError("判定結果のトップがオブジェクトではありません。")

    verdict_block = data.get("verdict")
    # verdict が入れ子(dict)か、トップ直下の文字列かを吸収する。
    if isinstance(verdict_block, dict):
        vb: Dict[str, Any] = verdict_block
        verdict = str(vb.get("verdict", "")).strip()
    else:
        vb = data
        verdict = str(verdict_block or data.get("verdict", "")).strip()
    if not verdict:
        raise JudgeTransportError("判定結果に verdict がありません。")

    reasons = vb.get("reasons") or data.get("reasons") or []
    if not isinstance(reasons, list):
        reasons = [str(reasons)]

    evidence = vb.get("evidence") if isinstance(vb.get("evidence"), dict) else {}
    order = data.get("order") if isinstance(data.get("order"), dict) else {}
    order_ok = order.get("order_ok")
    if order_ok is not None:
        order_ok = bool(order_ok)

    ci_std = evidence.get("ci_std")
    if ci_std is not None:
        try:
            ci_std = [float(ci_std[0]), float(ci_std[1])]
        except (TypeError, ValueError, IndexError):
            ci_std = None

    return JudgeOutcome(
        verdict=verdict,
        order_ok=order_ok,
        reasons=[str(r) for r in reasons],
        k=_as_int(evidence.get("K")),
        ci_std=ci_std,
        estimate_std=_as_float(evidence.get("estimate_std")),
        delta_min_std=_as_float(evidence.get("delta_min_std")),
        raw=vb if isinstance(vb, dict) else {},
    )


def load_request_json(path: str) -> str:
    """登録済み judge_request.json を文字列として読む(中身は書き換えない)。"""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _as_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
