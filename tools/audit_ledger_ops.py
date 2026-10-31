# -*- coding: utf-8 -*-
"""validity_audit_ledger -- read the audit ledger of the validity tools for one claim id.

Read-only. Returns every conversation row bound to the claim (goal, worker prompt and reply, tool
call and result, reviewer prompt and reply, verdict, outcome), tagged by role, oldest first. The
size is bounded by parameters and anything left out is COUNTED in the reply (`omitted_rows`,
`text_cut_to`), never dropped silently. See bridge/validity_audit.py for the role tags.
"""
from __future__ import annotations

import json


def validity_audit_ledger(claim_id: str = "", limit: int = 200, max_chars: int = 20000) -> str:
    """Audit ledger for a claim id, as JSON. claim_id='' lists the claim ids that have rows.

    claim_id: a pre-registered analysis id (e.g. 'SS1'), or 'UNATTRIBUTED' for conversations whose
    claim could not be identified. limit: most rows returned. max_chars: most text characters
    returned in total (a row cut to fit says so).
    """
    try:
        from bridge import session_store as S
        if not str(claim_id or "").strip():
            return json.dumps({"claims": S.validity_audit_claims()}, ensure_ascii=False)
        return json.dumps(S.validity_audit_ledger(str(claim_id).strip(), limit=limit,
                                                  max_chars=max_chars), ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": "%s: %s" % (type(exc).__name__, str(exc)[:300])},
                          ensure_ascii=False)


def validity_past_runs(config_id: str = "", limit: int = 200, store_path: str = "") -> str:
    """Past validity VERDICTS for a pre-registered claim id, as JSON, for use as judge-flow input.

    This is the public wiring that lets a judging agent feed the past-analysis DB into its
    decision: call it with the claim id (e.g. 'MCL_D1') before/alongside validity_judge to see
    whether that claim was already judged and what the verdict was. It reads the sessions DB
    (validity_audit, falling back to fleet_turns -- the raw past-analysis table the ledger is
    distilled from), and returns ONLY the distilled, allow-listed verdict fields
    (id/verdict/finding/reasons/notes/hashes/ts). No raw conversation row, measurement or
    employee id is ever surfaced (same data-minimisation guarantee as past_runs()).

    config_id: the analysis id. limit: most records. store_path: override the DB path (tests).
    Returns {"claim_id": <id>, "runs": [...]} or {"error": ...}.
    """
    try:
        from tools.validity_harness import past_runs
        cid = str(config_id or "").strip()
        if not cid:
            return json.dumps({"error": "config_id is required", "code": "BAD_ARGS"},
                              ensure_ascii=False)
        runs = past_runs(cid, store_path=(store_path or None), limit=int(limit))
        return json.dumps({"claim_id": cid, "runs": runs}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": "%s: %s" % (type(exc).__name__, str(exc)[:300])},
                          ensure_ascii=False)
