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
