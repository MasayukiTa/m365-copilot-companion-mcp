# -*- coding: utf-8 -*-
"""`propose_org_change`: file a change proposal for a role or protocol definition.

PROPOSAL ONLY. The tool records what is proposed (relay/org/proposals.py) and queues a decision
card; it applies nothing, and no prompt, role or admission reads what it writes. With the setting
`org_proposals` off (the default) it returns `refused` and writes nothing.

The author is not an argument: it is the worker the calling session is placed under, decided the
way tools/board_tool.py decides the campaign. The result is a short JSON string.
"""
from __future__ import annotations

import json


def propose_org_change(target: str, rationale: str, proposed_text: str, base_hash: str = "") -> str:
    """Propose replacing one role or protocol definition (for a person to decide; nothing is applied).

    target         "role:<name>" or "workflow:<name>" (an unknown name is refused and the known
                   ones are listed in the reply)
    rationale      why, at most 400 characters, one line
    proposed_text  the full replacement text (at most 20000 characters)
    base_hash      optional: the definition's current hash, to refuse a proposal made against an
                   older text

    Returns JSON {"status": "queued"|"duplicate"|"refused"|"error", "reason": ...}. Refused when
    the org_proposals setting is off. Read-write only to its own proposals log and the decision
    queue; it cannot change the definition."""
    try:
        from relay.org import proposals
        from tools.board_tool import _caller
        if proposals.mode() != "on":                 # before anything is looked up or written
            return json.dumps({"status": proposals.REFUSED, "reason": "org_proposals is off"})
        cid, worker = _caller()
        res = proposals.submit(target, rationale, proposed_text, worker or "unplaced",
                               campaign=cid, base_hash=base_hash)
        return json.dumps(res, ensure_ascii=False)
    except Exception:
        return json.dumps({"status": "error", "reason": "the proposal could not be recorded"})
