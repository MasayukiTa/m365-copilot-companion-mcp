# -*- coding: utf-8 -*-
"""judge_gate -- the final-step seam that routes DATA-ANALYSIS answers through the frozen
validity engine before they are delivered.

OWNER INSTRUCTION (2026-10-27, verbatim): 「データ解析系は、すべて最終でこの判定器を
通すハーネスを入れ、databricks を圧倒す。これが先週与えたタスクだ。」 Re-scoped 2026-10-30:
the owner leaves whether to freeze the registered claims to the agent, and the harness must be
able to read past verdicts from the sessions DB.

WHAT THIS MODULE IS, AND IS NOT.
  * It is the NON-SECRET orchestration seam in the public repo. It DECIDES which goals are data
    analysis (so the judge is applied to them and only them), and it COMPOSES the footer that
    carries the engine's verdict (or the reason the engine could not run) onto the final answer.
  * It is NOT the engine. The verdict engine lives OUTSIDE this repo
    (Desktop/AHA資料_参照一式/証明/公開データ検証/engine_v1_8c2/validity), is secret, remote-less
    and byte-identical-locked by PREREGISTRATION_v1_8c2.md. This module READS it (runs its CLI);
    it NEVER edits the engine's code, rules, thresholds (delta_min, k_min, ...) or registered
    claim files. (A) is read-only, by owner instruction.

THE THREE STEPS the harness performs for a data-analysis final answer (owner 「やること」 2):
  (i)   record the analysis RESULT + DATA sha256 and the fetch time  -> validity_harness.record_manifest
  (ii)  register the claim and LOCK it, recording register-vs-fetch time ORDER
                                                                    -> validity_harness.register_and_lock
  (iii) run the engine and put verdict / reason codes / interval / n on the final answer
                                                                    -> run_engine_judge + build_judge_footer
  When the engine cannot be run, build_judge_footer states WHY on the final answer -- never silent.

Data minimisation: this module surfaces only id / verdict / reason codes / hashes / aggregated
numbers (K, df, n, interval). It reads and emits no raw measurement row and no employee id.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

try:
    from relay.task_kind import task_kind, DATA_QUERY, product_label
except Exception:  # pragma: no cover - allow flat import when run as a script
    from task_kind import task_kind, DATA_QUERY, product_label  # type: ignore

__all__ = [
    "is_data_analysis_goal",
    "build_judge_footer",
    "design_check_result",
    "format_design_check_footer",
    "run_engine_judge",
    "judge_result_pointer",
    "ENGINE_ROOT",
    "MISCLASSIFICATION_NOTE",
    "confirm_overlap_fraction",
    "apply_confirm_independence",
    "confirm_independence_note",
    "design_feasibility",
    "DESIGN_K_MIN",
    "DESIGN_OVERLAP_MIN",
    "DESIGN_MIN_COMMON_STRATA",
    "design_feasibility_hold",
    "DESIGN_HOLD_DISPLAY",
    "load_domain_terms",
]

# The frozen engine checkout (A). Read-only. The engine lives OUTSIDE this public repo on the
# operator's machine, so its path is NOT hard-coded here (a machine-specific absolute path would
# leak a local account name into a public repo). It is supplied at run time via the
# VALIDITY_ENGINE_ROOT environment variable; if unset we fall back to a path under the current
# user's Desktop, built from HOME at run time so no account name is committed. The engine's own
# docs (SKILL.md) record the operator-specific location out-of-band.
_ENGINE_SUBPATH = os.path.join(
    "Desktop", "AHA資料_参照一式", "証明", "公開データ検証", "engine_v1_8c2"
)


def _default_engine_root() -> str:
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, _ENGINE_SUBPATH)


ENGINE_ROOT = os.environ.get("VALIDITY_ENGINE_ROOT") or _default_engine_root()

# --- optional, out-of-band domain vocabulary --------------------------------
#
# WHY THIS EXISTS. This module must read cleanly in a PUBLIC repo, so it carries NO
# site-specific product names, product-name column headers, or process words. The
# human-facing design-hold text below is therefore written in GENERIC terms
# ("grouping column", "product or equipment"). An operator who wants the footer to
# name their real columns/products can supply them out-of-band in
# .config/domain_terms.json (git-ignored, so nothing site-specific is committed).
# OPTIONAL: when absent or empty, the generic wording is used unchanged. Keys:
#   "grouping_column_examples": [str, ...]  # example grouping/stratum column names
#   "group_split_by": str                   # what the compared groups differ by
#   "hold_display": str                     # the forced final-display phrase
# Values are plain display text only; they never change any verdict.
_DOMAIN_TERMS_RELPATH = os.path.join(".config", "domain_terms.json")


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_domain_terms() -> dict:
    """Optional domain vocabulary as a dict, or {} when unavailable. Never raises."""
    path = os.path.join(_repo_root(), _DOMAIN_TERMS_RELPATH)
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

#: WHY THIS NOTE EXISTS. The classifier below is a keyword screen over the goal text (reusing
#: relay/task_kind.py's DATA_QUERY markers, which were read off the live history). A keyword
#: screen necessarily mis-sorts some goals; naming the mis-sorts is what turns the boundary into
#: a decision instead of an accident. The gate is deliberately CONSERVATIVE: a goal is treated as
#: data analysis only when a DATA_QUERY marker is present AND no stronger non-analysis kind
#: (code change / M365 lookup / prose) claims it first -- because applying the judge to a
#: non-analysis answer wastes an engine run and clutters the answer, while MISSING a real
#: analysis is the failure the owner is fixing. When in doubt the caller may force the gate on.
MISCLASSIFICATION_NOTE = (
    "判別条件: task_kind(goal) == DATA_QUERY を基礎に、database / sql / query / odbc / table 等のデータ"
    "取得と、その結果を表・相関・群間比較にする仕事を「データ解析」と判別する。\n"
    "誤検知の例(本当は解析なのに漏れる / 偽陰性): データ元を名指しせず「前回の表をもう一度"
    "集計して」だけの依頼は、DATA_QUERY マーカーが無ければ unknown に落ちて False になる。\n"
    "誤判定の例(解析でないのに拾う / 偽陽性): 「the database reader のコードを small fix する」のようにデータ"
    "語を含むが実際はコード変更の仕事。task_kind は CODE_CHANGE / M365_LOOKUP / WRITING を先に"
    "返す順位なので、それらは解析として拾わない。境界が疑わしいときは呼び出し側が強制 ON にできる。\n"
    "ツール使用だけでは解析と判別しない: run_python / odbc_query を走らせたコード監査やレビューも"
    "コード監査であって解析ではない。ツールはテキストが既に解析を指すときに確度を足すだけである"
    "(2026-11-02: ツール使用のみで解析扱いにした結果、監査系 28 件が誤って却下された回帰の修正)。"
)


#: Data tools whose PRESENCE, on a goal whose TEXT is already data analysis, adds confidence
#: that the run really was one. They are NOT a classifier on their own -- see the regression
#: note on is_data_analysis_goal below.
_DATA_TOOLS = frozenset({"odbc_query", "odbc_to_excel", "run_python"})


def is_data_analysis_goal(goal, request_text="", tool_names=()) -> bool:
    """True when the GOAL (or resolved request) is a data analysis BY ITS CONTENT.

    The classification is the task_kind of the goal and the resolved request text. Tool usage
    may only ADD CONFIDENCE once the text already points at data analysis; it may NEVER make a
    goal an analysis on its own.

    REGRESSION THIS PREVENTS (measured 2026-11-02 15:53, status.json of the running fleet).
    The earlier version returned True whenever the recorded tool names included run_python /
    odbc_query / odbc_to_excel -- i.e. on TOOL USAGE ALONE. Because almost every job runs a
    python snippet at some point, that made the provenance gate (relay/analysis_provenance.py,
    applied where a worker becomes DONE in relay/relay_fleet.py) treat repository audits and
    code reviews as data analyses: 28 worker answers were rejected VERIFY_FAILED "claimed
    numbers not found in this run's computation outputs", and many were NOT analyses at all --
    code-audit and review jobs (submitters h16_14, h16_18, h22a, rv_e_auth and unnamed workers)
    whose answers legitimately carry line numbers, file counts and ids. A code-audit goal such
    as "review backend/routers for defects and write review.md" must NOT be gated; a goal whose
    text asks to analyse / aggregate / screen / estimate from tables, Excel, CSV, DB or
    manufacturing data (task_kind == DATA_QUERY) must still be.
    """
    # The decision is BY CONTENT: the goal, or the resolved request that supplies the substance
    # hidden behind a short case reference, classified as a DATA_QUERY by task_kind.
    text_is_analysis = any(task_kind(text) == DATA_QUERY
                           for text in (goal, request_text))
    if not text_is_analysis:
        # Tool usage alone is NOT data analysis. A code audit that ran run_python to count
        # matches is still a code audit. Returning False here is the whole fix.
        return False
    # The text already points at data analysis; recorded data-tool calls only CONFIRM it
    # (they cannot change an already-positive decision, but reading them keeps the signal
    # explicit for callers that inspect it and documents that tools add confidence, not
    # classification).
    _ = any(name in _DATA_TOOLS for name in tool_names)
    return True


#: The keys the frozen engine's claim loader REQUIRES (validity/claim.py REQUIRED_KEYS plus
#: `assumptions`, which claim_schema.json also marks required and whose absence the engine
#: reports as INPUT_ASSUMPTION_UNDECLARED). Mirrored here because the engine is READ-ONLY and
#: outside this repo; we never edit it. A worker-registered claim that lacks any of these makes
#: the engine return verdict=ERROR with reason INPUT_MISSING_KEY (or INPUT_ASSUMPTION_UNDECLARED)
#: BEFORE any analysis runs -- so when the request supplies the data but the claim is a free-text
#: {id, statement, data_location} shape, we must name the exact missing keys to the worker rather
#: than let the engine fail opaquely. We only screen for PRESENCE of the keys (the engine still
#: owns all value validation); we never fabricate values for missing keys.
_ENGINE_CLAIM_REQUIRED_KEYS = (
    "id", "dataset_id", "data_kind", "response", "exposure", "hierarchy",
    "lot_effect_model", "claim_type", "direction", "delta_min", "mode", "family",
    "assumptions",
)

#: A data-free description of the shape each required engine-claim key expects, shown in a
#: correction so the worker knows what to supply. No measurement values -- only the schema shape,
#: taken from validity/claim_schema.json. The worker owns the real column names and levels (only
#: the worker's analysis knows them); the harness never invents them.
_ENGINE_CLAIM_SCHEMA_HINT = (
    "判定エンジンに渡す registration.claim は自由文ではなく、次の必須キーを持つ構造化オブジェクト"
    "である必要がある(値は解析者が実データの列名・水準から埋める。ハーネスは値を捏造しない): "
    "id(文字列), dataset_id(文字列), data_kind(public|subsampled_public|semi_synthetic|synthetic|"
    "negative_control), response{column:応答列名, type:continuous|binary|binomial_agg}, "
    "exposure{column:曝露列名, kind:numeric|categorical, level:within_lot|between_lot, "
    "contrast:{type:per_unit|range|levels, ...}}, hierarchy{unit_column:ロット列名}, "
    "lot_effect_model(random_independent|fixed_or_correlated), claim_type(superiority|equivalence), "
    "direction(positive|negative|either), delta_min{value:>0, scale:'standardized', rationale:文字列, "
    "source:'preregistration'}, mode(confirmatory|exploratory), family{id:文字列, m:整数>=1}, "
    "assumptions{distribution:文字列, balance:文字列}。lot_diagnosis の y/x/lot(ロット別の率・曝露・"
    "ロット名)を既に出しているなら、同じロット列を hierarchy.unit_column に、曝露列を exposure.column "
    "に対応させること。statement だけの自由文 claim ではエンジンは INPUT_MISSING_KEY になる。"
)


def _engine_claim_missing_keys_line(missing) -> str:
    """A SHORT correction line naming ONLY the engine-claim keys still missing, in schema order.

    Used for the SECOND and later correction turns: by then the worker has already seen the full
    key list and shapes (the first correction carries _ENGINE_CLAIM_SCHEMA_HINT, and so does the
    initial instruction), so a re-send of that long block mainly repeats bytes the worker already
    has. Naming only what is STILL missing is shorter and points at the remaining gap. Same single
    source of truth (the missing-key list the engine screen produced); the harness never invents a
    value for a missing key -- the worker owns the real column names and levels.
    """
    return (
        "registration.claim にまだ次の必須キーが足りない(値は解析者が実データから埋める。ハーネスは"
        "値を捏造しない。各キーの型は初回の指示・スキーマ参照): %s。" % ", ".join(missing))


#: The request schema, named once so a correction turn can hand the worker the EXACT shape
#: expected alongside the field-named message that said what was wrong. No data values here --
#: only the shape, so it is safe to show. The engine's required claim keys are appended from the
#: SAME single source that builds the first correction message (_ENGINE_CLAIM_SCHEMA_HINT), so the
#: INITIAL instruction and the correction can never drift: a worker can write a valid
#: registration.claim the FIRST time instead of learning the keys only after an engine failure.
JUDGE_REQUEST_SCHEMA = (
    "judge_request.json and manifest.json must be UTF-8 without BOM.\n"
    "judge_request.json は JSON オブジェクトで、必須キー: claim_id (空でない文字列・パス区切りを"
    "含まない), data_location (データファイルの絶対パスまたは出力フォルダからの相対パス), "
    "fetched_at (ISO 8601 の時刻文字列)。任意セクション: "
    "manifest {path: manifest を書き出す JSON ファイルのパス (フォルダ不可), dataset_id: 文字列, "
    "result: 解析結果そのものを表す JSON オブジェクト (パス文字列ではない。例 "
    '{"n_lots": 6, "defect_rate_by_lot": {...}, "top_correlation_vs_defect_rate": {...}}'
    ")}; "
    "registration {root: 文字列, claim: {id: <claim_id と一致>, ...}, registered_at: ISO時刻}; "
    "lot_diagnosis {y: 数値配列, x: 数値配列, lot: 配列 (3つは同じ長さ・空でない)}; "
    "confirm_overlap {confirm_lots: 確認用データのロット識別子の配列 (曝露*結果の組ごとのロット"
    "キー。文字列か [曝露, 結果, ロット] の三つ組), prior_opened: 既存解析が開封したロットの台帳 "
    "({ロットキー: 開封時刻ISO} の辞書、または [{lot: .., opened_at: ISO}] の配列)}。confirm_overlap "
    "を渡すと、確認用ロットのうち登録時刻より前に別解析が開封した割合を算出し、割合>0 なら判定票に"
    "『確認用データは開封済み(独立でない)』と明記して order_ok を false にする (registration が必要)。\n"
    "入力の無いセクションは省略する(空の値で埋めない)。\n"
    "判定エンジンを走らせるには、claim_id の claim がエンジンに事前登録(configs/<claim_id>.json)"
    "されているか、さもなければ registration セクションに claim_id と同じ id を持つ claim を入れる"
    "こと。どちらも無いとエンジンは走らず『registered claim not found』になる。\n\n"
    + _ENGINE_CLAIM_SCHEMA_HINT
)


#: Keys that mark a claim as the worker's FREE-TEXT registration shape ({id, statement,
#: data_location}) rather than an attempt at the engine's structured claim. We only screen a claim
#: for the engine schema when it is unambiguously free-text: it carries one of these AND carries
#: none of the engine's structured keys. A claim that already has any structured key (e.g.
#: dataset_id, claim_type, response) is the worker's own attempt at the engine shape -- we pass it
#: straight to the frozen engine, which owns all value validation and will itself report any
#: remaining INPUT_MISSING_KEY. This keeps a minimal {id}-only claim (used by other harness tests)
#: and a partially structured claim flowing to the engine unchanged, while the three real
#: free-text folders (statement + data_location) are screened and corrected before the engine runs.
_FREE_TEXT_CLAIM_MARKERS = ("statement", "data_location")


def engine_claim_schema_gap(claim) -> list:
    """Required engine-claim keys ABSENT from a FREE-TEXT `claim`, in schema order ([] otherwise).

    Returns the missing engine keys ONLY for a claim that is unambiguously the worker's free-text
    registration shape -- it has a free-text marker (statement/data_location) and NONE of the
    engine's structured keys (beyond id). For any other claim (a minimal {id}, or one already
    carrying structured keys) it returns [] so the claim flows to the frozen engine untouched; the
    engine still owns all value validation and will itself report a genuine INPUT_MISSING_KEY.

    PRESENCE check only -- never fabricates a value for a missing key. This exists so that when a
    worker registers a free-text {id, statement, data_location} claim but DID supply the analysis
    data (manifest.result, lot_diagnosis y/x/lot), the harness names the EXACT keys the engine
    needs instead of surfacing an opaque INPUT_MISSING_KEY.
    """
    if not isinstance(claim, dict):
        return []
    structured = [k for k in _ENGINE_CLAIM_REQUIRED_KEYS
                  if k != "id" and k in claim]
    has_marker = any(k in claim for k in _FREE_TEXT_CLAIM_MARKERS)
    if structured or not has_marker:
        # already (partly) structured, or not a free-text claim: let the engine judge it.
        return []
    return [k for k in _ENGINE_CLAIM_REQUIRED_KEYS if k not in claim]


def post_hoc_order_note(order_record) -> str:
    """A plain, honest line for a post-hoc (late) registration, or '' when the order is fine.

    A worker analyses data that already exists, so a registration recorded AFTER the data file's
    mtime can never satisfy a pre-registration order check (order_ok=False). That is NOT an input
    error: the engine's own rule classes a late registration as D_FAIL_LOCK_AFTER_DATA, which
    CAPS the verdict at HOLD (it never makes it ERROR -- see README_engine '判定の順序'). We state
    that plainly so the worker reads 'post-hoc (HOLD上限)' rather than mistaking it for a bug, and
    we name what would make it valid without faking the order or weakening the engine.
    """
    if not isinstance(order_record, dict) or order_record.get("order_ok") is not False:
        return ""
    reg = order_record.get("registered_at")
    mtime = order_record.get("data_file_mtime")
    return (
        "事後登録(post-hoc): 主張の登録時刻(%s)がデータファイルの更新時刻(%s)より後です。"
        "既に存在するデータを解析しているため、事前登録の順序検査は原理的に通りません。"
        "これは入力不備(ERROR)ではなく、エンジンの規則では事後登録=D_FAIL_LOCK_AFTER_DATA で"
        "判定は最大でも HOLD(保留)に留まります。妥当にするには、データ取得より前に主張を登録・"
        "ロックすること(事後にこの順序を偽装はしない)。" % (reg, mtime))


#: Key the harness adds to an order_record when confirmation data proves NOT independent of a
#: prior analysis (a lot already opened before this claim's trusted registration time). The
#: fraction and the offending lot keys are recorded so a reader of judge_result.json sees WHY
#: order_ok was forced false, and build_judge_footer surfaces the same line on the final answer.
_INDEPENDENCE_KEY = "confirm_independence"


def confirm_overlap_fraction(confirm_lots, prior_opened, *, registered_at):
    """Fraction of CONFIRMATION lots that a PRIOR analysis already opened before registration.

    Fixed condition (2): when judge_gate judges a CONFIRMATION run, it must compute -- over the
    confirmation data's lots, keyed by the (exposure, response) pair -- the share of lots the
    ledger shows were opened by ANOTHER analysis at a time BEFORE this claim's trusted
    registered_at. A share > 0 means the confirmation slice was not freshly fetched after
    registration; it was already seen, so it is NOT independent.

    Arguments (all data-free in the sense of carrying no measurement value -- only lot keys and
    times):
      * confirm_lots: iterable of the confirmation run's lot identifiers. Each identifier is the
        (exposure, response) pair's lot key -- a string or a [exposure, response, lot] triple;
        it is normalised to a hashable key here. Duplicates are counted once.
      * prior_opened: mapping {lot_key: opened_at_iso} OR an iterable of {"lot": .., "opened_at":
        ..} records distilled from the ledger (a prior analysis's manifest/lock records the lots
        it opened and when). opened_at is an ISO 8601 time.
      * registered_at: this claim's TRUSTED registration time (ISO 8601). A prior opening counts
        only when it is STRICTLY BEFORE this time -- an opening at or after registration is not a
        pre-registration leak.

    Returns {"fraction": float in [0,1], "n_lots": int, "n_overlap": int, "overlap_lots":
    sorted[str]}. An empty confirmation set yields fraction 0.0 (nothing to leak). Never raises on
    an unparseable prior time -- that record is skipped (it cannot be shown to precede
    registration), because this screen only STOPS on evidence, it never invents an overlap.
    """
    def _key(item):
        if isinstance(item, (list, tuple)):
            return "|".join(str(p) for p in item)
        return str(item)

    lots = []
    seen = set()
    for item in (confirm_lots or []):
        k = _key(item)
        if k not in seen:
            seen.add(k)
            lots.append(k)
    n_lots = len(lots)
    if n_lots == 0:
        return {"fraction": 0.0, "n_lots": 0, "n_overlap": 0, "overlap_lots": []}
    # Normalise prior_opened into {lot_key: [opened_at, ...]}.
    opened = {}
    if isinstance(prior_opened, dict):
        items = prior_opened.items()
    else:
        items = ((rec.get("lot"), rec.get("opened_at"))
                 for rec in (prior_opened or []) if isinstance(rec, dict))
    for lot_key, when in items:
        if lot_key is None:
            continue
        opened.setdefault(_key(lot_key), []).append(when)
    try:
        reg_dt = _parse_iso_gate(registered_at)
    except (ValueError, TypeError):
        # Without a trusted registration time we cannot say any opening PRECEDED it. Report no
        # overlap rather than guess -- the order check elsewhere already flags a missing time.
        return {"fraction": 0.0, "n_lots": n_lots, "n_overlap": 0, "overlap_lots": []}
    overlap = []
    for k in lots:
        for when in opened.get(k, []):
            try:
                if _parse_iso_gate(when) < reg_dt:
                    overlap.append(k)
                    break
            except (ValueError, TypeError):
                continue  # an unparseable prior time is not evidence of a leak; skip it
    n_overlap = len(overlap)
    return {"fraction": n_overlap / n_lots, "n_lots": n_lots,
            "n_overlap": n_overlap, "overlap_lots": sorted(overlap)}


def _parse_iso_gate(s):
    """Parse an ISO 8601 time the same way the harness registry does (assume JST when naive),
    kept local to judge_gate so the gate does not depend on the engine or import order."""
    import datetime as _dt
    t = _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone(_dt.timedelta(hours=9)))
    return t


def apply_confirm_independence(order_record, overlap):
    """Fold an overlap result into the order_record, STOPPING only (order_ok true -> false).

    Fixed condition (2) + the 'stricter / add-information only' rule: when the confirmation data
    is not independent (overlap fraction > 0), this
      * records the independence finding ON the order_record (information added), and
      * forces order_ok to False when it was True (stricter only -- it NEVER flips a False to
        True and NEVER relaxes anything; an order already False stays False).
    A fraction of 0 adds the finding as 'independent: true' and changes order_ok not at all.
    Returns the same order_record (mutated in place) for convenience; tolerates order_record None.
    """
    if not isinstance(order_record, dict):
        return order_record
    frac = float(overlap.get("fraction", 0.0)) if isinstance(overlap, dict) else 0.0
    record = {
        "checked": True,
        "independent": frac <= 0.0,
        "opened_before_registration_fraction": frac,
        "n_lots": overlap.get("n_lots") if isinstance(overlap, dict) else None,
        "n_overlap": overlap.get("n_overlap") if isinstance(overlap, dict) else None,
        "overlap_lots": overlap.get("overlap_lots") if isinstance(overlap, dict) else None,
    }
    order_record[_INDEPENDENCE_KEY] = record
    if frac > 0.0 and order_record.get("order_ok") is True:
        order_record["order_ok"] = False
        prior = order_record.get("order_reason") or ""
        note = ("confirmation data is NOT independent: %.1f%% of its lots (%s/%s, by "
                "exposure*response) were opened by a prior analysis BEFORE this claim's "
                "registration; order_ok forced false"
                % (frac * 100.0, record["n_overlap"], record["n_lots"]))
        order_record["order_reason"] = (prior + "; " + note) if prior else note
    return order_record


def confirm_independence_note(order_record):
    """A plain line for the footer when confirmation data was shown NOT independent, else ''.

    States that the confirmation slice was already opened (so it is not a post-registration fresh
    fetch), names the fraction, and that order_ok was set false for it -- never silent, never
    faked. Returns '' when the check did not run or found the data independent.
    """
    if not isinstance(order_record, dict):
        return ""
    rec = order_record.get(_INDEPENDENCE_KEY)
    if not isinstance(rec, dict) or not rec.get("checked"):
        return ""
    if rec.get("independent"):
        return ""
    frac = rec.get("opened_before_registration_fraction") or 0.0
    return ("確認用データは開封済み(独立でない): 確認用ロットの %.1f%%(%s/%s、曝露*結果の組ごと)が、"
            "この主張の登録時刻より前に別の解析で開封されていました。登録後に初めて取得した未開封"
            "データではないため、order_ok を false にしました(事後にこの順序を偽装はしない)。"
            % ((frac * 100.0), rec.get("n_overlap"), rec.get("n_lots")))


def _short_type(value) -> str:
    """A SHORT description of what a value is -- its type, and a short value only for scalars.
    NEVER a long data blob: a correction message names what was received without echoing data."""
    if isinstance(value, bool):
        return "bool %s" % value
    if isinstance(value, (int, float)):
        return "%s %s" % (type(value).__name__, value)
    if isinstance(value, str):
        v = value if len(value) <= 40 else value[:37] + "..."
        return "str %r (len %d)" % (v, len(value))
    if isinstance(value, list):
        return "array of length %d" % len(value)
    if isinstance(value, dict):
        return "object with keys %s" % (sorted(value.keys())[:8])
    if value is None:
        return "missing (null)"
    return type(value).__name__


#: A minimal, data-free example of a valid manifest.result object, shown in a correction so the
#: worker sees the SHAPE expected (an analysis-result object) instead of a path string.
_MANIFEST_RESULT_EXAMPLE = (
    '{"n_lots": 6, "defect_rate_by_lot": {"<lot>": 0.5}, '
    '"top_correlation_vs_defect_rate": {"<feature>": 0.9}}')


def _validate_manifest(manifest, output_folder="") -> str:
    """'' when the manifest section is usable; else a message naming the EXACT bad field, the
    expected shape, and what was received (type/short value, never long data).

    manifest.path is the JSON file the manifest is WRITTEN to -- never a directory. A worker that
    puts the data folder there makes record_manifest open a directory for writing, which fails
    with Errno 13; catching it here names the field and the mistake instead."""
    if "path" not in manifest or not isinstance(manifest.get("path"), str) or not manifest["path"].strip():
        return ("manifest.path must be a non-empty string (the JSON file the manifest is written "
                "to), got %s" % _short_type(manifest.get("path")))
    # The path must name a FILE to write, not an existing directory. Resolve it the same way the
    # harness will (relative to output_folder) before asking the filesystem.
    _mpath = manifest["path"]
    if output_folder and not os.path.isabs(_mpath):
        _mpath = os.path.join(output_folder, _mpath)
    if os.path.isdir(_mpath):
        return ("manifest.path must be a file to write the manifest JSON into, not a directory; "
                "got a directory path %s. Point it at e.g. "
                "'<output_folder>/manifest.json'." % _short_type(manifest.get("path")))
    if ("dataset_id" not in manifest or not isinstance(manifest.get("dataset_id"), str)
            or not manifest["dataset_id"].strip()):
        return ("manifest.dataset_id must be a non-empty string, got %s"
                % _short_type(manifest.get("dataset_id")))
    if "result" not in manifest or not isinstance(manifest.get("result"), dict):
        return ("manifest.result must be an object (the analysis result itself, NOT a path "
                "string), got %s. Minimal valid example: result=%s"
                % (_short_type(manifest.get("result")), _MANIFEST_RESULT_EXAMPLE))
    return ""


def _validate_registration(registration, claim_id) -> str:
    """'' when the registration section is usable; else a message naming the EXACT bad field or,
    for a claim id mismatch, BOTH ids that differ."""
    if ("root" not in registration or not isinstance(registration.get("root"), str)
            or not registration["root"].strip()):
        return ("registration.root must be a non-empty string (the registry root folder), "
                "got %s" % _short_type(registration.get("root")))
    claim = registration.get("claim")
    if not isinstance(claim, dict):
        return "registration.claim must be an object, got %s" % _short_type(claim)
    if claim.get("id") != claim_id:
        return ("registration.claim.id (%s) does not match the request claim_id (%r); "
                "the two must be identical"
                % (_short_type(claim.get("id")), claim_id))
    if ("registered_at" not in registration
            or not isinstance(registration.get("registered_at"), str)
            or not registration["registered_at"].strip()):
        return ("registration.registered_at must be a non-empty ISO 8601 time string, got %s"
                % _short_type(registration.get("registered_at")))
    return ""


def _validate_lot(lot) -> str:
    """'' when the lot_diagnosis section is usable; else a message naming the EXACT bad field."""
    for k in ("y", "x", "lot"):
        if k not in lot or not isinstance(lot.get(k), list) or not lot[k]:
            return ("lot_diagnosis.%s must be a non-empty array, got %s"
                    % (k, _short_type(lot.get(k))))
    lengths = {k: len(lot[k]) for k in ("y", "x", "lot")}
    if len(set(lengths.values())) != 1:
        return ("lot_diagnosis.y, .x and .lot must be the same length, got %s"
                % lengths)
    return ""


# --- design check: stratifier presence + per-exposure-group product skew ------
#
# WHY THIS EXISTS. An example confirmation returned order_ok=true / GO, but the comparison did not
# hold the product (the deployment's product/stratum column; see .config/domain_terms.json
# "product_label") fixed across the exposure groups, so the GO was not adoptable. order_ok only
# checks the clock order of registration vs data fetch;
# it says nothing about whether the design controlled for product. This block READS a
# worker-supplied `design_check` section and reports two facts the reader needs to judge the
# design -- it NEVER changes the engine verdict and NEVER loosens order_ok. It only ADDS
# information to the judge footer / judge_result.json.
#
# FIELD NAMESPACE. Results are emitted under the single key `design_check` so this block does
# not collide with any field the separate confound_gate owner adds; the two can be merged by key
# without overwriting each other. (As of this change confound_gate has no implementation in the
# repository, so there is no existing field shape to mirror; `design_check` is deliberately a
# self-contained sub-object.)
#
# INPUT (all optional; the section itself is optional and omission is allowed, not an error):
#   design_check = {
#     "stratifier_columns": [<column name>, ...],   # columns the analysis stratified / blocked on
#     "exposure_groups": {                          # per exposure group, a product->count map
#       "<group label>": {"<product name>": <int count>, ...}, ...
#     }
#   }
# No measurement rows, no employee ids -- only column names, product labels and counts.

def _validate_design_check(dc) -> str:
    """'' when the design_check section is usable; else a message naming the EXACT bad field.

    Both sub-fields are optional individually, but when present they must have the right shape:
    stratifier_columns a list of strings; exposure_groups an object whose every value is an
    object mapping product label (string) to a non-negative integer count.
    """
    if not isinstance(dc, dict):
        return "design_check must be an object, got %s" % _short_type(dc)
    strat = dc.get("stratifier_columns")
    if strat is not None:
        if not isinstance(strat, list) or not all(isinstance(c, str) for c in strat):
            return ("design_check.stratifier_columns must be an array of strings, got %s"
                    % _short_type(strat))
    groups = dc.get("exposure_groups")
    if groups is not None:
        if not isinstance(groups, dict):
            return ("design_check.exposure_groups must be an object mapping each exposure group "
                    "label to a {product: count} object, got %s" % _short_type(groups))
        for label, counts in groups.items():
            if not isinstance(counts, dict):
                return ("design_check.exposure_groups[%r] must be a {product: count} object, "
                        "got %s" % (label, _short_type(counts)))
            for prod, n in counts.items():
                if not isinstance(prod, str):
                    return ("design_check.exposure_groups[%r] has a non-string product key %s"
                            % (label, _short_type(prod)))
                if isinstance(n, bool) or not isinstance(n, int) or n < 0:
                    return ("design_check.exposure_groups[%r][%r] must be a non-negative "
                            "integer count, got %s" % (label, prod, _short_type(n)))
    return ""


def design_check_result(dc) -> dict:
    """Compute the design-check fields from a VALIDATED design_check section.

    Returns a self-contained object under which the footer / judge_result.json carry the design
    facts. Pure and side-effect free; it reads only the supplied section (already validated by
    _validate_design_check) and never touches the engine verdict. Fields:

      stratifier_present : bool  -- whether the analysis declared at least one stratifier column.
      stratifier_columns : [str] -- the declared columns (empty when none).
      exposure_groups    : [str] -- the exposure-group labels seen (sorted).
      product_skew: per group, the dominant product and its share, plus a repo-level flag
        `products_aligned` that is True only when every exposure group shares an identical set of
        products (the condition a misaligned comparison violated). `skew_detected` is its negation.

    This reports; it does not decide the verdict. The harness surfaces skew so a human (and the
    separate confound_gate) can see that the groups were not matched on product.
    """
    strat = list(dc.get("stratifier_columns") or [])
    groups = dc.get("exposure_groups") or {}
    out = {
        "stratifier_present": bool(strat),
        "stratifier_columns": strat,
        "exposure_groups": sorted(groups.keys()),
    }
    per_group = {}
    product_sets = []
    for label in sorted(groups.keys()):
        counts = groups[label] or {}
        total = sum(counts.values())
        if total <= 0:
            per_group[label] = {"total": 0, "dominant_product": None, "dominant_share": None}
            product_sets.append(frozenset())
            continue
        dom_prod, dom_n = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
        per_group[label] = {
            "total": total,
            "dominant_product": dom_prod,
            "dominant_share": round(dom_n / total, 4),
        }
        product_sets.append(frozenset(p for p, n in counts.items() if n > 0))
    non_empty = [s for s in product_sets if s]
    products_aligned = bool(non_empty) and all(s == non_empty[0] for s in non_empty)
    out["product_skew"] = {
        "per_group": per_group,
        "products_aligned": products_aligned if groups else None,
        "skew_detected": (not products_aligned) if groups else None,
    }
    return out


def format_design_check_footer(dc_result) -> list:
    """Footer lines for a computed design-check result. [] when there is nothing to report.

    The shape is deliberately a labelled block so it reads next to the engine verdict and lines
    up with whatever confound_gate later emits under the same `design_check` key.
    """
    if not isinstance(dc_result, dict) or not dc_result:
        return []
    term = product_label()
    lines = ["設計検査(design check):"]
    cols = dc_result.get("stratifier_columns") or []
    lines.append("  層別列: %s" % (", ".join(cols) if cols else "なし"))
    skew = dc_result.get("product_skew") or {}
    per_group = skew.get("per_group") or {}
    if per_group:
        for label in sorted(per_group.keys()):
            g = per_group[label]
            if g.get("dominant_product") is None:
                lines.append("  曝露群 %s: %sデータなし" % (label, term))
            else:
                lines.append("  曝露群 %s: 主要%s=%s (占有率 %.1f%%, n=%d)" % (
                    label, term, g["dominant_product"], 100.0 * g["dominant_share"], g["total"]))
        aligned = skew.get("products_aligned")
        if aligned is True:
            lines.append("  %sの偏り: 曝露群間で%s構成は一致(揃っている)" % (term, term))
        elif aligned is False:
            lines.append("  %sの偏り: 曝露群間で%s構成が不一致。比較が%sをそろえていない"
                         "恐れ(採用前に要確認)" % (term, term, term))
    return lines


def complete_judge_request(goal, output_folder, *, strict=False, request_text="",
                           tool_names=(), correction_budget=None, correction_attempt=0):
    """Evaluate the worker's request at completion.

    Returns (accept, visible detail) by default. When ``correction_budget`` is not None the
    caller is prepared to send the worker back to fix a PRESENT-but-INVALID request, so this
    returns a 3-tuple (accept, visible detail, correction) instead: ``correction`` is None when
    nothing is correctable, else {"messages": [...], "schema": <text>, "attempt": n} naming the
    exact field-level problems and the expected schema. A request whose fields are VALID but the
    engine is merely unavailable is NOT correctable (that is not the worker's fault) and yields
    correction=None. An ABSENT request keeps the existing 'judge: none' behaviour unchanged.
    """
    want_correction = correction_budget is not None

    def _ret(accept, detail, correction=None):
        return (accept, detail, correction) if want_correction else (accept, detail)

    if not is_data_analysis_goal(goal, request_text, tool_names):
        return _ret(True, "")
    path = os.path.join(output_folder, "judge_request.json") if output_folder else ""
    if not path or not os.path.exists(path):
        return _ret(False, "judge: none (request required)") if strict else _ret(True, "judge: none")
    try:
        # DEFECT 2: a corrected request must be judged again. judge_result.json is write-once
        # (a harness-written record is never clobbered), so a stale ERROR record left by an
        # earlier failed request would otherwise block the corrected request from ever being
        # judged. Supersede ONLY a stale harness ERROR record that predates this (rewritten)
        # request, archiving it as judge_result.superseded.<ts>.json. A real verdict
        # (HOLD/NA/ADOPT) is NEVER touched.
        _supersede_retryable_result(path)
        try:
            with open(path, encoding="utf-8-sig") as f:
                request = json.load(f)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            if isinstance(e, UnicodeDecodeError):
                problem = "not valid UTF-8 at byte %d" % e.start
            else:
                problem = "not valid JSON at line %d, column %d" % (e.lineno, e.colno)
            msg = ("judge_request.json is %s; rewrite it as UTF-8 without BOM with "
                   "json." "dump(obj, f, ensure_ascii=False)" % problem)
            if not want_correction:
                return _ret(False, "judge: invalid request (%s)" % msg)
            correction = {"messages": [msg], "schema": JUDGE_REQUEST_SCHEMA,
                          "attempt": correction_attempt}
            if correction_attempt < int(correction_budget):
                correction["attempt"] = correction_attempt + 1
                detail = ("judge: request invalid (attempt %d/%d)\n- %s\n\n%s"
                          % (correction_attempt + 1, int(correction_budget), msg,
                             JUDGE_REQUEST_SCHEMA))
                _record_correction(output_folder, claim_id=None,
                                   attempt=correction_attempt + 1, messages=[msg])
                return _ret(True, detail, correction)
            correction["exhausted"] = True
            return _ret(not strict, "judge: request invalid (%s)" % msg, correction)
        claim_id, data_location, fetched_at = (request[k] for k in
                                               ("claim_id", "data_location", "fetched_at"))
        if not all(isinstance(v, str) and v.strip() for v in
                   (claim_id, data_location, fetched_at)):
            raise ValueError("request values must be non-empty strings")
        if os.path.basename(claim_id) != claim_id or claim_id in (".", ".."):
            raise ValueError("invalid claim id")
        if not os.path.isabs(data_location):
            data_location = os.path.abspath(os.path.join(output_folder, data_location))
        from tools import validity_harness

        statuses = []
        order_record = None
        #: Field-level problems the worker CAN fix. A problem lands here only when the section
        #: was PRESENT but a field was the wrong type/shape (or the claim ids differ) -- not when
        #: a section was deliberately OMITTED (omission is allowed) and not when the harness
        #: raised an unexpected OSError. Correcting these is bounded by the caller's budget.
        corrections = []
        manifest = request.get("manifest")
        if manifest is None:
            pass  # omitted on purpose: allowed, not an error
        elif not isinstance(manifest, dict):
            msg = "manifest must be an object, got %s" % _short_type(manifest)
            statuses.append("manifest: not run: %s" % msg)
            corrections.append(msg)
        else:
            bad = _validate_manifest(manifest, output_folder)
            if bad:
                statuses.append("manifest: not run: %s" % bad)
                corrections.append(bad)
            else:
                try:
                    manifest_path = os.path.abspath(os.path.join(output_folder, manifest["path"]))
                    validity_harness.record_manifest(manifest_path, dataset_id=manifest["dataset_id"],
                                                     data_path=data_location, result=manifest["result"],
                                                     fetched_at=fetched_at)
                    statuses.append("manifest: recorded")
                except (OSError, ValueError, TypeError) as e:
                    # An engine/filesystem failure is NOT a worker-correctable field problem.
                    statuses.append("manifest: not run: %s" % e)
        registration = request.get("registration")
        if registration is None:
            pass  # omitted on purpose: allowed
        elif not isinstance(registration, dict):
            msg = "registration must be an object, got %s" % _short_type(registration)
            statuses.append("registration: not run: %s" % msg)
            corrections.append(msg)
        else:
            bad = _validate_registration(registration, claim_id)
            if bad:
                statuses.append("registration: not run: %s" % bad)
                corrections.append(bad)
            else:
                try:
                    claim = registration["claim"]
                    # Validate timestamps before register_and_lock writes the claim.
                    validity_harness._parse_iso(registration["registered_at"])
                    validity_harness._parse_iso(fetched_at)
                    root = os.path.abspath(os.path.join(output_folder, registration["root"]))
                    # Pass data_location so the harness can check the order against the data
                    # file's mtime (evidence), and let the harness stamp a TRUSTED registered_at
                    # itself -- the worker-supplied value is kept only as declared.
                    order_record = validity_harness.register_and_lock(
                        root, claim=claim, data_fetched_at=fetched_at,
                        registered_at=registration["registered_at"],
                        data_location=data_location)
                    statuses.append("registration: recorded (order_ok=%s)" % order_record["order_ok"])
                except (ValueError, OverflowError) as e:
                    # A malformed timestamp string the worker wrote IS correctable.
                    msg = ("registration timestamp could not be parsed as ISO 8601: %s" % e)
                    statuses.append("registration: not run: %s" % msg)
                    corrections.append(msg)
                except (OSError, TypeError) as e:
                    statuses.append("registration: not run: %s" % e)
        lot = request.get("lot_diagnosis")
        if lot is None:
            pass  # omitted on purpose: allowed
        elif not isinstance(lot, dict):
            msg = "lot_diagnosis must be an object, got %s" % _short_type(lot)
            statuses.append("lot diagnosis: not run: %s" % msg)
            corrections.append(msg)
        else:
            bad = _validate_lot(lot)
            if bad:
                statuses.append("lot diagnosis: not run: %s" % bad)
                corrections.append(bad)
            else:
                try:
                    diagnosis = validity_harness.lot_independence_check(
                        y=lot["y"], x=lot["x"], lot=lot["lot"])
                    statuses.append("lot diagnosis: " + json.dumps(diagnosis, ensure_ascii=False))
                except (ValueError, TypeError, ImportError) as e:
                    statuses.append("lot diagnosis: not run: %s" % e)
        # CONFIRMATION-DATA INDEPENDENCE (fixed condition 2). A CONFIRMATION run must be judged on
        # data fetched AFTER the claim was registered. When the request carries a confirm_overlap
        # section -- the confirmation run's lots (by exposure*response) plus the ledger of lots a
        # prior analysis already opened and when -- compute the share of confirmation lots opened
        # before this claim's TRUSTED registration time. A share > 0 means the slice was already
        # seen (not independent): we FORCE order_ok false (stricter only, never relaxes) and add
        # the finding to the order_record so the footer states it. Needs the trusted registered_at
        # from register_and_lock, so it runs only when a registration order_record exists.
        confirm = request.get("confirm_overlap")
        if confirm is not None and order_record is not None:
            if not isinstance(confirm, dict):
                msg = "confirm_overlap must be an object, got %s" % _short_type(confirm)
                statuses.append("confirm independence: not run: %s" % msg)
                corrections.append(msg)
            else:
                try:
                    overlap = confirm_overlap_fraction(
                        confirm.get("confirm_lots"), confirm.get("prior_opened"),
                        registered_at=order_record.get("registered_at"))
                    apply_confirm_independence(order_record, overlap)
                    statuses.append("confirm independence: "
                                    + json.dumps(order_record.get(_INDEPENDENCE_KEY),
                                                 ensure_ascii=False)
                                    + " -> order_ok=%s" % order_record.get("order_ok"))
                except (ValueError, TypeError) as e:
                    statuses.append("confirm independence: not run: %s" % e)
        elif confirm is not None and order_record is None:
            # The check needs a trusted registration time; without a registration section there
            # is none. Say so plainly rather than skip silently.
            statuses.append("confirm independence: not run: no registration order_record "
                            "(a trusted registered_at is required to judge independence)")
        # DESIGN CHECK (this change). Optional section. When present it is validated like the
        # others (a bad shape is a worker-correctable field problem); when valid it is computed
        # into `design_check_computed` and later ADDED to the footer / judge_result.json under the
        # `design_check` key. It never changes the engine verdict and never loosens order_ok --
        # it only surfaces whether the analysis stratified and whether the exposure groups held
        # the product fixed, which is what a misaligned comparison fails to do.
        design_check_computed = None
        dc_section = request.get("design_check")
        if dc_section is None:
            pass  # omitted on purpose: allowed, not an error
        else:
            bad = _validate_design_check(dc_section)
            if bad:
                statuses.append("design check: not run: %s" % bad)
                corrections.append(bad)
            else:
                try:
                    design_check_computed = design_check_result(dc_section)
                    statuses.append("design check: "
                                    + json.dumps(design_check_computed, ensure_ascii=False))
                except (ValueError, TypeError) as e:
                    statuses.append("design check: not run: %s" % e)
        # A BOUNDED CORRECTION TURN, BEFORE THE ENGINE RUNS. If a section was PRESENT but has a
        # field-level problem the worker can fix (wrong type/shape, or a claim id mismatch), and
        # the caller gave a budget that is not yet spent, hand the specific messages + schema back
        # so the worker rewrites the request -- rather than silently ending as 'not run'. An
        # engine that is merely unavailable is NOT a correction (handled below, never sent back).
        if corrections and want_correction and correction_attempt < int(correction_budget):
            detail = ("judge: request invalid (attempt %d/%d)\n" % (
                          correction_attempt + 1, int(correction_budget))
                      + "\n".join("- " + m for m in corrections)
                      + "\n\n" + JUDGE_REQUEST_SCHEMA)
            _record_correction(output_folder, claim_id=claim_id,
                               attempt=correction_attempt + 1, messages=corrections)
            return _ret(True, detail, {"messages": list(corrections),
                                       "schema": JUDGE_REQUEST_SCHEMA,
                                       "attempt": correction_attempt + 1})
        # Pass the worker's registered claim (if the request carried a valid one) so the engine
        # can judge it when the engine owner has not pre-registered configs/<id>.json. A human
        # pre-registered configs claim still wins inside run_engine_judge. The claim is written to
        # a temp file UNDER output_folder (outside the engine root), never into the engine's
        # configs/.
        reg = request.get("registration")
        registered_claim = reg.get("claim") if isinstance(reg, dict) else None
        if not (isinstance(registered_claim, dict)
                and registered_claim.get("id") == claim_id):
            registered_claim = None
        # WORKER CLAIM PRESENT BUT NOT IN THE ENGINE'S SHAPE. A worker that registers a free-text
        # {id, statement, data_location} claim DID the analysis (manifest.result / lot_diagnosis),
        # but the engine's claim loader needs a structured claim (REQUIRED_KEYS). Passing the
        # free-text claim straight through makes the engine return verdict=ERROR / INPUT_MISSING_KEY
        # with nothing saying which keys are missing. When there is NO human-pre-registered configs
        # claim (that one wins and is already structured) and the worker's claim lacks required
        # keys, name the EXACT missing keys and their shape to the worker -- within the correction
        # budget -- instead of letting the engine fail opaquely. We never fabricate the values; the
        # worker owns the real column names and levels. A configs-claim id (human pre-registered)
        # is not screened here: run_engine_judge prefers it and it is already in the engine shape.
        if registered_claim is not None:
            gap = engine_claim_schema_gap(registered_claim)
            if gap:
                # The FIRST correction (correction_attempt == 0) carries the full key list with
                # shapes (_ENGINE_CLAIM_SCHEMA_HINT) and the whole request schema. A SECOND or later
                # correction (the worker has already seen all that, in this message AND in the
                # initial instruction) names ONLY the keys STILL missing and drops the long schema:
                # shorter, and pointed at the remaining gap rather than repeating known bytes.
                first_turn = correction_attempt < 1
                if first_turn:
                    gap_msg = (
                        "judge engine would return INPUT_MISSING_KEY: registration.claim is missing "
                        "the engine-required key(s) %s. %s"
                        % (", ".join(gap), _ENGINE_CLAIM_SCHEMA_HINT))
                    gap_schema = JUDGE_REQUEST_SCHEMA
                else:
                    gap_msg = (
                        "judge engine would return INPUT_MISSING_KEY: "
                        + _engine_claim_missing_keys_line(gap))
                    gap_schema = ""
                statuses.append("engine: not run: registration.claim missing engine keys: %s"
                                % ", ".join(gap))
                corrections.append(gap_msg)
                if want_correction and correction_attempt < int(correction_budget):
                    detail = ("judge: request invalid (attempt %d/%d)\n" % (
                                  correction_attempt + 1, int(correction_budget))
                              + "- " + gap_msg
                              + (("\n\n" + gap_schema) if gap_schema else ""))
                    _record_correction(output_folder, claim_id=claim_id,
                                       attempt=correction_attempt + 1, messages=[gap_msg])
                    return _ret(True, detail, {"messages": [gap_msg],
                                               "schema": gap_schema,
                                               "attempt": correction_attempt + 1})
        verdict = run_engine_judge(claim_id, data_location=data_location, fetched_at=fetched_at,
                                   registered_claim=registered_claim,
                                   judge_folder=output_folder)
        engine_available = bool(verdict.get("engine_available", False))
        # DEFECT 1: tell the worker the engine's OWN messages, not just codes. When the engine
        # could not produce a verdict on a worker-supplied claim, run the engine's claim loader
        # and the data-file column check and put the engine's exact text (unknown key paths, the
        # load_claim messages, and any claim column that is not a column of the data file, with
        # the columns that do exist) into both the correction sent back and _record_correction,
        # spending the existing correction budget. We only preflight a worker-supplied claim with
        # no human-pre-registered configs/<id>.json -- the engine prefers a human config, so we
        # must not preflight a different claim against it. A real verdict (engine produced
        # ADOPT/HOLD/NA) never reaches here as a correction because want_correction turns are
        # bounded and a produced verdict is reported, not corrected.
        if (want_correction and registered_claim is not None and engine_available
                and correction_attempt < int(correction_budget)
                and verdict.get("verdict") not in ("ADOPT", "HOLD", "NA")
                and not os.path.isfile(
                    os.path.join(ENGINE_ROOT, "configs", claim_id + ".json"))):
            preflight = _claim_preflight(path, data_location)
            if preflight:
                corrections.extend(preflight)
                detail = ("judge: request invalid (attempt %d/%d)\n" %
                          (correction_attempt + 1, int(correction_budget))
                          + "\n".join("- " + m for m in preflight))
                _record_correction(output_folder, claim_id=claim_id,
                                   attempt=correction_attempt + 1, messages=preflight)
                return _ret(True, detail, {"messages": preflight,
                                           "schema": JUDGE_REQUEST_SCHEMA,
                                           "attempt": correction_attempt + 1})
        # ENGINE COULD NOT FIND A CLAIM, AND THE WORKER CAN FIX THAT. When the engine did not run
        # ONLY because no claim was found (no human-pre-registered configs/<id>.json AND the
        # request carried no matching registration.claim), the worker CAN fix it by adding the
        # claim to the request -- unlike a genuinely unavailable engine (root missing, timeout),
        # which is not the worker's fault and is never sent back. We detect the fixable case from
        # the structured signal run_engine_judge returns (claim_source is None with the
        # 'registered claim not found' reason) rather than string-matching the whole message, and
        # name EXACTLY what to send: a registration.claim whose id equals the request claim_id.
        if (not engine_available and registered_claim is None
                and isinstance(verdict, dict) and verdict.get("claim_source") is None
                and str(verdict.get("unavailable_reason", "")).startswith(
                    "registered claim not found")):
            claim_msg = (
                "judge engine did not run: no registered claim for id %r. Add a \"registration\" "
                "section whose \"claim\" is an object with \"id\": %r (matching the request "
                "claim_id) and \"registered_at\": an ISO 8601 time, plus \"root\": a registry "
                "folder path. The harness passes that claim to the engine."
                % (claim_id, claim_id))
            statuses.append("engine: not run: %s" % verdict.get("unavailable_reason"))
            if want_correction and correction_attempt < int(correction_budget):
                detail = ("judge: request invalid (attempt %d/%d)\n" % (
                              correction_attempt + 1, int(correction_budget))
                          + "- " + claim_msg + "\n\n" + JUDGE_REQUEST_SCHEMA)
                _record_correction(output_folder, claim_id=claim_id,
                                   attempt=correction_attempt + 1, messages=[claim_msg])
                return _ret(True, detail, {"messages": [claim_msg],
                                           "schema": JUDGE_REQUEST_SCHEMA,
                                           "attempt": correction_attempt + 1})
            corrections.append(claim_msg)
        # DESIGN FORCE-HOLD (this slice). Task 1's design-feasibility check (which reads
        # NO response/outcome column) writes its RESULT into the optional
        # request["design_feasibility"] section. When it reports the comparison design is
        # infeasible, the footer and the durable record force the final display to
        # 設計不成立(保留) regardless of the engine verdict, naming the reason code and
        # what was seen (common strata, per-group lot counts, overlap coefficient). This
        # is stricter-only and leaves the engine's own output untouched. A missing or
        # feasible section changes nothing. We only read the section if it is a dict; the
        # normaliser (design_feasibility_hold) ignores anything else.
        design = request.get("design_feasibility")
        if not isinstance(design, dict):
            design = None
        elif design.get("feasible") is False:
            statuses.append("design feasibility: infeasible -> final display forced to "
                            + DESIGN_HOLD_DISPLAY
                            + " (reason_code=%s)" % design.get("reason_code"))
        else:
            statuses.append("design feasibility: feasible (no hold forced)")
        footer = build_judge_footer(verdict=verdict,
                                    engine_available=engine_available,
                                    order_record=order_record,
                                    design_check=design_check_computed,
                                    design_feasibility=design)
        full_text = "judge: requested\n" + "\n".join(statuses) + "\n" + footer
        # THE DURABLE RECORD COMES FROM HERE, NOT FROM THE WORKER. The worker used to run this
        # function itself and submit the returned text as its own reason; that made the record
        # forgeable. Write the full footer + structured result to judge_result.json next to the
        # job output so the authoritative record is harness-authored. A harness-written file
        # already present is never overwritten (a re-run must not clobber the record). The full
        # text is still returned for the VISIBLE answer (the footer the owner reads and the gate
        # checks); only the outcome.json pointer is taken from the harness record, never worker
        # text -- see RelayWorker._settle_done, which reads judge_result.json for the reason.
        _write_judge_result(
            output_folder, claim_id=claim_id, statuses=statuses, footer=footer,
            verdict=verdict, engine_available=engine_available, order_record=order_record,
            design_check=design_check_computed, design_feasibility=design)
        # BUDGET EXHAUSTED WITH THE REQUEST STILL INVALID. The correction turns were spent and a
        # fixable field problem remains: the completion marker names it, and under strict the job
        # is rejected (VERIFY_FAILED). A valid request whose only issue is an unavailable engine
        # reaches here with corrections == [] and is accepted as before (not the worker's fault).
        if corrections and want_correction:
            marker = ("judge: request invalid (" + "; ".join(corrections) + ")")
            invalid_text = full_text + "\n" + marker
            if strict:
                return _ret(False, invalid_text,
                            {"messages": list(corrections), "schema": JUDGE_REQUEST_SCHEMA,
                             "attempt": correction_attempt, "exhausted": True})
            return _ret(True, invalid_text,
                        {"messages": list(corrections), "schema": JUDGE_REQUEST_SCHEMA,
                         "attempt": correction_attempt, "exhausted": True})
        return _ret(True, full_text)
    except (OSError, ValueError, TypeError, KeyError) as e:
        return _ret(False, "judge: invalid request (%s)" % e)


def _claim_preflight(request_path, data_location):
    """Return the engine's own claim/column messages, run in an isolated interpreter.

    Shells judge_claim_preflight.py with the engine root on sys.path so importing the frozen
    engine never perturbs the harness process. Read-only. An unavailable engine (root missing,
    import failure, timeout, non-zero exit) is NOT a worker-fixable problem, so it returns [] --
    never a spurious correction.
    """
    if not os.path.isdir(ENGINE_ROOT):
        return []
    from tools.childproc import headless_creationflags
    try:
        result = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(__file__), "judge_claim_preflight.py"),
             ENGINE_ROOT, request_path, data_location],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
            creationflags=headless_creationflags())
        return json.loads(result.stdout) if result.returncode == 0 else []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def _supersede_retryable_result(request_path):
    """Archive ONLY a stale harness ERROR record that predates the (rewritten) request.

    judge_result.json is write-once, so a corrected request would never be judged while an older
    ERROR record sits next to it. We move that one aside to judge_result.superseded.<ts>.json so
    the corrected request is judged afresh. Guards, in order:
      * the request must be strictly newer than the record (an untouched request changes nothing);
      * a real verdict -- HOLD, NA or ADOPT -- is NEVER archived;
      * a harness-correction record (status 'correcting', no verdict) is NEVER archived -- it
        carries the bounded correction history that _write_judge_result folds into the eventual
        authoritative record, so moving it would lose that history.
    Only a 'source: harness' record whose verdict is ERROR is superseded. Never raises.
    """
    result = os.path.join(os.path.dirname(request_path), "judge_result.json")
    try:
        if os.stat(request_path).st_mtime_ns <= os.stat(result).st_mtime_ns:
            return
        with open(result, encoding="utf-8-sig") as f:
            record = json.load(f)
        verdict = record.get("verdict")
        token = verdict.get("verdict") if isinstance(verdict, dict) else verdict
        if record.get("source") == "harness" and token == "ERROR":
            archive = os.path.join(os.path.dirname(result),
                                   "judge_result.superseded.%s.json" % time.time_ns())
            os.rename(result, archive)
    except (OSError, ValueError, TypeError, AttributeError):
        return


def _record_correction(output_folder, *, claim_id, attempt, messages) -> None:
    """Append one correction turn (attempt number + the specific messages) to judge_result.json
    so the durable record shows the request was sent back and why. NEVER raises -- a lost record
    line must not fail the correction it is recording. Does not write the authoritative 'source:
    harness' verdict record (that only happens once the engine step runs); it keeps a running
    'corrections' list so each bounded retry is visible even while the request stays invalid.
    """
    if not output_folder:
        return
    path = os.path.join(output_folder, "judge_result.json")
    rec = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as f:
                existing = json.load(f)
            if isinstance(existing, dict):
                rec = existing
        except (OSError, ValueError, TypeError):
            rec = {}
    # Do not overwrite an authoritative harness verdict record with a correction note.
    if rec.get("source") == "harness":
        return
    history = rec.get("corrections")
    if not isinstance(history, list):
        history = []
    history.append({"attempt": int(attempt), "messages": list(messages)})
    rec.update({"status": "correcting", "claim_id": claim_id,
                "corrections": history, "source": "harness-correction"})
    try:
        with open(path, "wb") as f:
            f.write(json.dumps(rec, ensure_ascii=False, indent=1,
                               sort_keys=True).encode("utf-8"))
    except OSError:
        return


def _engine_status_line(verdict: dict, engine_available: bool) -> str:
    """One-line engine status for the record. When the engine did not run, it names WHY in the
    'judge: not run (engine unavailable: <reason>)' form the owner asks for (never silent)."""
    if engine_available and isinstance(verdict, dict):
        return "judge: ran (verdict=%s)" % verdict.get("verdict")
    reason = (verdict.get("unavailable_reason") if isinstance(verdict, dict) else "") or "理由不明"
    return "judge: not run (engine unavailable: %s)" % reason


def _write_judge_result(output_folder, *, claim_id, statuses, footer, verdict,
                        engine_available, order_record, design_check=None,
                        design_feasibility=None) -> str:
    """Write the authoritative judge record next to the job output and return a short pointer.

    Stores judge_result.json = {status, engine_available, engine_status, verdict-or-reason,
    order (trusted times + order_ok), lot diagnosis lines, full footer}. Returns a one-line
    pointer ('judge: recorded -> judge_result.json | <first footer line>') for outcome.json so
    the worker's own text is never taken as the judge result. An existing harness-written
    judge_result.json is NOT overwritten. Returns '' when there is no folder to write into, so
    the caller falls back to the full text.
    """
    if not output_folder:
        return ""
    engine_status = _engine_status_line(verdict, engine_available)
    # Which registry supplied the claim: 'engine configs (human pre-registered)' or
    # 'worker-registered via harness registry'. Recorded whether or not the engine ran, so the
    # durable record names the claim source. For a worker-registered claim, append the trusted
    # registration time from the order record so the record reads
    # 'worker-registered via harness registry (trusted registration time <t>)'.
    claim_source = verdict.get("claim_source") if isinstance(verdict, dict) else None
    if (claim_source == "worker-registered via harness registry"
            and isinstance(order_record, dict) and order_record.get("registered_at")):
        claim_source = ("worker-registered via harness registry "
                        "(trusted registration time %s)" % order_record["registered_at"])
    # A post-hoc (late) registration is surfaced as a FIRST-CLASS field in the durable record,
    # not only inside the footer text, so a reader of judge_result.json sees order_ok=False was a
    # post-hoc claim (engine rule: D_FAIL_LOCK_AFTER_DATA, HOLD cap) rather than mistaking it for
    # the ERROR the engine returned for a separate reason (an unmapped claim). Never faked.
    post_hoc = post_hoc_order_note(order_record)
    # DESIGN FORCE-HOLD (this slice). When the upstream design-feasibility check says
    # the comparison design is infeasible, record the forced hold under a SEPARATE key
    # and set a top-level `final_display`, WITHOUT touching the engine's own `verdict`.
    # A reader of judge_result.json thus sees both the forced final display and the
    # untouched engine output. Stricter-only: a feasible/missing design leaves
    # final_display as the engine's verdict token and adds no hold record.
    hold = design_feasibility_hold(design_feasibility)
    engine_token = verdict.get("verdict") if (engine_available and isinstance(verdict, dict)) else None
    final_display = DESIGN_HOLD_DISPLAY if hold is not None else engine_token
    record = {
        "status": "requested",
        "claim_id": claim_id,
        "claim_source": claim_source,
        "engine_available": bool(engine_available),
        "engine_status": engine_status,
        "verdict": verdict if engine_available else None,
        "unavailable_reason": (None if engine_available
                               else (verdict.get("unavailable_reason")
                                     if isinstance(verdict, dict) else None)),
        "order": order_record,
        "post_hoc": post_hoc or None,
        "design_check": design_check,
        "design_feasibility_hold": hold,
        "final_display": final_display,
        "statuses": list(statuses),
        "footer": footer,
        "source": "harness",
    }
    path = os.path.join(output_folder, "judge_result.json")
    # Do not clobber a harness-written record already on disk. But DO carry forward a
    # 'corrections' history left by _record_correction: a harness-correction record (written each
    # bounded correction turn) is NOT a harness verdict, so it would otherwise be overwritten --
    # and the record of how many times the request was sent back, and why, would be lost. Reading
    # it here folds that history into the authoritative record the engine step writes.
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as f:
                existing = json.load(f)
            if isinstance(existing, dict):
                if existing.get("source") == "harness":
                    first = str(existing.get("engine_status") or "").splitlines()[0] if \
                        existing.get("engine_status") else engine_status
                    return "judge: recorded -> judge_result.json | " + first
                prior = existing.get("corrections")
                if isinstance(prior, list) and prior:
                    record["corrections"] = prior
        except (OSError, ValueError, TypeError):
            pass  # unreadable existing file: overwrite with a good one below
    try:
        with open(path, "wb") as f:
            f.write(json.dumps(record, ensure_ascii=False, indent=1,
                               sort_keys=True).encode("utf-8"))
    except OSError:
        return ""
    return "judge: recorded -> judge_result.json | " + engine_status


def judge_result_pointer(output_folder) -> str:
    """The one-line, harness-authored pointer to a job's judge_result.json, or ''.

    This is what outcome.json should record for a data-analysis job's judge result: it names the
    harness-written record file and carries its engine status line, so the durable reason never
    comes from text the worker produced. Returns '' when no harness record exists. The full
    footer and structured result live in judge_result.json, so nothing is lost to truncation.
    """
    if not output_folder:
        return ""
    path = os.path.join(output_folder, "judge_result.json")
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8-sig") as f:
            rec = json.load(f)
    except (OSError, ValueError, TypeError):
        return ""
    if not isinstance(rec, dict) or rec.get("source") != "harness":
        return ""
    status = str(rec.get("engine_status") or "").splitlines()[0]
    order = rec.get("order") if isinstance(rec.get("order"), dict) else {}
    order_bit = (" | order_ok=%s" % order.get("order_ok")) if order else ""
    return "judge: recorded -> judge_result.json | " + status + order_bit


def run_engine_judge(config_id: str, *, data_location: str | None = None,
                     fetched_at: str | None = None, root: str | None = None,
                     python_exe: str | None = None, timeout: int = 120,
                     registered_claim: dict | None = None,
                     judge_folder: str | None = None) -> dict:
    """Run the frozen engine's `judge` CLI for a claim and return its verdict JSON.

    This READS (A): it shells `python -m validity judge --claim <path> --root . --out
    results/tool_runs [--lock lock.json]` with the engine checkout as cwd, exactly as the skill
    prescribes. It changes nothing in (A) -- it NEVER writes to or edits the engine's configs/,
    lock.json or any file inside the engine root.

    CLAIM SOURCE (two registries that used to never meet). The claim can come from either place:
      * engine configs (human pre-registered): ENGINE_ROOT/configs/<id>.json, placed and locked
        by the engine owner per PREREGISTRATION. If it exists it WINS -- a human-registered,
        locked claim is always preferred and used exactly as before.
      * worker-registered via the harness registry: when configs/<id>.json is ABSENT but the
        judge request carried the registered claim (`registered_claim`), we write that claim to a
        temporary file OUTSIDE the engine root (under `judge_folder`, the per-job judge folder)
        and pass its ABSOLUTE path as --claim. The engine root is never touched. The engine CLI
        accepts an arbitrary --claim path, so the worker's claim reaches the engine this way.

    LOCK. The engine does not require the claim to be present in its lock.json: lock verification
    (validity/lock.py verify_lock) covers the engine's own configs*/ and validity/ files and the
    freeze commit; a claim file outside the engine root is not part of locked_paths, so passing a
    temp claim neither adds an "unlocked file present" failure nor requires the claim to be in the
    lock. When lock.json is present we still pass --lock so the engine applies its own D gate (a
    lock failure caps the verdict at HOLD inside the engine); we never weaken or fake that.

    On any failure it returns a dict with engine_available False and a reason that NAMES what to
    fix, so the caller can surface WHY on the final answer rather than omit it. The returned dict
    also carries `claim_source` describing which registry supplied the claim.
    """
    root = root or ENGINE_ROOT
    if not os.path.isdir(root):
        return {"engine_available": False,
                "unavailable_reason":
                    f"engine root not found: {root} "
                    f"(set the VALIDITY_ENGINE_ROOT environment variable to the engine "
                    f"checkout, or place the engine at this path)"}
    cfg = os.path.join(root, "configs", f"{config_id}.json")
    claim_from_configs = os.path.isfile(cfg)
    claim_path = cfg
    claim_source = "engine configs (human pre-registered)"
    temp_claim_path = None
    if not claim_from_configs:
        # No human-pre-registered claim. Fall back to the worker's registered claim, if the
        # judge request carried one, by writing it OUTSIDE the engine root and pointing --claim
        # at that absolute path. Never create configs/<id>.json in the engine root.
        if not isinstance(registered_claim, dict) or not registered_claim:
            registry_hint = (os.path.join(judge_folder, "registry", f"{config_id}.json")
                             if judge_folder else f"<job>/registry/{config_id}.json")
            return {"engine_available": False,
                    "claim_source": None,
                    "unavailable_reason":
                        f"registered claim not found for id {config_id}. "
                        f"Tried the engine configs path ({cfg}) and the harness registry "
                        f"({registry_hint}). To fix: have the engine owner pre-register and lock "
                        f"configs/{config_id}.json in the engine root, or include the registered "
                        f"claim in the judge request so the harness can pass it to the engine."}
        if not judge_folder or not os.path.isdir(judge_folder):
            return {"engine_available": False,
                    "claim_source": None,
                    "unavailable_reason":
                        "a worker-registered claim was supplied but there is no per-job judge "
                        "folder to write the temporary claim into outside the engine root; "
                        "cannot pass the claim to the engine without writing inside the engine "
                        "root (which is read-only)"}
        if registered_claim.get("id") != config_id:
            return {"engine_available": False,
                    "claim_source": None,
                    "unavailable_reason":
                        f"registered claim id {registered_claim.get('id')!r} does not match the "
                        f"requested claim id {config_id!r}"}
        try:
            temp_claim_path = os.path.abspath(
                os.path.join(judge_folder, f"engine_claim_{config_id}.json"))
            with open(temp_claim_path, "wb") as f:
                f.write(json.dumps(registered_claim, ensure_ascii=False,
                                   sort_keys=True, indent=1).encode("utf-8"))
        except (OSError, ValueError, TypeError) as e:
            return {"engine_available": False, "claim_source": None,
                    "unavailable_reason": f"could not stage the registered claim: {e}"}
        claim_path = temp_claim_path
        claim_source = "worker-registered via harness registry"
    if data_location is not None and claim_from_configs:
        # Human-pre-registered claim path: the frozen engine obtains fetch time from its manifest,
        # not from a CLI option. Never run against a different dataset or silently substitute
        # another fetch time. (A worker-registered claim's data lives outside the engine root and
        # is not in the engine manifest, so this manifest check applies only to configs claims;
        # for a worker claim we pass --data directly below.)
        try:
            with open(os.path.join(root, "data", "manifest.json"), encoding="utf-8-sig") as f:
                manifest = json.load(f)
            with open(cfg, encoding="utf-8-sig") as f:
                claim = json.load(f)
            entries = [manifest.get("datasets", {}).get(claim.get("dataset_id"))]
            matches = [e for e in entries if isinstance(e, dict)
                       and os.path.normcase(os.path.abspath(os.path.join(root, e.get("path", ""))))
                       == os.path.normcase(os.path.abspath(data_location))
                       and e.get("fetched_at") == fetched_at]
            if not matches:
                return {"engine_available": False, "claim_source": claim_source,
                        "unavailable_reason":
                        "requested data and fetch time are not recorded in the engine manifest"}
        except (OSError, ValueError, TypeError, AttributeError) as e:
            return {"engine_available": False, "claim_source": claim_source,
                    "unavailable_reason": f"manifest unavailable: {e}"}
    py = python_exe or sys.executable
    claim_arg = f"configs/{config_id}.json" if claim_from_configs else claim_path
    args = [py, "-m", "validity", "judge", "--claim", claim_arg,
            "--root", ".", "--out", "results/tool_runs"]
    if data_location is not None:
        args += ["--data", data_location]
    if os.path.isfile(os.path.join(root, "lock.json")):
        args += ["--lock", "lock.json"]
    try:
        # Unattended by construction: the engine's `judge` CLI reads files and prints JSON; it
        # never prompts (no console I/O). So we ask for headless creationflags BY NAME -- without
        # them a parent that has no console (wscript/WPF/scheduled task) would make Windows hand
        # the child a brand-new console that Windows Terminal then shows. See
        # tools.childproc.headless_creationflags (its docstring: not safe for anything that prompts).
        from tools.childproc import headless_creationflags
        p = subprocess.run(args, cwd=root, capture_output=True, text=True,
                           encoding="utf-8", timeout=timeout,
                           creationflags=headless_creationflags())
    except subprocess.TimeoutExpired:
        return {"engine_available": False, "claim_source": claim_source,
                "unavailable_reason": f"engine timed out after {timeout}s"}
    except OSError as e:
        return {"engine_available": False, "claim_source": claim_source,
                "unavailable_reason": f"engine exec failed: {e}"}
    last = ""
    for line in (p.stdout or "").splitlines():
        if line.strip():
            last = line.strip()
    try:
        verdict = json.loads(last)
    except (ValueError, TypeError):
        return {"engine_available": False, "claim_source": claim_source,
                "unavailable_reason": "engine produced no JSON verdict on stdout",
                "stderr_tail": (p.stderr or "")[-400:]}
    verdict["engine_available"] = True
    verdict["claim_source"] = claim_source
    return verdict


def _fmt_interval(ev: dict) -> str:
    for k in ("ci_std", "ci", "interval"):
        if k in ev and ev[k] is not None:
            return f"{k}={ev[k]}"
    return "区間=未返却"


def _fmt_n(ev: dict) -> str:
    parts = []
    for k in ("K", "df", "n", "loo_n"):
        if k in ev and ev[k] is not None:
            parts.append(f"{k}={ev[k]}")
    return ", ".join(parts) if parts else "n=未返却"


# --- DESIGN-FEASIBILITY FORCE-HOLD (this slice) ------------------------------
#
# WHY THIS EXISTS, AND HOW IT DIFFERS FROM design_check. A SEPARATE, info-only
# `design_check` footer section (feat/judge-footer-design-check) ADDS what it saw
# about the comparison design but changes nothing about the verdict. The owner
# (2026-11-04 07:45) said that is not enough: when groups being compared differ by
# PRODUCT (the deployment's product/stratum column; see .config/domain_terms.json "product_label") rather than by the exposure under study, the comparison is not
# a comparison at all (equipment-vs-product cannot be separated), and that must be
# judged AUTOMATICALLY -- not noticed by a human later. So this path does more than
# inform: when an UPSTREAM design-feasibility check (produced WITHOUT reading the
# response/outcome column -- feat/design-feasibility-core) reports the design is
# INFEASIBLE, the FINAL DISPLAY is forced to DESIGN_HOLD_DISPLAY regardless of what the
# engine verdict was.
#
# STRICTER-ONLY, AND NON-DESTRUCTIVE. This can only turn a deliverable answer INTO a
# hold; it never turns a hold into GO and never relaxes anything. The engine's own
# output is PRESERVED UNCHANGED -- the footer still prints the original verdict block
# below the hold banner (labelled 参考・変更なし), and _write_judge_result keeps the
# original `verdict` key intact while recording the hold under the SEPARATE key
# `design_feasibility_hold` plus a top-level `final_display`. So a reader always sees
# both the forced final display and the untouched engine verdict.
#
# INDEPENDENCE. This consumes a plain dict (the design_feasibility RESULT), it does
# not import or recompute the upstream check. The dict shape it reads:
#   {"feasible": bool,                 # False => force a hold
#    "reason_code": str,               # e.g. one of _DESIGN_INFEASIBLE_REASONS
#    "common_strata": int,             # number of common strata (grouping levels)
#    "lots_per_group": {group: int},   # 群ごとのロット数
#    "overlap_coefficient": number,    # 群間の層構成の重なり係数
#    ...}                              # any other keys are carried as `seen`
# Only `feasible is False` forces the hold; a missing/True/None design never does,
# so wiring this in cannot flip an otherwise-deliverable answer by accident.

#: The reason codes this slice understands for an infeasible comparison design. Kept
#: as a catalogue so the footer/record can show a human-readable gloss next to the
#: machine code. An unknown code is still surfaced verbatim (never swallowed).
_DESIGN_INFEASIBLE_REASONS = {
    "STRATUM_COLUMN_ABSENT":
        "層別の候補列(製品・品種、ライン、期間など)が入力に無い",
    "NO_COMMON_STRATUM":
        "両群に共通の層(grouping level)が無い(群を分ける属性の差か設備の差か分けられない)",
    "INSUFFICIENT_LOTS_IN_COMMON_STRATA":
        "共通の層内で各群 k_min ロット以上を満たす層の数が下限未満",
    "GROUP_COMPOSITION_IMBALANCE":
        "群ごとの層構成の偏りが大きい(重なり係数が閾値未満)",
}

#: The single phrase the forced final display uses, matched elsewhere so the gate and
#: tests can pin it. The engine's own verdict tokens (GO/HOLD/NA) are NOT reused for
#: this -- a forced design hold is a HARNESS decision about the comparison's validity,
#: distinct from the engine's statistical verdict, and must read as such.
_DEFAULT_HOLD_DISPLAY = "DESIGN INFEASIBLE / 設計上比較不能（保留）"
DESIGN_HOLD_DISPLAY = str(load_domain_terms().get("hold_display") or _DEFAULT_HOLD_DISPLAY)


def design_feasibility_hold(design) -> dict | None:
    """Normalise a design_feasibility RESULT into a force-hold record, or None.

    Returns None (no forcing) unless `design` is a dict whose `feasible` is EXACTLY
    False -- a missing design, design is None, or feasible True/None never forces a
    hold (stricter-only: this can only add a hold, never remove one). When it does
    force, returns a data-free record naming WHAT WAS SEEN so the footer and the
    durable judge record can show it:
      {"forced": True, "reason_code": str, "reason_gloss": str,
       "common_strata": int|None, "lots_per_group": dict|None,
       "overlap_coefficient": number|None, "seen": {..other keys..}}
    Carries no measurement value -- only stratum counts, per-group lot counts and the
    overlap coefficient, which are design facts, not outcomes. Never raises.
    """
    if not isinstance(design, dict):
        return None
    if design.get("feasible") is not False:
        return None
    code = design.get("reason_code")
    code = str(code) if code is not None else "DESIGN_INFEASIBLE"
    gloss = _DESIGN_INFEASIBLE_REASONS.get(code, "")
    # Carry the design facts the owner asked to see named on the judge票: the number
    # of common strata, each group's lot count, and the overlap coefficient. Anything
    # else the upstream check reported is preserved under `seen` (minus the response
    # column, which the upstream check does not read and does not emit).
    known = {"feasible", "reason_code", "common_strata", "lots_per_group",
             "overlap_coefficient"}
    seen = {k: v for k, v in design.items() if k not in known}
    return {
        "forced": True,
        "reason_code": code,
        "reason_gloss": gloss,
        "common_strata": design.get("common_strata"),
        "lots_per_group": design.get("lots_per_group"),
        "overlap_coefficient": design.get("overlap_coefficient"),
        "seen": seen or None,
    }


def _design_hold_lines(hold: dict) -> list:
    """The footer lines for a forced design hold (banner + what was seen). Data-free."""
    code = hold.get("reason_code")
    gloss = hold.get("reason_gloss")
    head = "最終表示: %s" % DESIGN_HOLD_DISPLAY
    why = "理由コード: %s" % code + (("(%s)" % gloss) if gloss else "")
    lpg = hold.get("lots_per_group")
    if isinstance(lpg, dict) and lpg:
        lots = ", ".join("%s=%s" % (g, n) for g, n in sorted(lpg.items(), key=lambda kv: str(kv[0])))
    else:
        lots = "未返却"
    seen = ("見たもの: 共通層の数=%s, 各群のロット数{%s}, 重なり係数=%s"
            % (hold.get("common_strata"), lots, hold.get("overlap_coefficient")))
    split_by = str(load_domain_terms().get("group_split_by")
                   or "群を分ける属性 (product or equipment)")
    note = ("比較する群が曝露ではなく%sで分かれているため、群を分ける属性の差か設備の差か"
            "分けられません。結果(応答列)を読まずに設計だけで自動判定しています。" % split_by)
    return [head, why, seen, note]


def build_judge_footer(*, verdict: dict | None, engine_available: bool,
                       unavailable_reason: str = "", order_record: dict | None = None,
                       design_check: dict | None = None,
                       design_feasibility: dict | None = None) -> str:
    """Compose the block appended to a data-analysis final answer.

    When the engine ran: surfaces id / verdict (+ja) / finding / claim_supported / reason codes /
    notes / interval / n / input hash -- the fields the skill requires, verbatim from the engine.
    When it did NOT run: states that the judge did NOT run and WHY (never silently omitted).
    Language is bounded by the skill's (e): a verdict is 'judgeable', never 'proven'/'correct'.

    When a `design_check` result is supplied, its lines are added to BOTH branches
    (engine ran / engine did not run) because the design facts -- whether the analysis stratified,
    and whether the exposure groups held the product fixed -- are information the reader needs
    regardless of the engine verdict. The design check NEVER changes the verdict and NEVER
    loosens order_ok; it only adds information.

    DESIGN FORCE-HOLD. When `design_feasibility` reports the comparison
    design is INFEASIBLE (feasible is False), the FINAL DISPLAY is forced to
    DESIGN_HOLD_DISPLAY: the footer LEADS with a hold banner naming the reason code and
    what was seen (common strata, per-group lot counts, overlap coefficient), then
    prints the engine's ORIGINAL verdict block UNCHANGED below it, labelled
    参考・変更なし. Stricter-only; never edits the engine output. A feasible / missing /
    None design changes nothing.
    """
    hold = design_feasibility_hold(design_feasibility)
    lines = ["", "--- 判定器(少標本評価 validity_judge)の結果 ---"]
    if hold is not None:
        # Force the final display to a design hold BEFORE the engine block, so a reader
        # sees the forced verdict first. The engine's own block still follows, unchanged.
        lines.extend(_design_hold_lines(hold))
        lines.append("--- 判定器の元の出力(参考・変更なし) ---")
    if order_record is not None:
        lines.append(
            "登録→ロックの順序: order_ok=%s (registered_at=%s, data_fetched_at=%s)" % (
                order_record.get("order_ok"), order_record.get("registered_at"),
                order_record.get("data_fetched_at")))
        # Post-hoc (late) registration is NOT an input error: state plainly that the order check
        # cannot pass for data that already existed, that the engine's own rule caps such a claim
        # at HOLD (D_FAIL_LOCK_AFTER_DATA), and what would make it valid -- without faking order.
        _ph = post_hoc_order_note(order_record)
        if _ph:
            lines.append(_ph)
        # Confirmation-data independence (fixed condition 2). When the confirmation slice was
        # already opened by a prior analysis before registration, state it plainly and that
        # order_ok was set false for it -- never silently omitted, never faked.
        _indep = confirm_independence_note(order_record)
        if _indep:
            lines.append(_indep)
    # Result-blind design feasibility (PR #438 core). When the verdict carries per-group
    # stratum lot counts (group_stratum_lots: {group: {stratum: lot_count}}), run the
    # design_feasibility screen -- it never reads the response column and can only ADD a
    # design HOLD note, never relax a verdict. Stated plainly in the footer so a
    # design-infeasible contrast is visible even when the engine otherwise ran.
    if isinstance(verdict, dict) and verdict.get("group_stratum_lots"):
        _feas = design_feasibility(verdict.get("group_stratum_lots"))
        if _feas.get("design_infeasible"):
            _seen = _feas.get("seen") or {}
            lines.append(
                "設計の成立性: 設計不成立(保留) reason=%s "
                "(共通層=%s, 被覆層(各群>=%s)=%s, overlap=%.3f)" % (
                    _feas.get("reason_code"),
                    _seen.get("n_common_strata"), _seen.get("k_min"),
                    _seen.get("n_common_strata_covered"),
                    float(_seen.get("overlap_coefficient") or 0.0)))
    # Design check lines go here so they appear whether or not the engine ran below.
    lines.extend(format_design_check_footer(design_check))
    if not engine_available or not verdict:
        reason = unavailable_reason or (
            verdict.get("unavailable_reason") if isinstance(verdict, dict) else "") or "理由不明"
        lines.append("判定器は実行されませんでした(judge NOT run)。理由: " + str(reason))
        lines.append("※ この解析は判定器を通していません。品質判断に用いる前に人が判断器を通してください。")
        lines.append("--- ここまで ---")
        return "\n".join(lines)
    ev = verdict.get("evidence") or {}
    lines.append("ID: %s" % verdict.get("id"))
    lines.append("判定: %s(%s)  結果: %s  主張の支持: %s" % (
        verdict.get("verdict"), verdict.get("verdict_ja"),
        verdict.get("finding"), verdict.get("claim_supported")))
    reasons = verdict.get("reasons") or []
    notes = verdict.get("notes") or []
    lines.append("理由コード: %s" % (", ".join(map(str, reasons)) if reasons else "(なし)"))
    lines.append("付記: %s" % (", ".join(map(str, notes)) if notes else "(なし)"))
    lines.append("数値: %s, %s" % (_fmt_n(ev), _fmt_interval(ev)))
    hashes = verdict.get("hashes") or {}
    if hashes.get("input_sha256"):
        lines.append("input_sha256: %s" % hashes.get("input_sha256"))
    lines.append("(事前登録した規則に従って機械的に判定。「採用=正しい」ではない。)")
    lines.append("--- ここまで ---")
    return "\n".join(lines)


# --- the final-step GATE used by the relay loop ------------------------------
#
# WHY THIS LIVES HERE (and is a pure function). The relay loop (copilot_autopilot_relay.py)
# reaches a candidate DONE and, for a data-analysis goal, must confirm the final answer passed
# the judge BEFORE it accepts the DONE -- the owner task. Measured 2026-10-30: the 679かすれ
# analysis was delivered three times with no judge block and nothing caught it. The relay code
# is not unit-tested in isolation; this seam is, so the decision lives in a tested pure function
# and the loop only calls it.

#: Markers that show the final answer already carries a validity-judge result (either a real
#: verdict block from build_judge_footer, or an explicit statement of WHY the judge could not
#: run). Matched case-sensitively against the delivered text.
_JUDGE_BLOCK_MARKERS = (
    "判定器(少標本評価 validity_judge)の結果",   # build_judge_footer header
    "validity_judge",
    "判定器は実行されませんでした",                # the unavailable-reason line
    "judge NOT run",
)

#: Reason codes / verdict tokens that, when present, show a real engine verdict was surfaced.
_VERDICT_TOKENS = ("GO", "HOLD", "NA")


def judge_block_present(final_response) -> bool:
    """True when the delivered answer already carries a judge result OR a stated reason it could
    not run. Either one satisfies the gate: the owner forbids SILENT omission, not an honest
    'the judge could not run because ...'."""
    if not isinstance(final_response, str) or not final_response.strip():
        return False
    return any(m in final_response for m in _JUDGE_BLOCK_MARKERS)


def needs_judge_rework(goal, final_response) -> bool:
    """True when `goal` is data analysis AND its final answer carries no judge block/reason.

    This is the single predicate the relay loop consults at candidate-DONE time. It is False for
    non-analysis goals (so nothing changes for them) and False once the answer carries the block
    or an explicit 'could not run because ...'.
    """
    if not is_data_analysis_goal(goal):
        return False
    return not judge_block_present(final_response)


#: Job text sent back to the implementer when a data-analysis answer is missing the judge block.
#: It asks for the three steps and -- per the skill -- forbids inventing a verdict: if the judge
#: cannot run, the answer must SAY WHY, not omit it.
REWORK_JOB = (
    "このゴールはDB照会(database / sql / query / odbc / table 等)の結果を表・相関・群間比較にするデータ解析の仕事です。"
    "最終回答に、少標本評価の判定器(validity_judge)の結果が載っていません。次の工程を通し、"
    "結果を最終回答に追記してください:\n"
    "(i) 解析結果とデータのsha256・取得時刻を記録 (tools/validity_harness.record_manifest)\n"
    "(ii) 主張を登録してロックし、登録→取得の時刻順を記録 (tools/validity_harness.register_and_lock)\n"
    "(iii) 判定エンジンにかけ、判定・理由コード・区間・n を最終回答に載せる\n"
    "判定器が使えない場合(未登録ID・エンジン不在・タイムアウト等)は、その理由を最終回答に明記し、"
    "黙って省略しないこと。規則・閾値・既存の登録主張は変えないこと。判定を自作・捏造しないこと。"
)


def rework_job_text(goal="", final_response="") -> str:
    """The job handed back to the implementer to make it pass the judge. Constant text; the
    arguments are accepted so a caller can log what triggered it."""
    return REWORK_JOB


# ---------------------------------------------------------------------------
# Design-feasibility gate (result-blind).  Design rationale: the gate must judge the
# soundness of a two-group comparison's design automatically and without reading the
# response column, so that swapping the response cannot change the verdict.
#
# This is the CORE pure function for that gate.  It takes, PER EXPOSURE GROUP, the
# breakdown of how many LOTS fall into each stratum level (product, line, period).
# It NEVER takes the response column
# and NEVER reads any measurement value -- the whole point is that the design is
# judged blind to the result, so swapping the response column cannot change the
# verdict.
#
# It returns design_infeasible=True with a reason code when any of the
# three conditions holds:
#   NO_STRATIFIER             (a) no stratifier breakdown was supplied at all
#   NO_COMMON_STRATUM         (b) the two groups share no stratum level
#   INSUFFICIENT_LOTS_IN_COMMON (b) too few common strata have >= k_min lots in BOTH groups
#   HIGH_SKEW                 (c) the groups' stratum mixes barely overlap (Overlap < min)
#
# Overlap coefficient here = sum_level min(p_g1(level), p_g2(level)) over the union
# of levels, where p is the within-group lot share.  Overlap is 1.0 when the two
# groups have identical stratum mixes and 0.0 when they share no level -- it is the
# complement, on shared mass, of the total-variation distance used by the harness
# screen (tools/confound_check.py); kept here as its own measure so the gate reads
# its own number.  k_min / delta_min / corrections are NOT changed by this gate and
# it only ever makes the display STRICTER (adds a HOLD-for-design), never looser.
#
# HERMETIC: standard library only; pure; no file / network / OS access.

#: Minimum lots a stratum must have IN EACH GROUP to count as "covered" in common.
#: This is the design gate's own k_min for stratum coverage; it is 8 to match the
#: engine's independent-lot floor and is NOT the engine's k_min (which is
#: untouched).  Raising the bar is allowed; this gate never lowers any threshold.
DESIGN_K_MIN: int = 8

#: At least this many common strata must each clear DESIGN_K_MIN in BOTH groups for
#: a stratified estimate to be possible.  One shared, well-covered product is the
#: minimum on which a within-stratum comparison can even be formed.
DESIGN_MIN_COMMON_STRATA: int = 1

#: Overlap coefficient at or below this is reported as HIGH_SKEW (the groups' mixes
#: barely overlap, so an unstratified contrast is product-vs-equipment confounded).
#: 0.5 means the groups share at most half their mass; chosen as a visible, round
#: screening line and flagged empirical so it can be tuned without pretending it is
#: derived.  It is a STRICTER-side screen: it can only add a design HOLD.
DESIGN_OVERLAP_MIN: float = 0.5
DESIGN_OVERLAP_MIN_IS_EMPIRICAL: bool = True


def _overlap_coefficient(counts_a, counts_b) -> float:
    """Overlap = sum_level min(p_a(level), p_b(level)) over the union of levels.

    Each side is normalised to sum 1 over its own total.  Returns a value in [0, 1]:
    1.0 when the two mixes are identical, 0.0 when they share no level.  An all-zero
    side yields 0.0 against any non-empty side (nothing shared), and 0.0 against
    another all-zero side (no mass to overlap -- reported as no overlap, the
    stricter reading).
    """
    levels = set(counts_a or {}) | set(counts_b or {})
    ta = float(sum(max(0.0, float(v)) for v in (counts_a or {}).values()))
    tb = float(sum(max(0.0, float(v)) for v in (counts_b or {}).values()))
    if ta <= 0.0 or tb <= 0.0:
        return 0.0
    s = 0.0
    for lv in levels:
        pa = max(0.0, float((counts_a or {}).get(lv, 0.0))) / ta
        pb = max(0.0, float((counts_b or {}).get(lv, 0.0))) / tb
        s += min(pa, pb)
    return s


def design_feasibility(group_stratum_lots,
                       *,
                       k_min: int = DESIGN_K_MIN,
                       min_common_strata: int = DESIGN_MIN_COMMON_STRATA,
                       overlap_min: float = DESIGN_OVERLAP_MIN):
    """Decide, WITHOUT reading the response column, whether a two-group exposure
    comparison's design is sound enough to estimate.

    Argument
      group_stratum_lots: mapping {group_label: {stratum_level: lot_count}} for
        EXACTLY TWO exposure groups.  Each inner map says, for that group, how many
        LOTS fell into each stratum level (one stratifier, e.g. product, line, or
        period).  The
        counts are lot counts, never measurements.  An empty outer map, an outer map
        without two groups, or two groups whose inner maps are both empty is treated
        as having no stratifier information (NO_STRATIFIER).

    Keyword (all defaulted; a caller may pass STRICTER values, never looser -- the
    function does not enforce that, but the gate's contract is stricter-or-equal):
      k_min: lots a stratum needs IN EACH GROUP to count as covered (default 8).
      min_common_strata: how many such covered common strata are required (default 1).
      overlap_min: Overlap coefficient below which the mix is HIGH_SKEW (default 0.5).

    Returns a dict:
      design_infeasible: bool
      reason_code: one of NO_STRATIFIER / NO_COMMON_STRATUM /
                   INSUFFICIENT_LOTS_IN_COMMON / HIGH_SKEW / None (feasible)
      seen: {
        groups: [label, label],
        group_lots: {label: total lots},
        n_common_strata: int,              # levels present in BOTH groups
        n_common_strata_covered: int,      # common levels with >= k_min lots in BOTH
        overlap_coefficient: float,        # Overlap = sum min(p1,p2)
        k_min: int, min_common_strata: int, overlap_min: float,
      }

    The checks are applied in the order (a) then (b) then (c); the FIRST
    failing one sets the reason code.  design_infeasible True means the final
    display must be forced to "設計不成立(保留)" -- this function never relaxes a
    verdict, it only ever adds a design HOLD.
    """
    gsl = group_stratum_lots or {}
    groups = list(gsl.keys())

    # (a) no stratifier information at all: not two groups, or both sides empty.
    def _is_empty(d):
        return not d or sum(max(0, int(v)) for v in d.values()) <= 0
    if len(groups) != 2 or all(_is_empty(gsl.get(g) or {}) for g in groups):
        seen = {
            "groups": groups,
            "group_lots": {g: int(sum(max(0, int(v)) for v in (gsl.get(g) or {}).values()))
                           for g in groups},
            "n_common_strata": 0,
            "n_common_strata_covered": 0,
            "overlap_coefficient": 0.0,
            "k_min": int(k_min),
            "min_common_strata": int(min_common_strata),
            "overlap_min": float(overlap_min),
        }
        return {"design_infeasible": True, "reason_code": "NO_STRATIFIER", "seen": seen}

    g1, g2 = groups[0], groups[1]
    a = {k: max(0, int(v)) for k, v in (gsl.get(g1) or {}).items()}
    b = {k: max(0, int(v)) for k, v in (gsl.get(g2) or {}).items()}
    common = [lv for lv in (set(a) & set(b))
              if a.get(lv, 0) > 0 and b.get(lv, 0) > 0]
    covered = [lv for lv in common if a.get(lv, 0) >= k_min and b.get(lv, 0) >= k_min]
    overlap = _overlap_coefficient(a, b)

    seen = {
        "groups": [g1, g2],
        "group_lots": {g1: int(sum(a.values())), g2: int(sum(b.values()))},
        "n_common_strata": len(common),
        "n_common_strata_covered": len(covered),
        "overlap_coefficient": float(overlap),
        "k_min": int(k_min),
        "min_common_strata": int(min_common_strata),
        "overlap_min": float(overlap_min),
    }

    # (b) no common stratum at all, or too few covered to the required depth.
    if len(common) == 0:
        return {"design_infeasible": True, "reason_code": "NO_COMMON_STRATUM", "seen": seen}
    if len(covered) < int(min_common_strata):
        return {"design_infeasible": True,
                "reason_code": "INSUFFICIENT_LOTS_IN_COMMON", "seen": seen}

    # (c) the groups' stratum mixes barely overlap -> confounded, hold for design.
    if overlap < float(overlap_min):
        return {"design_infeasible": True, "reason_code": "HIGH_SKEW", "seen": seen}

    return {"design_infeasible": False, "reason_code": None, "seen": seen}
