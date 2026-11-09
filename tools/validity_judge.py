# -*- coding: utf-8 -*-
"""validity_judge -- small-sample validity judge rebuilt from the proof-side canon.

NON-SECRET. Written independently from the canon (SPEC_v1 family + 改訂C1〜C6 +
`ツール依存可能な主張一覧.md` §0/§1/§2/§5). No code is copied from the frozen engine
(engine_v1_8c2), which is read-only in the invalid folder; this is an independent
re-implementation of the *rules*, not of that code.

What the canon requires and this module implements:
  (1) Claim must declare the §0 twelve items; a missing declaration is INPUT_MISSING_KEY
      (the claim is unusable -> at most HOLD / N/A with a reason code).
  (2) C-rules used directly: C1 (lot is the unit, K = lots), C3 (two-stage t, df K-2 / K-1),
      C4 (no bootstrap main interval), C5 (HOLD vs N/A boundary), C6 (support), C7 (time
      collinearity), C8 (pure loading), C9 (effect type declared), C10 (Bonferroni family).
      D-items appear only with a 'diagnostic' mark.
  (3) Order gate: a preregistration not strictly before the data (order_ok False) caps at HOLD.
  (4) delta_min: only a value declared before seeing the data (source == preregistration).
  (5) verdict/finding consistency: a finding is reported only on GO; a capped verdict never
      carries a GO finding (CAPPED_FINDING_* note instead).
  (6) §5 footer: claim ids + ledger rows used, declaration-not-testable note, interval
      method/df/K, diagnostic / empirical-threshold marks.
  (7) Context gate (ahead of the numeric path): same-product only (else COMPARISON_INVALID),
      self-evident comparison stop (TRIVIAL_COMPARISON), confirmable N count, input validity.
  (8) Negative control: effect-zero permutation must not produce GO+EFFECT.

Forbidden words (§3) are never emitted. Verdicts are GO/HOLD/NA/ERROR only.
"""
from __future__ import annotations

import hashlib
import json
import math

# ----------------------------------------------------------------- declarations (§0)
# The twelve items that every usable claim must declare (ツール依存可能な主張一覧.md §0).
DECL_ITEMS = (
    "lot_effect_model",   # random_independent | fixed_or_correlated  (C3,C4,C5; T64 not testable)
    "exposure_level",     # between_lot | within_lot                   (C3 df, C5)
    "distribution",       # gaussian | non_gaussian                    (C3)
    "balance",            # balanced | unbalanced                      (C3)
    "unit_column",        # independent-unit (lot) column name         (C1,C2,C3)
    "K",                  # number of lots                             (C1,C2,C3)
    "effect_type",        # A (stay) | B (drive); fixed before data    (C9)
    "time_unit",          # time unit of q                             (C12)
    "family",             # {id, m}; fixed before data                 (C10)
    "split_unit",         # lot (only lot is dependable)               (C11)
    "support",            # observed design cells / exposure range     (C6)
    "axis0",              # (M1)(M2)(M4)(M5)(M6) declarations          (C8; D1-D3)
    "mode",               # confirmatory | exploratory                 (C10,C11)
)

LOT_EFFECT_MODELS = ("random_independent", "fixed_or_correlated")
EXPOSURE_LEVELS = ("between_lot", "within_lot")
DISTRIBUTIONS = ("gaussian", "non_gaussian")
BALANCES = ("balanced", "unbalanced")
EFFECT_TYPES = ("A", "B")
MODES = ("confirmatory", "exploratory")

VERDICT_JA = {"GO": "\u63a1\u7528", "HOLD": "\u4fdd\u7559", "NA": "\u8b58\u5225\u4e0d\u80fd",
              "ERROR": "\u5165\u529b\u4e0d\u5099"}

# Reason codes, grouped by stage (precedence top to bottom).
INPUT_CODES = ("INPUT_MISSING_KEY", "INPUT_SCHEMA_ERROR", "INPUT_COLUMN_MISSING",
               "INPUT_GENERIC_ERR", "INPUT_LOT_DUPLICATE", "INPUT_SHA_MISMATCH")
CONTEXT_CODES = ("CTX_COMPARISON_INVALID_CROSS_PRODUCT", "CTX_TRIVIAL_COMPARISON",
                 "CTX_N_INSUFFICIENT", "CTX_INPUT_INVALID")
NA_CODES = ("NA_NO_VARIATION", "NA_FIXED_LOT_BETWEEN_EXPOSURE", "NA_PURE_LOADING",
            "NA_TIME_ALIAS", "NA_OUT_OF_SUPPORT")
A2_CODES = ("A_FAIL_SPARSE_LOTS", "A_FAIL_SINGLE_LOT_CELL", "A_FAIL_LOW_DF")
D_CODES = ("D_FAIL_GENERIC", "D_FAIL_ORDER", "D_FAIL_EXPLORATORY",
          "D_FAIL_POSTHOC_FAMILY", "D_FAIL_SPLIT_INDEPENDENCE_UNKNOWN")
C_CODES = ("C_FAIL_STRADDLE", "C_NOT_COMPUTABLE")
OK_CODES = ("A_OK", "D_OK", "C_OK", "CTX_OK")
FINDINGS = ("EFFECT", "EFFECT_OPPOSITE", "EFFECT_BELOW_MDES", "EFFECT_GENERIC")

# ----------------------------------------------------------------- evidence-rank gate (§5-5)
# IMPORTANT: these R_* codes are the *evidence rank* gate of canon §5 item 5
# ("a verdict resting only on D-group claims or ledger state B-or-below defaults to HOLD").
# They are NOT the D_FAIL_* order/lock failure codes above (dcap). Do not conflate the two:
#   - D_FAIL_* / dcap  = the preregistration order/lock gate (order_gate).
#   - R_* / evidence   = the *rank* of the claims cited as grounds (diagnostic D-group /
#                        ledger status), per 主張一覧.md §1 (C1-C16, state A) vs §2 (D1-D10).
R_CODES = ("R_ONLY_DIAGNOSTIC_D", "R_ONLY_LEDGER_B_OR_BELOW", "R_EVIDENCE_UNKNOWN")

# The diagnostic D-group (主張一覧.md §2, 106-120): usable only with a 'diagnostic' mark.
D_GROUP_IDS = frozenset("D%d" % i for i in range(1, 11))
# Direct-use claims (主張一覧.md §1): C1-C16. Direct use requires ledger state A (+ proof R).
C_GROUP_IDS = frozenset("C%d" % i for i in range(1, 17))
# Ledger states, ordered A > A-B > B > B-C > C > D (主張一覧.md line 4 basis; 1of6 record).
# "B or below" = anything that is not pure state A (B, C, D, and B-containing mixes A-B/B-C).
_LEDGER_A_ONLY = frozenset({"A"})


def _ledger_is_b_or_below(state) -> bool:
    """True if a ledger state counts as 'B or below' (not pure A). Unknown -> True (fail-safe)."""
    if state is None:
        return True
    s = str(state).strip().upper()
    if not s:
        return True
    return s not in _LEDGER_A_ONLY


def evidence_rank_gate(evidence) -> list:
    """Canon §5-5 evidence-rank cap. Returns R_* codes that force a default HOLD (empty = clear).

    `evidence` describes the claims cited as grounds for the verdict:
        {"claims": [{"id": "C3", "ledger_state": "A"}, {"id": "D4", "ledger_state": "B"}, ...]}
    A verdict defaults to HOLD when its grounds are *only* D-group diagnostics, or *only*
    ledger state B-or-below -- i.e. there is no single upper-rank ground (a C-group claim in
    ledger state A). Missing / malformed / unknown-id evidence is fail-safe: R_EVIDENCE_UNKNOWN
    (do NOT advance to GO on unknown grounds; canon: an undeclared assumption means the claim
    is unusable -> at most HOLD).

    This gate never *raises* a verdict; it can only cap a would-be GO down to HOLD.
    """
    if evidence is None:
        # Caller opted out of rank gating (back-compatible). No cap.
        return []
    if not isinstance(evidence, dict):
        return ["R_EVIDENCE_UNKNOWN"]
    claims = evidence.get("claims")
    if not isinstance(claims, list) or not claims:
        # Grounds required but none supplied -> cannot certify an upper-rank ground.
        return ["R_EVIDENCE_UNKNOWN"]
    has_unknown = False
    has_upper = False          # a C-group claim in ledger state A (a genuine upper-rank ground)
    all_diagnostic_d = True    # every cited claim is a D-group diagnostic
    all_b_or_below = True       # every cited claim is ledger state B-or-below
    for item in claims:
        if not isinstance(item, dict):
            has_unknown = True
            continue
        cid = item.get("id")
        state = item.get("ledger_state")
        if not isinstance(cid, str) or cid not in (D_GROUP_IDS | C_GROUP_IDS):
            has_unknown = True
            continue
        is_d = cid in D_GROUP_IDS
        if not is_d:
            all_diagnostic_d = False
        b_below = _ledger_is_b_or_below(state)
        if not b_below:
            all_b_or_below = False
        # An upper-rank ground = a direct-use C claim whose ledger state is A.
        if (not is_d) and (not b_below):
            has_upper = True
    if has_unknown:
        # Any unknown/unparseable ground -> cannot prove an upper-rank ground exists.
        return ["R_EVIDENCE_UNKNOWN"]
    if has_upper:
        # At least one C-group, ledger-A ground exists -> §5-5 does not force HOLD.
        return []
    codes = []
    if all_diagnostic_d:
        codes.append("R_ONLY_DIAGNOSTIC_D")
    if all_b_or_below:
        codes.append("R_ONLY_LEDGER_B_OR_BELOW")
    if not codes:
        # No upper-rank ground, yet neither "all D" nor "all B-below" -> still cannot certify
        # an A-ranked direct-use ground. Fail-safe to HOLD rather than GO.
        codes.append("R_EVIDENCE_UNKNOWN")
    return codes


class ClaimError(Exception):
    def __init__(self, codes, messages):
        super().__init__("; ".join(messages))
        self.codes = sorted(set(codes))
        self.messages = messages


def _canon_bytes(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def claim_sha256(claim: dict) -> str:
    return hashlib.sha256(_canon_bytes(claim)).hexdigest()


def validate_claim(raw: dict) -> dict:
    """Check the §0 twelve declarations and the preregistered delta_min (D4). Raise ClaimError.

    A missing or out-of-vocabulary declaration is INPUT_MISSING_KEY / INPUT_SCHEMA_ERROR, which
    makes the claim unusable (the judge then can only HOLD / NA, never GO).
    """
    codes, msgs = [], []

    def bad(code, msg):
        codes.append(code)
        msgs.append(msg)

    if not isinstance(raw, dict):
        raise ClaimError(["INPUT_SCHEMA_ERROR"], ["claim must be an object"])
    for k in DECL_ITEMS:
        if k not in raw:
            bad("INPUT_MISSING_KEY", "missing declaration: %s" % k)
    if "id" not in raw:
        bad("INPUT_MISSING_KEY", "missing declaration: id")
    if "product" not in raw:
        bad("INPUT_MISSING_KEY", "missing declaration: product")
    if "delta_min" not in raw:
        bad("INPUT_MISSING_KEY", "missing declaration: delta_min")
    if codes:
        raise ClaimError(codes, msgs)

    en=(
        ("lot_effect_model", LOT_EFFECT_MODELS), ("exposure_level", EXPOSURE_LEVELS),
        ("distribution", DISTRIBUTIONS), ("balance", BALANCES),
        ("effect_type", EFFECT_TYPES), ("mode", MODES))
    for key, allowed in en:
        if raw[key] not in allowed:
            bad("INPUT_SCHEMA_ERROR", "%s must be one of %s" % (key, allowed))
    if not (isinstance(raw["K"], int) and not isinstance(raw["K"], bool) and raw["K"] >= 0):
        bad("INPUT_SCHEMA_ERROR", "K must be a non-negative integer")
    if not isinstance(raw["unit_column"], str) or not raw["unit_column"]:
        bad("INPUT_SCHEMA_ERROR", "unit_column must be a non-empty string")
    fam = raw["family"]
    if not (isinstance(fam, dict) and isinstance(fam.get("id"), str)
            and isinstance(fam.get("m"), int) and not isinstance(fam.get("m"), bool)
            and fam["m"] >= 1):
        bad("INPUT_SCHEMA_ERROR", "family must be {id: str, m: int >= 1} (C10)")
    if raw["split_unit"] != "lot":
        bad("INPUT_SCHEMA_ERROR", "split_unit must be 'lot' (C11: only lot is dependable)")
    dm = raw["delta_min"]
    if not (isinstance(dm, dict) and isinstance(dm.get("value"), (int, float))
            and not isinstance(dm.get("value"), bool) and math.isfinite(dm["value"])
            and dm["value"] > 0):
        bad("INPUT_SCHEMA_ERROR", "delta_min.value must be a number > 0")
    elif dm.get("source") != "preregistration":
        # D4: only a value declared before the data may be used.
        bad("INPUT_SCHEMA_ERROR", "delta_min.source must be 'preregistration' (D4)")
    elif not isinstance(dm.get("rationale"), str) or not dm["rationale"].strip():
        bad("INPUT_SCHEMA_ERROR", "delta_min.rationale required (D4)")
    if codes:
        raise ClaimError(codes, msgs)
    return raw


# ----------------------------------------------------------------- context gate (§7)
def context_gate(claim: dict, data: dict) -> list:
    """AI-side context checks, run BEFORE the numeric path. Returns a list of CONTEXT_CODES
    (empty = passes). Each stop records a human-readable reason in the output.

    (a) same product only: a cross-product comparison is COMPARISON_INVALID.
    (b) self-evident by definition (e.g. setup lots are fully scrapped so defects are higher;
        the defect definition IS the exposure): TRIVIAL_COMPARISON.
    (c) confirmable N: number of lots carrying an outcome record; if below k_min -> N_INSUFFICIENT.
    (d) input validity: unit column present, no duplicate lot ids, units consistent.
    """
    out = []
    # (d) input validity first -- a malformed input makes the rest meaningless.
    if not data.get("input_valid", True):
        out.append("CTX_INPUT_INVALID")
        return out
    # (a) same product
    prods = data.get("products")
    if prods is not None and len({str(p) for p in prods}) > 1:
        out.append("CTX_COMPARISON_INVALID_CROSS_PRODUCT")
    if data.get("claim_product") is not None and data.get("data_product") is not None \
            and str(claim.get("product")) != str(data["data_product"]):
        out.append("CTX_COMPARISON_INVALID_CROSS_PRODUCT")
    # (b) self-evident / trivial comparison (declared by the reading AI)
    if data.get("trivial_comparison"):
        out.append("CTX_TRIVIAL_COMPARISON")
    # (c) confirmable N (lots with an outcome record)
    n_conf = data.get("n_confirmable_lots")
    if n_conf is not None and n_conf < int(claim.get("k_min", 3)):
        out.append("CTX_N_INSUFFICIENT")
    return out


# ----------------------------------------------------------------- order gate (§3)
def order_gate(order_ok, lock_present) -> list:
    """D-cap codes from the preregistration order. order_ok must be True (registration strictly
    before the data existed) and a lock must be present; otherwise cap at HOLD."""
    codes = []
    if not lock_present:
        codes.append("D_FAIL_GENERIC")
    if order_ok is not True:
        codes.append("D_FAIL_ORDER")
    return codes


# ----------------------------------------------------------------- exploratory cap (C10/C11)
def exploratory_cap(claim: dict) -> list:
    """Claim-intrinsic HOLD caps from the canon's exploratory rules (C10, C11).

    The canon is explicit that exploration is 'at most HOLD' (主張一覧.md L22, L79, L83):
      - C10 (L77-79): a family (set + size m) fixed BEFORE the data gives Bonferroni FWER<=alpha;
        a family decided AFTER the data does not (27 cells -> top 9 -> FWER=0.139). A scan with no
        declared family + hash is exploratory (mode=exploratory) and caps at HOLD.
      - C11 (L81-83): split-sample confirmation is valid only when discovery and confirmation sides
        are INDEPENDENT (separate independent lots, no shared component). Splitting products of the
        SAME lot is invalid (误确认率 0.38). split_unit='lot' is necessary but not sufficient; the
        independence of the two sides must itself be asserted.

    Safe-side policy (do NOT presume an unknown preregistration or split-independence to be a fact):
      - mode == 'exploratory'                 -> D_FAIL_EXPLORATORY (always; GO is impossible).
      - family explicitly marked post-hoc     -> D_FAIL_POSTHOC_FAMILY
        (family['preregistered'] is False, or family['decided'] == 'posthoc').
      - split explicitly NOT independent      -> D_FAIL_SPLIT_INDEPENDENCE_UNKNOWN
        (claim['split_independent'] is False, or claim['split_independence'] == 'unknown').

    When family['preregistered'] / split_independent are simply ABSENT, this function does not
    fabricate either outcome: a confirmatory claim is taken at its declared word (mode), and the
    footer records that the family-preregistration flag was not asserted and is not data-testable.
    That reliance on the declaration is noted as a limitation, not treated as a verified fact.
    """
    caps = []
    if claim.get("mode") == "exploratory":
        caps.append("D_FAIL_EXPLORATORY")
    fam = claim.get("family")
    if isinstance(fam, dict) and (fam.get("preregistered") is False
                                  or fam.get("decided") == "posthoc"):
        caps.append("D_FAIL_POSTHOC_FAMILY")
    if claim.get("split_independent") is False \
            or claim.get("split_independence") == "unknown":
        caps.append("D_FAIL_SPLIT_INDEPENDENCE_UNKNOWN")
    return caps


# ----------------------------------------------------------------- finding (§5 classify)
def classify(lo: float, hi: float, d: float):
    """Map an aligned standardized interval [lo, hi] and delta_min d to a finding (None=straddle)."""
    if lo > d:
        return "EFFECT"
    if hi < -d:
        return "EFFECT_OPPOSITE"
    if lo >= -d and hi <= d:
        return "EFFECT_GENERIC" if lo <= 0.0 <= hi else "EFFECT_BELOW_MDES"
    return None


def decide(claim: dict, structure: dict, interval: dict, context: list, dcap: list,
           *, evidence=None, require_evidence=False):
    """Pure ordered decision. Returns (verdict, finding, reasons, notes).

    Precedence: ERROR -> CONTEXT (stop) -> NA -> HOLD(A2) -> D cap(<=HOLD) -> C
                -> evidence-rank cap(§5-5, <=HOLD) -> GO.
    verdict/finding consistency: a finding rides out only on GO; under any cap the finding
    becomes a CAPPED_FINDING_* note and verdict carries no GO.

    `evidence` (optional, keyword-only) feeds the §5-5 evidence-rank gate. When omitted
    (None), rank gating is skipped and behaviour is identical to before (back-compatible).
    When supplied, a would-be GO resting only on D-group diagnostics or ledger state
    B-or-below (or on unknown grounds) is capped to HOLD with an R_* reason. The rank gate
    is distinct from dcap (D_FAIL_* order/lock codes) and is evaluated only on the GO path.

    `require_evidence` (keyword-only, default False) closes the back-compatibility gap: when
    True, a GO path with evidence=None (no grounds cited) is NOT allowed to skip the gate --
    it is treated as R_EVIDENCE_UNKNOWN and capped to HOLD. Canon §4/§5-5: a verdict with no
    declared ground is at most HOLD. Operational GO paths MUST set require_evidence=True;
    the default stays False so the pure 5-arg call is unchanged for non-opted-in callers.
    """
    notes = ["LOT_EFFECT_MODEL_DECLARED_NOT_TESTABLE"]  # §5: data cannot verify this
    # C10/C11: exploration is at most HOLD. Fold claim-intrinsic exploratory caps into the
    # D cap so a scan (mode=exploratory), a post-hoc family, or a non-independent split can
    # never reach GO. Unknown preregistration / split-independence is NOT presumed to be a fact.
    dcap = sorted(set(list(dcap or []) + exploratory_cap(claim)))
    if claim.get("mode") == "exploratory" or any(
            c in dcap for c in ("D_FAIL_EXPLORATORY", "D_FAIL_POSTHOC_FAMILY",
                                "D_FAIL_SPLIT_INDEPENDENCE_UNKNOWN")):
        notes.append("EXPLORATORY_CAP_AT_MOST_HOLD")  # §5: C10/C11 cap recorded
    if isinstance(claim.get("family"), dict) \
            and claim["family"].get("preregistered") is not True \
            and claim.get("mode") != "exploratory":
        # family preregistration flag not asserted -> relied on the declared mode; note the limit.
        notes.append("FAMILY_PREREGISTRATION_NOT_ASSERTED_RELIED_ON_MODE")
    errs = sorted(set(structure.get("input_errors") or []))
    if errs:
        return "ERROR", None, errs, notes
    if context:
        # context stop: never GO. COMPARISON_INVALID / TRIVIAL are N/A-like identification
        # stops; N_INSUFFICIENT is a HOLD (same design, more lots would resolve it).
        if "CTX_N_INSUFFICIENT" in context and not (
                set(context) & {"CTX_COMPARISON_INVALID_CROSS_PRODUCT",
                                "CTX_TRIVIAL_COMPARISON", "CTX_INPUT_INVALID"}):
            return "HOLD", None, list(context), notes
        return "NA", None, list(context), notes

    na = []
    if structure.get("response_constant") or structure.get("exposure_constant"):
        na.append("NA_NO_VARIATION")
    if claim["lot_effect_model"] == "fixed_or_correlated" and claim["exposure_level"] == "between_lot":
        na.append("NA_FIXED_LOT_BETWEEN_EXPOSURE")
    tg = structure.get("time_gate") or {}
    if tg.get("pure_loading"):
        na.append("NA_PURE_LOADING")
    if tg.get("alias"):
        na.append("NA_TIME_ALIAS")
    if structure.get("out_of_support"):
        na.append("NA_OUT_OF_SUPPORT")
    if na:
        return "NA", None, na + dcap, notes

    a = claim.get("assumptions") or {}
    exact = (claim["balance"] == "balanced" and claim["distribution"] == "gaussian"
             and structure.get("balanced", claim["balance"] == "balanced"))
    notes.append("C3_EXACT_UNDER_DECLARED_ASSUMPTIONS" if exact else "C3_ASYMPTOTIC_ONLY")

    K = int(interval.get("K") or 0)
    k_min = int(claim.get("k_min", 3))
    a2 = []
    if K < k_min:
        a2.append("A_FAIL_SPARSE_LOTS")
    mlpl = structure.get("min_lots_per_level")
    if mlpl is not None and mlpl < 2:
        a2.append("A_FAIL_SINGLE_LOT_CELL")
    df = interval.get("df")
    if df is None or df < 2:
        a2.append("A_FAIL_LOW_DF")
    if a2:
        return "HOLD", None, a2 + dcap, notes

    d = float(claim["delta_min"]["value"])
    cb = []
    finding = None
    if not interval.get("computable"):
        cb.append("C_NOT_COMPUTABLE")
    else:
        finding = classify(float(interval["lo_std"]), float(interval["hi_std"]), d)
        if finding is None:
            cb.append("C_FAIL_STRADDLE")
    notes.append("LOLO_IS_DIAGNOSTIC_NOT_TEST")  # C14: LOLO is a diagnostic, not a test
    if dcap:
        if finding is not None and not cb:
            notes.append("CAPPED_FINDING_%s" % finding)
        return "HOLD", None, dcap + cb, notes
    if cb:
        return "HOLD", None, cb, notes
    # §5-5 evidence-rank cap: a GO resting only on D-group / ledger-B-or-below / unknown
    # grounds defaults to HOLD. Evaluated last, on the GO path only; never raises a verdict.
    #
    # Canon §4 (line 4) / §5-5 (line 157): "宣言が無ければ、その主張は使えない（判定は最大でも
    # 保留）". A GO *must* cite an upper-rank ground; it cannot rest on no grounds at all.
    # evidence=None is a back-compatibility opt-out ONLY. On any operational GO path the caller
    # MUST pass require_evidence=True, which turns a missing/None evidence into R_EVIDENCE_UNKNOWN
    # (HOLD) instead of silently skipping the gate. This closes the "existing 5-arg call reaches
    # GO with the §5-5 gate disabled" gap while keeping the pure 5-arg call behaviour unchanged
    # for callers that have not opted in.
    gate_input = evidence
    if require_evidence and evidence is None:
        gate_input = {}  # no grounds supplied -> evidence_rank_gate returns R_EVIDENCE_UNKNOWN
    rcap = evidence_rank_gate(gate_input)
    if rcap:
        if finding is not None:
            notes.append("CAPPED_FINDING_%s" % finding)
        return "HOLD", None, rcap, notes
    return "GO", finding, list(OK_CODES), notes


# ----------------------------------------------------------------- two-stage estimator (C3)
def two_stage_interval(lot_means, lot_exposure, *, level, alpha, m, sd_response,
                       contrast=1.0, direction="positive", lot_slopes=None):
    """C3 two-stage lot-level t interval, standardized and direction-aligned.

    between_lot: OLS of lot means on lot exposure, df = K-2 (改訂C1 命題1.1).
    within_lot: mean of per-lot slopes, se = sd/sqrt(K), df = K-1 (改訂C1 命題1.2).
    Bonferroni over the declared family: level 1 - alpha/m (C10). No bootstrap (C4).
    """
    from statistics import mean, fmean  # stdlib only; no numpy dependency for the core
    out = {"computable": False, "method": None, "K": 0, "df": None,
           "lo_std": None, "hi_std": None, "estimate_std": None,
           "alpha_fam": alpha / m, "not_computable_reason": None}
    if sd_response is None or sd_response <= 0:
        out["not_computable_reason"] = "zero or undefined response SD"
        return out
    sign = 1 if direction != "negative" else -1
    if level == "between_lot":
        ys, xs = list(map(float, lot_means)), list(map(float, lot_exposure))
        K = len(ys)
        out["method"] = "between_lot_ols_on_lot_means_t_K-2"
        out["K"] = K
        if K < 3:
            out["df"] = K - 2
            out["not_computable_reason"] = "residual df < 1"
            return out
        xbar = fmean(xs)
        xc = [x - xbar for x in xs]
        sxx = sum(v * v for v in xc)
        if sxx <= 0:
            out["not_computable_reason"] = "no exposure variation across lots"
            return out
        ybar = fmean(ys)
        slope = sum(c * (y - ybar) for c, y in zip(xc, ys)) / sxx
        resid = [y - ybar - slope * c for c, y in zip(xc, ys)]
        rss = sum(r * r for r in resid)
        df = K - 2
        se = math.sqrt(rss / df / sxx) if rss > 0 else 0.0
    else:
        if lot_slopes is None:
            out["not_computable_reason"] = "within_lot requires per-lot slopes"
            return out
        bs = [float(b) for b in lot_slopes]
        K = len(bs)
        out["method"] = "within_lot_two_stage_mean_slope_t_K-1"
        out["K"] = K
        if K < 2:
            out["df"] = K - 1
            out["not_computable_reason"] = "residual df < 1"
            return out
        slope = fmean(bs)
        var = sum((b - slope) ** 2 for b in bs) / (K - 1)
        se = math.sqrt(var / K)
        df = K - 1
    out["df"] = df
    est = sign * slope * contrast / sd_response
    out["estimate_std"] = est
    if se <= 0 or df < 1:
        out["not_computable_reason"] = "zero residual variance"
        return out
    tcrit = _t_ppf(1.0 - (alpha / m) / 2.0, df)
    lo_raw, hi_raw = slope - tcrit * se, slope + tcrit * se
    a_, b_ = sign * lo_raw * contrast / sd_response, sign * hi_raw * contrast / sd_response
    out["lo_std"], out["hi_std"] = (min(a_, b_), max(a_, b_))
    out["computable"] = True
    out["tcrit"] = tcrit
    return out


def _t_ppf(p: float, df: int) -> float:
    """Inverse Student-t CDF via a bisection on a continued-fraction CDF (no scipy dependency)."""
    lo, hi = -1e4, 1e4
    for _ in range(200):
        mid = (lo + hi) / 2
        if _t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _t_cdf(t: float, df: int) -> float:
    x = df / (df + t * t)
    ib = _betainc(df / 2.0, 0.5, x)
    return 1.0 - 0.5 * ib if t > 0 else 0.5 * ib


def _betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a,b) by Lentz's continued fraction."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(math.log(x) * a + math.log(1 - x) * b - lbeta) / a
    f, c, d = 1.0, 1.0, 0.0
    for i in range(0, 300):
        m = i // 2
        if i == 0:
            num = 1.0
        elif i % 2 == 0:
            num = (m * (b - m) * x) / ((a + 2 * m - 1) * (a + 2 * m))
        else:
            num = -((a + m) * (a + b + m) * x) / ((a + 2 * m) * (a + 2 * m + 1))
        d = 1.0 + num * d
        if abs(d) < 1e-30:
            d = 1e-30
        d = 1.0 / d
        c = 1.0 + num / c
        if abs(c) < 1e-30:
            c = 1e-30
        f *= d * c
        if abs(1.0 - d * c) < 1e-12:
            break
    return front * (f - 1.0)


# ----------------------------------------------------------------- §5 footer
def footer(claim: dict, interval: dict, verdict: str, reasons: list, notes: list,
           claim_ids_used, ledger_rows) -> dict:
    """Build the §5 co-statement: claim ids + ledger rows, the not-testable declarations,
    interval method/df/K, and the diagnostic / empirical-threshold marks.
    """
    empirical = [n for n in notes if "EMPIRICAL" in n or "THRESHOLD" in n]
    diag = [n for n in notes if "DIAGNOSTIC" in n or n.startswith("LOLO_IS_DIAGNOSTIC")]
    return {
        "claims_used": list(claim_ids_used),
        "ledger_rows": list(ledger_rows),
        "declarations_not_data_testable": ["lot_effect_model", "axis0"],
        "interval_method": interval.get("method"),
        "df": interval.get("df"),
        "K": interval.get("K"),
        "diagnostic_marks": diag,
        "empirical_threshold_marks": empirical,
        # §5-5 declaration. Still always-True as the *policy statement* that such grounds
        # default to HOLD; the enforcement lives in decide()'s evidence_rank_gate.
        "default_hold_if_only_D_or_B_below": True,
    }


__all__ = ["DECL_ITEMS", "VERDICT_JA", "validate_claim", "ClaimError", "claim_sha256",
           "context_gate", "order_gate", "exploratory_cap", "classify", "decide",
           "two_stage_interval", "footer", "_t_ppf", "_t_cdf",
           "evidence_rank_gate", "R_CODES", "D_GROUP_IDS", "C_GROUP_IDS"]
