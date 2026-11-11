# -*- coding: utf-8 -*-
"""sec_report -- plain-Japanese report generator for the screen->estimate->confirm tool.

This is a SEPARATE tool from the validity engine and from the judge harness. It does NOT
judge anything: it takes the structured ConfirmOutcome records produced by
`tools.screen_estimate_confirm.confirm` (whose confirmation step is the only place the judge
is called) and turns them into one plain-language block per factor group, of the exact specified form:

    「AAA では、<原因: 判定器の理由コードや n・交絡・区間から具体的に> のため BBB は言えない。
     一方で、CCC は <確認できた内容と数値>」

TRACEABILITY IS THE WHOLE POINT. Every clause is assembled from either (i) a number already
computed upstream (effect, interval, n=K, delta_min, family m) or (ii) an engine reason code
mapped through REASON_PHRASES below. No sentence is free-invented: the generator never adds a
cause that is not backed by a code or a number present on the outcome. If a reason code is not
in the table, it is surfaced verbatim (never silently dropped, never paraphrased away) so an
unmapped code is visible rather than hidden.

CLI: `python -m tools.sec_report results.json` (or `--stdin`) prints the report. The input is
a JSON list of outcome dicts with the ConfirmOutcome fields.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from typing import Sequence

from tools.screen_estimate_confirm import ConfirmOutcome, verdict_label


# --------------------------------------------------------------------------- reason table
#
# Engine reason codes -> plain Japanese cause clauses. The engine's verdict is organised in
# four gates: A (identification / assumptions), B (balance / overlap), C (effect vs delta_min
# interval), D (registration / lock order). These are the codes the engine emits; each maps to
# ONE fixed phrase. The phrase is a *cause*, meant to be read as "... のため".
REASON_PHRASES: dict[str, str] = {
    # D -- registration / lock order (caps the verdict at HOLD)
    "D_FAIL_LOCK_AFTER_DATA": "主張の登録・ロックが確認データの取得より後だった(事後登録)",
    "D_FAIL_MISSING_LOCK": "主張がロックされていない(事前登録のロックが無い)",
    "D_FAIL_EXPLORATORY": "主張が探索モードのままで確認モードになっていない",
    # C -- effect size vs delta_min interval
    "C_FAIL_STRADDLE": "確認データの区間が最小効果量(delta_min)の内と外にまたがり、効果の有無を分離できない",
    "C_NOT_COMPUTABLE": "確認データのロット数が不足し、区間を計算できない",
    # A -- identification / assumptions
    "A_FAIL_SPARSE_LOTS": "確認データのロット数が少なすぎて量を識別できない",
    # OK codes carry no cause; present only so a mixed list renders nothing for them.
    "A_OK": "", "B_OK": "", "C_OK": "", "D_OK": "",
}

#: Verdict -> the three report words (delegated to the workflow module so the two agree).
_VERDICT_WORD = {"adopt": "採用できる", "hold": "言えない(保留)",
                 "not identifiable": "識別できないため言えない"}


# --------------------------------------------------------------------------- helpers

def _as_outcome(o) -> ConfirmOutcome:
    """Accept either a ConfirmOutcome or a plain dict (from JSON) and normalise to the dataclass."""
    if isinstance(o, ConfirmOutcome):
        return o
    if isinstance(o, dict):
        fields = {f.name for f in dataclasses.fields(ConfirmOutcome)}
        return ConfirmOutcome(**{k: v for k, v in o.items() if k in fields})
    raise TypeError("outcome must be a ConfirmOutcome or a dict")


def _fmt(x, nd: int = 2) -> str:
    """Format a number for the report, or a fixed placeholder when it is missing."""
    if x is None:
        return "該当なし"
    try:
        return f"{float(x):.{nd}f}"
    except (TypeError, ValueError):
        return str(x)


def _interval(ci) -> str:
    if not ci or ci[0] is None or ci[1] is None:
        return "区間なし"
    return f"[{_fmt(ci[0])}, {_fmt(ci[1])}]"


def cause_clause(outcome) -> str:
    """Build the concrete cause (the '...のため' part) from reason codes AND numbers.

    Order: mapped reason-code phrases first (deduplicated, OK codes dropped), then any unmapped
    codes verbatim. Always append the specified numeric context -- n(=K), the
    interval, delta_min -- so the cause names a code and the numbers behind it. Never invents.
    """
    o = _as_outcome(outcome)
    phrases: list[str] = []
    for code in o.reasons:
        # Older stored reason codes used NO where current codes use MISSING.
        lookup_code = code if code in REASON_PHRASES else code.replace("_NO_", "_MISSING_")
        if lookup_code in REASON_PHRASES:
            ph = REASON_PHRASES[lookup_code]
            if ph and ph not in phrases:
                phrases.append(ph)
        else:
            verbatim = f"理由コード {code}"
            if verbatim not in phrases:
                phrases.append(verbatim)
    numeric = (f"確認データでは効果量 {_fmt(o.confirmed_effect)}、"
               f"区間 {_interval(o.confirmed_ci)}、"
               f"ロット数 K={o.n_lots if o.n_lots is not None else '該当なし'}、"
               f"最小効果量 delta_min={_fmt(o.delta_min)}")
    # When a confounding stratum was separated, say so: a reader must see that the 'cannot'
    # conclusion already accounts for the confound (not an artefact of ignoring it).
    if o.group_separated:
        numeric += "、交絡群は層別で分離済み"
    if phrases:
        return "、".join(phrases) + "(" + numeric + ")"
    return numeric


def confirmed_clause(outcome) -> str:
    """Build the 'CCC は <確認できた内容と数値>' part. For an adopt, state the confirmed effect
    and interval with K and family m; otherwise state exactly what the screen could say and
    that confirmation did not reach adopt -- still only numbers that exist."""
    o = _as_outcome(outcome)
    label = verdict_label(o.verdict)
    fam = o.family_m if o.family_m is not None else "該当なし"
    sep = "(交絡を層別で分離したうえで)" if o.group_separated else ""
    if label == "adopt":
        return (f"確認データで{sep}効果量 {_fmt(o.confirmed_effect)}、区間 "
                f"{_interval(o.confirmed_ci)} が delta_min={_fmt(o.delta_min)} を越え、"
                f"ロット数 K={o.n_lots if o.n_lots is not None else '該当なし'}・"
                f"スクリーニング族サイズ m={fam} の多重性を保ったまま採用できた")
    # not adopted: say what the screen showed, and that confirm did not reach adopt
    return (f"スクリーニングでは効果量 {_fmt(o.screened_effect)}、区間 "
            f"{_interval(o.screened_ci)} を示したが、確認データでは採用(adopt)に達していない"
            f"(判定: {_VERDICT_WORD.get(label, label)})")


def sentence_for(outcome) -> str:
    """One factor-group block in the exact requested form.

    「AAA では、<原因> のため BBB は言えない。一方で、CCC は <確認できた内容と数値>」

    AAA/BBB = the factor group; the cause and the confirmed content come straight from the
    structured outcome and the reason-code table.
    """
    o = _as_outcome(outcome)
    aaa = o.factor
    cause = cause_clause(o)
    confirmed = confirmed_clause(o)
    label = verdict_label(o.verdict)
    if label == "adopt":
        # Nothing is being denied: still keep the two-part shape, but BBB is 'より強い主張'.
        return (f"{aaa} では、{cause} のため delta_min を越える以上の過大な主張は言えない。"
                f"一方で、{aaa} は {confirmed}。")
    return (f"{aaa} では、{cause} のため {aaa} の効果があるとは言えない。"
            f"一方で、{aaa} は {confirmed}。")


def render_report(outcomes: Sequence) -> str:
    """Render one block per factor group, in input order, each a standalone paragraph."""
    return "\n\n".join(sentence_for(o) for o in outcomes)


# --------------------------------------------------------------------------- CLI entry

def _load(path_or_none) -> list:
    raw = sys.stdin.read() if path_or_none is None else open(path_or_none, encoding="utf-8").read()
    data = json.loads(raw)
    if isinstance(data, dict) and "outcomes" in data:
        data = data["outcomes"]
    if not isinstance(data, list):
        raise SystemExit("input must be a JSON list of outcome objects (or {outcomes: [...]})")
    return data


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="sec_report",
        description="Render the screen->estimate->confirm results as plain-Japanese, "
                    "one block per factor group (cannot-conclude + can-conclude, with numbers).")
    ap.add_argument("results", nargs="?", default=None,
                    help="Path to a JSON list of outcome objects. Omit (or '-') to read stdin.")
    ap.add_argument("--stdin", action="store_true", help="Read the JSON from stdin.")
    ns = ap.parse_args(argv)
    path = None if (ns.stdin or ns.results in (None, "-")) else ns.results
    outcomes = _load(path)
    sys.stdout.write(render_report(outcomes) + "\n")
    return 0


if __name__ == "__main__":   # pragma: no cover - thin CLI shim, logic is in main()
    raise SystemExit(main())
