# -*- coding: utf-8 -*-
"""validity_harness -- NON-SECRET orchestration around the (external, frozen) validity engine.

WHY THIS EXISTS (owner decision 2026-10-30 07:40). Whether to freeze the six registered
claims (MCL_D1/D3/D4/K1/P1/P2) is left to the agent; and if past analysis results are in the
sessions DB, the harness should be able to read them. The 679-kasure v6 report (欠け#3) found
the engine's D gate could not line up the lock time with the round-4 data fetch time because
the data was absent from the engine manifest, and that every claim carried
LOT_INDEPENDENCE_NOT_CHECKED.

WHAT THIS FILE IS, AND IS NOT. The verdict engine itself lives OUTSIDE this public repository
(Desktop/AHA資料_参照一式/証明/公開データ検証/engine_v1_8c2), is a SECRET folder, has no git
remote, and is byte-identical-locked by PREREGISTRATION_v1_8c2.md -- so its code must not be
edited and its secret data must not be copied into this public repo. This module therefore
holds only the parts that can live in the open:

  (c) past_runs()         -- read distilled past verdicts from the sessions DB (no raw rows);
  (a) record_manifest()   -- write the data + result sha256 into a manifest at analysis time;
  (b) register_and_lock()  -- register a claim and lock it, recording the register-vs-fetch
                             time order so the D gate can check it;
      lot_independence_check() -- a DIAGNOSTIC for LOT_INDEPENDENCE_NOT_CHECKED. It NEVER
                             changes a verdict or a threshold; it only reports.

Data minimisation: no raw measurement row and no employee id is ever read or returned. The
reader surfaces only id / verdict / reasons / notes / hashes / timestamps.

The engine's rules, thresholds (delta_min, k_min, ...) and claim files are NOT touched here.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import sqlite3

__all__ = ["past_runs", "record_manifest", "register_and_lock", "lot_independence_check"]

_ALLOWED_RUN_FIELDS = ("claim_id", "run_id", "verdict", "finding", "reasons", "notes",
                       "hashes", "ts")


def _default_store_path() -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, ".fleet", "sessions", "sessions.sqlite3")


def _sha_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _canon_bytes(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _parse_iso(s: str) -> _dt.datetime:
    t = _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone(_dt.timedelta(hours=9)))  # local JST assumed
    return t


# ---------------------------------------------------------------------------- (c)
def past_runs(claim_id: str, store_path: str | None = None, limit: int = 200) -> list[dict]:
    """Distilled past verdict records for a claim id, newest last, read from the sessions DB.

    Reads the `validity_audit` table (see bridge/validity_audit.py) and returns, for every
    tool_result row of a validity_judge call on this claim, only the non-sensitive fields the
    engine's verdict JSON carried (id/verdict/finding/reasons/notes/hashes). Raw conversation
    text and any other column are dropped. Returns [] for an unknown claim or a missing table.
    """
    path = store_path or _default_store_path()
    if not os.path.isfile(path):
        return []
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        out = _read_from_validity_audit(conn, claim_id, limit)
        if out:
            return out
        # FALLBACK to the raw past-analysis table the ledger is distilled FROM.
        # validity_audit is a normalised view over fleet_turns + tool_events; if a
        # claim's verdict was never distilled into it (or the table is absent), the
        # past results still live in fleet_turns, which the owner decision names as
        # the DB the harness must read. We read it here with the same allow-list.
        return _read_from_fleet_turns(conn, claim_id, limit)
    finally:
        conn.close()


def _distill_payload(rec: dict, text) -> dict:
    """Pull the allow-listed verdict fields out of one row's text into rec.

    text may be the engine's verdict JSON (a dict) or a bare verdict token such as
    "GO"/"HOLD"/"NA" written by a fleet_turns `verdict` row. Both are accepted; raw
    conversation text is never surfaced -- only the allow-listed fields survive.
    """
    try:
        payload = json.loads(text or "{}")
    except (ValueError, TypeError):
        payload = {}
    if isinstance(payload, dict) and payload:
        for k in ("verdict", "finding", "reasons", "notes", "hashes"):
            if k in payload:
                rec[k] = payload[k]
    elif isinstance(text, str):
        token = text.strip()
        # a short bare verdict token only (GO/HOLD/NA/...); never a conversation blob
        if token and len(token) <= 24 and "verdict" not in rec:
            rec["verdict"] = token
    return {k: v for k, v in rec.items() if k in _ALLOWED_RUN_FIELDS}


def _read_from_validity_audit(conn, claim_id: str, limit: int) -> list[dict]:
    """Distilled verdicts from the normalised validity_audit ledger.

    Reads both the engine `tool_result` JSON and any fleet_turns-sourced `verdict`
    row, so a final verdict recorded only as a conversation turn is not missed.
    """
    try:
        cur = conn.execute(
            "SELECT claim_id, run_id, ts, text, role_tag FROM validity_audit "
            "WHERE claim_id = ? AND role_tag IN ('tool_result','verdict') "
            "ORDER BY ts, seq LIMIT ?",
            (str(claim_id), int(limit)))
    except sqlite3.OperationalError:
        return []
    out: list[dict] = []
    for row in cur:
        rec = {"claim_id": row["claim_id"], "run_id": row["run_id"], "ts": row["ts"]}
        out.append(_distill_payload(rec, row["text"]))
    return out


def _read_from_fleet_turns(conn, claim_id: str, limit: int) -> list[dict]:
    """Fallback: read past verdicts straight from fleet_turns (the raw past-analysis
    table). validity_audit is derived from this table; when a verdict was not
    distilled, it is still here. The claim id appears as the turn's goal/key; we
    accept a `verdict`/`outcome` role or a text that names the claim, and apply the
    SAME allow-list so no raw conversation blob is surfaced.
    """
    for sql, args in (
        ("SELECT goal_id AS cid, key AS run_id, ts, text, role FROM fleet_turns "
         "WHERE role IN ('verdict','outcome') AND "
         "(goal LIKE ? OR text LIKE ?) ORDER BY ts LIMIT ?",
         ("%" + str(claim_id) + "%", "%" + str(claim_id) + "%", int(limit))),
    ):
        try:
            cur = conn.execute(sql, args)
        except sqlite3.OperationalError:
            return []
        out: list[dict] = []
        for row in cur:
            rec = {"claim_id": str(claim_id),
                   "run_id": row["run_id"], "ts": row["ts"]}
            out.append(_distill_payload(rec, row["text"]))
        if out:
            return out
    return []


# ---------------------------------------------------------------------------- (a)
def record_manifest(manifest_path: str, *, dataset_id: str, data_path: str, result: dict,
                    fetched_at: str | None = None) -> dict:
    """Record the sha256 of the data file and of the verdict result into a manifest, at run time.

    This is what the engine's D gate needs but never wrote itself: data/manifest.json gains an
    entry {path, sha256, result_sha256, fetched_at} per dataset_id. Idempotent per dataset_id
    (an existing entry is replaced). Returns the entry. No raw data row is stored -- only hashes.
    """
    if not os.path.isfile(data_path):
        raise FileNotFoundError(data_path)
    fetched_at = fetched_at or _dt.datetime.now(
        _dt.timezone(_dt.timedelta(hours=9))).isoformat()
    entry = {
        "path": os.path.relpath(data_path, os.path.dirname(os.path.abspath(manifest_path)))
        .replace("\\", "/"),
        "sha256": _sha_file(data_path),
        "result_sha256": hashlib.sha256(_canon_bytes(result)).hexdigest(),
        "fetched_at": fetched_at,
    }
    man: dict = {"datasets": {}}
    if os.path.isfile(manifest_path):
        try:
            man = json.loads(open(manifest_path, encoding="utf-8-sig").read())
        except (ValueError, OSError):
            man = {"datasets": {}}
    if not isinstance(man.get("datasets"), dict):
        man["datasets"] = {}
    man["datasets"][dataset_id] = entry
    os.makedirs(os.path.dirname(os.path.abspath(manifest_path)) or ".", exist_ok=True)
    with open(manifest_path, "wb") as f:
        f.write(json.dumps(man, sort_keys=True, indent=1, ensure_ascii=False).encode("utf-8"))
    return entry


# ---------------------------------------------------------------------------- (b)
def _file_mtime_iso(path: str) -> str | None:
    """The data file's modification time as an ISO string (JST), or None when there is no file.

    This is the lower bound of when the data provably EXISTED: the engine's D gate needs the
    registration to precede the data, and a worker-declared fetch time is not evidence of that.
    The filesystem mtime is.
    """
    if not path or not os.path.isfile(path):
        return None
    ts = os.path.getmtime(path)
    return _dt.datetime.fromtimestamp(
        ts, _dt.timezone(_dt.timedelta(hours=9))).isoformat()


def register_and_lock(root: str, *, claim: dict, data_fetched_at: str,
                      registered_at: str | None = None, data_location: str | None = None,
                      trusted: bool = True) -> dict:
    """Register a claim and record the lock, capturing a TRUSTED register-vs-fetch time order.

    Writes <root>/registry/<id>.json (the claim as registered) and appends a line to
    <root>/registry/lock_log.jsonl with the claim sha256 and the times.

    WHY THIS NO LONGER TRUSTS THE WORKER'S CLOCK (欠け 2026-11-01). The old version used the
    `registered_at` the worker wrote and set `order_ok` to a bare string compare of two values
    the worker also wrote -- so a backdated registration passed with nothing checking it. Now:

      * registered_at is STAMPED HERE from the system clock at the moment the registration is
        recorded (trusted=True, the default). A worker-supplied value is kept only as
        `declared_registered_at`, shown beside the trusted one and NEVER used for order_ok.
      * the data's existence is checked against EVIDENCE: when data_location is a file, its
        modification time (`data_file_mtime`) is the lower bound of when the data existed. A
        declared data_fetched_at EARLIER than that mtime is impossible and is flagged
        `fetched_at_inconsistent=True`.
      * order_ok is true only when the trusted registration time is strictly BEFORE the data
        existed -- i.e. before the file mtime (and, when consistent, before the declared fetch
        time). If the registration happens AFTER the data file's mtime, order_ok is false and
        `order_reason` says so.

    The engine's verdict rules, thresholds and claim files are untouched; this only records a
    trustworthy order. Set trusted=False to fall back to the declared registered_at (kept for
    callers that have an independently trusted time source of their own).
    """
    declared_registered_at = registered_at
    now_iso = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=9))).isoformat()
    if trusted or not registered_at:
        registered_at = now_iso
    cid = str(claim.get("id") or "UNKNOWN")
    reg_dir = os.path.join(root, "registry")
    os.makedirs(reg_dir, exist_ok=True)
    claim_sha = hashlib.sha256(_canon_bytes(claim)).hexdigest()
    with open(os.path.join(reg_dir, f"{cid}.json"), "wb") as f:
        f.write(json.dumps(claim, sort_keys=True, indent=1, ensure_ascii=False).encode("utf-8"))

    data_file_mtime = _file_mtime_iso(data_location)
    reg_dt = _parse_iso(registered_at)
    declared_fetch_dt = _parse_iso(data_fetched_at)
    fetched_at_inconsistent = False
    if data_file_mtime is not None:
        mtime_dt = _parse_iso(data_file_mtime)
        # A declared fetch earlier than the file's mtime is impossible: the file existed by
        # its mtime, so it could not have been fetched before then. Report, do not accept.
        fetched_at_inconsistent = declared_fetch_dt < mtime_dt
        # The data provably existed by its mtime; registration must precede that.
        order_ok = reg_dt < mtime_dt
        if order_ok:
            order_reason = ("trusted registration precedes the data file mtime "
                            "(data existed no earlier than its mtime)")
        else:
            order_reason = ("trusted registration is NOT before the data file mtime: "
                            "the data already existed when the claim was registered")
        if fetched_at_inconsistent:
            order_reason += ("; declared fetched_at is earlier than the data file mtime "
                             "(inconsistent -- declared value not used for order_ok)")
    else:
        # No file evidence available: fall back to comparing trusted registration with the
        # declared fetch time, and say plainly that this leg rests on a declared value.
        order_ok = reg_dt < declared_fetch_dt
        order_reason = ("no data file mtime available; order_ok rests on the declared "
                        "data_fetched_at compared with the trusted registration time")
    rec = {"id": cid, "claim_sha256": claim_sha, "registered_at": registered_at,
           "declared_registered_at": declared_registered_at,
           "registered_at_trusted": bool(trusted or not declared_registered_at),
           "data_fetched_at": data_fetched_at, "data_file_mtime": data_file_mtime,
           "fetched_at_inconsistent": bool(fetched_at_inconsistent),
           "order_ok": bool(order_ok), "order_reason": order_reason, "lock_recorded": True}
    with open(os.path.join(reg_dir, "lock_log.jsonl"), "ab") as f:
        f.write((json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
    return rec


# ---------------------------------------------------------------------------- diagnostic
def lot_independence_check(*, y, x, lot) -> dict:
    """DIAGNOSTIC for LOT_INDEPENDENCE_NOT_CHECKED. Reports whether the exposure can be told
    apart from the lot; it NEVER changes a verdict or a threshold.

    Reports:
      - exposure_constant_within_all_lots: every lot has a single exposure value (between-lot).
      - each_level_single_lot: for a 0/1 exposure, each level appears in exactly one lot
        (perfect alias of exposure with lot -> not identified without more lots).
      - independent: a coarse flag = NOT (constant-within AND each-level-single-lot). This is a
        necessary-condition screen, not a test of the random-effect independence assumption,
        which (per the skill) is a DECLARATION that data cannot verify.
    """
    import numpy as np
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    lot = np.asarray(lot)
    _, lot_codes = np.unique(lot, return_inverse=True)
    K = int(lot_codes.max()) + 1 if len(lot_codes) else 0
    # exposure constant within each lot?
    const_within = True
    for k in range(K):
        xs = x[lot_codes == k]
        if len(xs) and (xs.max() - xs.min()) > 0:
            const_within = False
            break
    # for a binary exposure, does each level sit in exactly one lot?
    levels = np.unique(x)
    each_level_single_lot = None
    if set(levels.tolist()) <= {0.0, 1.0}:
        lots_per_level = {lv: len(np.unique(lot_codes[x == lv])) for lv in levels}
        each_level_single_lot = all(v <= 1 for v in lots_per_level.values()) and len(levels) >= 2
    independent = not (const_within and bool(each_level_single_lot))
    return {"checked": True, "K": K, "exposure_constant_within_all_lots": bool(const_within),
            "each_level_single_lot": each_level_single_lot, "independent": bool(independent),
            "note": "diagnostic only; does not change any verdict or threshold; "
                    "lot_effect_model independence is a declaration data cannot verify"}
