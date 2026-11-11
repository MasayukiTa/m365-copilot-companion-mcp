# -*- coding: utf-8 -*-
"""Peer work requests, SHADOW ONLY: once per coordinator tick it records which peer
REQUEST/OFFER/CLAIM a peer-request protocol WOULD have produced from the live worker set,
next to what actually happened. Nothing else. It never posts to the board, never changes a
child, a prompt, the queue, a budget, the delivery order or who the coordinator is.

WHY A SEPARATE MODULE, NOT THREE NEW BOARD KINDS. Roadmap slice C2 asks for REQUEST/OFFER/
CLAIM on the agent board. The board is on the RF hot serialized path: relay/agent_board.py
validates, caps, renders and distributes every kind in relay/board_kinds.REGISTRY, and the
board tests pin that exactly four kinds exist (claim, result, fact, question) and that
"request" is refused as a bad kind. Adding the kinds to that table would therefore change the
serialized path and break those tests before any behaviour was ever trusted. So C2 is built
shadow-first OFF the board: this module holds the three peer kinds AS ITS OWN DATA
(PEER_KINDS) and only writes a shadow ledger. Moving them onto the board, with the coordinator
granting a CLAIM inside the A3 budget, is a later change with its own review once
scripts/peer_requests_report.py says there is enough signal.

THE RULES IT RECORDS (kept small on purpose, stated so a reader can disagree).
From the live worker rows of one tick, grouped by campaign_id:
  * REQUEST  a campaign has >=2 of its subtasks still unfinished AND the run has queued work:
             that campaign could have asked a peer for help.
  * OFFER    a campaign has at least one finished subtask while some OTHER campaign is still
             unfinished: its freed worker could have offered itself.
  * CLAIM    a REQUEST and an OFFER co-occur in the same tick: the coordinator could have let
             the offer claim the request, within the tree budget (observed, never spent).
A tick with no REQUEST and no OFFER records nothing. Only counts, campaign ids and role names
are written -- never goal or step text.

THE SETTING. `peer_requests` accepts `off` and `shadow` and nothing else: there is deliberately
no value that posts a request or grants a claim, so there is nothing to switch on by mistake.
In `off` nothing here reads or writes a file.

HONESTY RULES. A write error is counted and swallowed: the recorder runs inside the
coordinator's tick and must never raise into it. The file is bounded (one rotated copy) and
append-only. STDLIB ONLY.
"""
from __future__ import annotations

import io
import json
import os
import time

KEY = "peer_requests"
#: ONLY these. There is deliberately no value that posts a request or grants a claim.
MODES = ("off", "shadow")

#: The three peer kinds, AS DATA, kept here and NOT on relay/board_kinds.REGISTRY (see module
#: docstring). A later, reviewed change may promote them onto the board.
PEER_KINDS = ("request", "offer", "claim_work")

LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   ".fleet", "peer_requests_shadow.jsonl")
#: Rotated to `<LOG>.1` (replacing the old copy) at this size, so disk never exceeds ~2x.
MAX_BYTES = 1024 * 1024
#: Ticks remembered so a repeated identical tick is one row. Bounds memory.
SEEN_MAX = 4096
SCHEMA = 1

_STATE = {"recorded": 0, "requests": 0, "offers": 0, "claims": 0, "write_errors": 0}
_seen = set()
_settings_cache = {}


def mode(settings_path=None):
    """`peer_requests=` from settings.txt: "shadow" or "off". Anything else reads as off.
    Re-read when the file changes (each_gate). Never raises."""
    try:
        path = settings_path
        if path is None:
            from tools.settings_path import settings_file
            path = settings_file()
        if not path:
            return "off"
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
        hit = _settings_cache.get(path)
        if hit is not None and hit[0] == key:
            return hit[1]
        val = "off"
        with io.open(path, encoding="utf-8-sig") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln.startswith(KEY + "="):
                    v = ln.split("=", 1)[1].strip().lower()
                    val = v if v in MODES else "off"
                    break
        _settings_cache[path] = (key, val)
        return val
    except Exception:
        return "off"


def _campaign_of(row):
    for k in ("campaign_id", "campaign", "cid"):
        v = row.get(k)
        if v:
            return str(v)
    return ""


def _is_subtask(row):
    return str(row.get("role") or "") == "subtask"


def _is_finished(row):
    if row.get("outcome") not in (None, "", "pending"):
        return True
    return bool(row.get("done") or row.get("finished"))


def propose(worker_rows, queued=0):
    """(requests, offers, claims) peer actions a protocol would have produced this tick.
    Pure: reads only the rows and the queued count, mutates nothing, posts nothing."""
    rows = [r for r in (worker_rows or []) if isinstance(r, dict)]
    by_campaign = {}
    for r in rows:
        by_campaign.setdefault(_campaign_of(r), []).append(r)

    unfinished_campaigns = set()
    for cid, group in by_campaign.items():
        if any(not _is_finished(r) for r in group):
            unfinished_campaigns.add(cid)

    requests, offers = [], []
    for cid, group in by_campaign.items():
        if not cid:
            continue
        subs = [r for r in group if _is_subtask(r)]
        unfinished_subs = [r for r in subs if not _is_finished(r)]
        finished_subs = [r for r in subs if _is_finished(r)]
        if len(unfinished_subs) >= 2 and int(queued or 0) > 0:
            requests.append({"kind": "request", "campaign_id": cid,
                             "unfinished": len(unfinished_subs)})
        if finished_subs and (unfinished_campaigns - {cid}):
            offers.append({"kind": "offer", "campaign_id": cid,
                           "free": len(finished_subs)})

    claims = []
    if requests and offers:
        n = min(len(requests), len(offers))
        for i in range(n):
            claims.append({"kind": "claim_work",
                           "request_campaign": requests[i]["campaign_id"],
                           "offer_campaign": offers[i]["campaign_id"]})
    return requests, offers, claims


def _append(row):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        try:
            if os.path.getsize(LOG) >= MAX_BYTES:
                os.replace(LOG, LOG + ".1")
        except OSError:
            pass
        with io.open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        return True
    except Exception:
        _STATE["write_errors"] += 1
        return False


def shadow_tick(worker_rows, queued=0, run_id="", settings_path=None, now=None):
    """Called by the coordinator once per tick. In `shadow` it appends one row when there is any
    REQUEST/OFFER. In `off` it returns at once. Never raises, never mutates its arguments."""
    try:
        if mode(settings_path) != "shadow":
            return None
        requests, offers, claims = propose(worker_rows, queued)
        if not requests and not offers:
            return None
        ident = (str(run_id or ""),
                 tuple(sorted(r["campaign_id"] for r in requests)),
                 tuple(sorted(o["campaign_id"] for o in offers)))
        if ident in _seen:
            return None
        if len(_seen) >= SEEN_MAX:
            _seen.clear()
        _seen.add(ident)
        row = {
            "v": SCHEMA, "ts": round(time.time() if now is None else now, 1),
            "run_id": str(run_id or ""), "mode": "shadow",
            "queued": int(queued or 0),
            "requests": requests, "offers": offers, "claims": claims,
            "counts": {"request": len(requests), "offer": len(offers),
                       "claim_work": len(claims)},
        }
        if _append(row):
            _STATE["recorded"] += 1
            _STATE["requests"] += len(requests)
            _STATE["offers"] += len(offers)
            _STATE["claims"] += len(claims)
        return None
    except Exception:
        return None


def status_block(settings_path=None):
    """{"peer_requests": {mode, recorded, requests, offers, claims}} for status.json (additive)."""
    return {KEY: {"mode": mode(settings_path), "recorded": int(_STATE["recorded"]),
                  "requests": int(_STATE["requests"]), "offers": int(_STATE["offers"]),
                  "claims": int(_STATE["claims"])}}


def _reset_for_test():
    _STATE.update({"recorded": 0, "requests": 0, "offers": 0, "claims": 0, "write_errors": 0})
    _seen.clear()
    _settings_cache.clear()
