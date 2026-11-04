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
    additional_lots_p80: Optional[int] = None  # 規定の確からしさ(p80)まであと何ロット
    additional_lots_p90: Optional[int] = None  # 規定の確からしさ(p90)まであと何ロット
    per_stratum: Dict[str, Any] = field(default_factory=dict)  # 品名等で層別した内訳
    source_form: str = "verdict"  # どの実物スキーマから読んだか
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

    実物には 3 つのスキーマがある(生記録/AHA本番準備で実在確認):
      1) verdict_form     : {"verdict": {...}|str, "order": {...}}(多数派)
      2) harness_form     : {"classification","finding","order_ok","basis"}
                            (手製ハーネスの品名層別出力。basis.per_stratum 付き)
      3) engine_full_form : {"claim","interval","lock","structure","output"}
                            (判定器エンジンの完全出力。verdict 語を持たない)
    いずれの形でも数値は判定器/ハーネスの出力を読み取るだけで、生成・偽装しない。
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

    keys = set(data.keys())
    # 実物には 3 つのスキーマがある(生記録/AHA本番準備で確認):
    #  1) verdict_form     : {"verdict": {...}|str, "order": {...}}
    #  2) harness_form     : {"classification", "finding", "order_ok", "basis"}
    #  3) engine_full_form : {"claim", "interval", "lock", "structure", "output"}
    # どの形でも数値は判定器/ハーネスの出力を読み取るだけで、生成・偽装しない。
    if "classification" in keys and "finding" in keys:
        return _parse_harness_form(data)
    if "claim" in keys and "interval" in keys and "lock" in keys:
        return _parse_engine_full_form(data)
    return _parse_verdict_form(data)


def _parse_verdict_form(data: Dict[str, Any]) -> JudgeOutcome:
    """verdict_form: {"verdict": {...}|str, "order": {...}} を読む。"""
    verdict_block = data.get("verdict")
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

    return JudgeOutcome(
        verdict=verdict,
        order_ok=order_ok,
        reasons=[str(r) for r in reasons],
        k=_as_int(evidence.get("K")),
        ci_std=_as_pair(evidence.get("ci_std")),
        estimate_std=_as_float(evidence.get("estimate_std")),
        delta_min_std=_as_float(evidence.get("delta_min_std")),
        source_form="verdict",
        raw=vb if isinstance(vb, dict) else {},
    )


def _parse_harness_form(data: Dict[str, Any]) -> JudgeOutcome:
    """harness_form: 手製ハーネスの層別出力を読む。

    例: {"classification":"HOLD","finding":"NO_GO_BELOW_DELTA_MIN","order_ok":true,
         "basis":{"confirm_std_effect_stratified":0.278,"confirm_ci95":[..],
                  "delta_min":0.3,"above_delta_min":false,"per_stratum":{...}}}
    classification をそのまま verdict として扱う(GO/HOLD/NO 等)。
    """
    verdict = str(data.get("classification", "")).strip()
    if not verdict:
        raise JudgeTransportError("harness 形に classification がありません。")
    basis = data.get("basis") if isinstance(data.get("basis"), dict) else {}
    order_ok = data.get("order_ok")
    if order_ok is not None:
        order_ok = bool(order_ok)
    finding = data.get("finding")
    reasons = [str(finding)] if finding else []
    # ロット不足(C 分類)の次契機に効く補助情報を reason に残す。
    if basis.get("what_is_missing"):
        reasons.append("what_is_missing: " + str(basis.get("what_is_missing")))
    if basis.get("reason"):
        reasons.append("reason: " + str(basis.get("reason")))
    # per_stratum が無い版では、層別の補助指標を per_stratum に集約して渡す
    # (読み取りのみ。無い項目は入れない)。
    strat = basis.get("per_stratum") if isinstance(basis.get("per_stratum"), dict) else {}
    if not strat:
        for key in ("k_min", "confirm_strata_with_ge3_each", "explore_std_effect_stratified", "sign_flip"):
            if key in basis:
                strat = dict(strat)
                strat[key] = basis[key]
    # k は「観測済みの独立ロット数」を表す場で、k_min(必要最小)とは別物。
    # 混同を避けるため k には入れず、k_min は per_stratum 側に保持する。
    if "k_min" in basis and "k_min" not in strat:
        strat = dict(strat)
        strat["k_min"] = basis["k_min"]
    return JudgeOutcome(
        verdict=verdict,
        order_ok=order_ok,
        reasons=reasons,
        k=None,
        ci_std=_as_pair(basis.get("confirm_ci95")),
        estimate_std=_as_float(basis.get("confirm_std_effect_stratified")),
        delta_min_std=_as_float(basis.get("delta_min")),
        per_stratum=strat,
        source_form="harness",
        raw=data,
    )


def _parse_engine_full_form(data: Dict[str, Any]) -> JudgeOutcome:
    """engine_full_form: 判定器エンジンの完全出力を読む。

    verdict という語を持たないため、エンジンが出した事実のみを読み取る:
      - lock.codes(D_FAIL_NO_LOCK 等)→ reasons
      - interval(estimate_std, lo_std, hi_std, df)→ ci_std/estimate_std
      - structure.K → K、output.diagnostics.capacity.additional_lots_p80/p90
      - claim.delta_min.value → delta_min_std
      - claim_supported(true/false/null)→ GO/NO/UNDETERMINED に写像(偽装しない)
    順序(order_ok)はこの形には明示されないため、lock.codes に NO_LOCK があれば
    False、無ければ None(不明)とする。順序を True に捏造しない。
    """
    claim = data.get("claim") if isinstance(data.get("claim"), dict) else {}
    interval = data.get("interval") if isinstance(data.get("interval"), dict) else {}
    lock = data.get("lock") if isinstance(data.get("lock"), dict) else {}
    structure = data.get("structure") if isinstance(data.get("structure"), dict) else {}
    output = data.get("output") if isinstance(data.get("output"), dict) else {}

    codes = lock.get("codes") or []
    if not isinstance(codes, list):
        codes = [str(codes)]
    codes = [str(c) for c in codes]

    # order_ok: NO_LOCK 系があれば順序不成立(False)、無ければ不明(None)。捏造しない。
    order_ok: Optional[bool] = None
    if any("NO_LOCK" in c or "LOCK" in c for c in codes):
        order_ok = False

    # verdict: エンジンの claim_supported のみに従う。null は UNDETERMINED。
    cs = data.get("claim_supported", output.get("claim_supported"))
    if cs is True:
        verdict = "GO"
    elif cs is False:
        verdict = "NO"
    else:
        verdict = "UNDETERMINED"

    # ci_std: interval.lo_std/hi_std があれば使う。
    ci_std = None
    lo, hi = interval.get("lo_std"), interval.get("hi_std")
    if lo is not None and hi is not None:
        ci_std = _as_pair([lo, hi])

    # capacity(あと何ロットで規定の確からしさに届くか)
    cap = {}
    diag = output.get("diagnostics") if isinstance(output.get("diagnostics"), dict) else {}
    if isinstance(diag.get("capacity"), dict):
        cap = diag["capacity"]

    delta_min = {}
    if isinstance(claim.get("delta_min"), dict):
        delta_min = claim["delta_min"]

    return JudgeOutcome(
        verdict=verdict,
        order_ok=order_ok,
        reasons=codes,
        k=_as_int(structure.get("K") or interval.get("K") or cap.get("K")),
        ci_std=ci_std,
        estimate_std=_as_float(interval.get("estimate_std")),
        delta_min_std=_as_float(delta_min.get("value")),
        additional_lots_p80=_as_int(cap.get("additional_lots_p80")),
        additional_lots_p90=_as_int(cap.get("additional_lots_p90")),
        source_form="engine_full",
        raw=data,
    )


def _as_pair(v: Any) -> Optional[List[float]]:
    """[lo, hi] を float ペアに。読めなければ None。"""
    if v is None:
        return None
    try:
        return [float(v[0]), float(v[1])]
    except (TypeError, ValueError, IndexError):
        return None


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
