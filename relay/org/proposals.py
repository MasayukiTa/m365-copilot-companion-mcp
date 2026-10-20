"""A change proposal for a role or protocol definition, kept as a record and nothing else (slice D3).

WHAT DECISION THIS SERVES. How an edit to a prompt gets proposed by whoever noticed it needs one
(an agent or a person) without that edit being applied, and without the proposal living only in
a chat that ends. A proposal is a typed record: which definition (a name from relay/org/
definitions.py), the hash of that definition now, the hash of the proposed text, a short
rationale, and who proposed it. The proposed text itself goes on the pending-decision card
(relay/selfimprove/pending.py), where the person who decides can read it; it is not in the log.

A PROPOSAL DOES NOTHING BY ITSELF. No prompt, role, workflow or admission reads this file.
Applying a proposal is a separate step that a person approves and that is NOT implemented here:
approving the card records a decision and nothing more (the card carries no action).

SETTING `org_proposals`: off | on, default off, read on each call (the tool is called by an
agent between turns, so there is no run start to read it at). Off: the tool returns `refused`,
reads one settings line, and writes nothing, anywhere.

IDENTITY IS NEVER AN ARGUMENT. The author is the worker the calling session is placed under (the
same evidence board_digest uses, tools/board_tool.py); a session that cannot be placed is
recorded as "unplaced". A caller cannot name another author.

THE LINE, one JSON object, keys fixed (anything else is refused, not stored):
  ts, target ("role:<name>" | "workflow:<name>"), base_hash, proposed_hash, proposed_len,
  rationale (<= 400 chars, no control characters), author, campaign.
Never goal text.

BOUNDED. The file stops growing at MAX_BYTES; one author may have MAX_PER_AUTHOR lines in it;
the same (target, base, proposed) is one proposal however often it is submitted. Append-only.

FAILURE-SAFE. `submit` returns a dict and never raises; a write that fails is counted in
`_STATE["write_errors"]` and reported as `error`.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import threading
import time

KEY = "org_proposals"
MODES = ("off", "on")
FILENAME = "org_proposals.jsonl"

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
#: Runtime state, untracked. Tests redirect it (conftest LIVE_RECORD_REDIRECTS).
LOG_PATH = os.path.join(_REPO, ".fleet", FILENAME)

MAX_BYTES = 1024 * 1024
MAX_PER_AUTHOR = 25
MAX_RATIONALE = 400
MAX_PROPOSED_CHARS = 20000          # larger text is refused, not cut: a cut text is another text
CARD_TEXT_CHARS = 8000              # what the decision card shows of the proposed text
MAX_AUTHOR = 64

FIELDS = ("ts", "target", "base_hash", "proposed_hash", "proposed_len", "rationale", "author",
          "campaign")

SUBMITTED, REFUSED, ERROR, DUPLICATE = "queued", "refused", "error", "duplicate"

_LOCK = threading.Lock()
_STATE = {"queued": 0, "refused": 0, "write_errors": 0, "skipped_full": 0}

_HEX = re.compile(r"^[0-9a-f]{8,64}$")
_AUTHOR = re.compile(r"^[A-Za-z0-9_.:\-]{1,%d}$" % MAX_AUTHOR)
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


def _reset_for_test():
    with _LOCK:
        _STATE.update(queued=0, refused=0, write_errors=0, skipped_full=0)


def mode(settings_path=None) -> str:
    """`org_proposals=` from settings.txt: "on" or "off". Anything else reads as off."""
    try:
        path = settings_path
        if path is None:
            from tools.settings_path import settings_file
            path = settings_file()
        if not path or not os.path.isfile(path):
            return "off"
        val = "off"
        with io.open(path, encoding="utf-8-sig") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln.startswith(KEY + "="):
                    v = ln.split("=", 1)[1].strip().lower()
                    val = v if v in MODES else "off"
                    break
        return val
    except Exception:
        return "off"


def targets(snap=None) -> dict:
    """{"role:planner": current hash, "workflow:worker_protocol": ...}. An unreadable definition
    has hash "" (it can be named but not proposed against). `snap` is a definitions.snapshot()
    the caller already has (the shadow evaluator hashes the prompts once per run, not twice)."""
    from relay.org import definitions
    if snap is None:
        snap = definitions.snapshot()
    out = {}
    for kind, section in (("role", snap["roles"]), ("workflow", snap["workflows"])):
        for name, entry in section.items():
            out["%s:%s" % (kind, name)] = str(entry.get("hash") or "")
    return out


def text_hash(text) -> str:
    """Hash of proposed text, comparable with a definition hash: line endings normalised."""
    t = str(text).replace("\r\n", "\n")
    return hashlib.sha256(json.dumps(t, ensure_ascii=False, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()[:16]


def validate(rec, known_targets=None):
    """(True, "") or (False, reason). Pure; never raises.

    Unknown keys, a missing key, a wrong type, an unknown target, a hash that is not lowercase
    hex, a rationale over the bound or holding a control character, and an author outside the
    safe alphabet are each refused."""
    try:
        if not isinstance(rec, dict):
            return False, "record is not an object"
        extra = sorted(set(rec) - set(FIELDS))
        if extra:
            return False, "unknown keys: " + ",".join(str(k) for k in extra[:5])
        missing = [k for k in FIELDS if k not in rec]
        if missing:
            return False, "missing keys: " + ",".join(missing)
        known = targets() if known_targets is None else known_targets
        tgt = rec["target"]
        if not isinstance(tgt, str) or tgt not in known:
            return False, "unknown target"
        for k in ("base_hash", "proposed_hash"):
            v = rec[k]
            if not isinstance(v, str) or not _HEX.match(v):
                return False, k + " is not a lowercase hex hash"
        if rec["base_hash"] == rec["proposed_hash"]:
            return False, "proposed text equals the current definition"
        if isinstance(rec["proposed_len"], bool) or not isinstance(rec["proposed_len"], int) \
                or not 0 < rec["proposed_len"] <= MAX_PROPOSED_CHARS:
            return False, "proposed_len out of range"
        r = rec["rationale"]
        if not isinstance(r, str) or not r.strip():
            return False, "rationale is empty"
        if len(r) > MAX_RATIONALE:
            return False, "rationale over %d characters" % MAX_RATIONALE
        if _CTRL.search(r):
            return False, "rationale has a control character"
        a = rec["author"]
        if not isinstance(a, str) or not _AUTHOR.match(a):
            return False, "author is not a plain identifier"
        c = rec["campaign"]
        if not isinstance(c, str) or len(c) > MAX_AUTHOR or _CTRL.search(c):
            return False, "campaign is not a short plain string"
        if isinstance(rec["ts"], bool) or not isinstance(rec["ts"], (int, float)):
            return False, "ts is not a number"
        return True, ""
    except Exception:
        return False, "record could not be checked"


def read(path=None):
    """(records, unreadable). Missing file is empty; a line that is not a valid record is
    counted, never guessed at. Read-only."""
    path = path or LOG_PATH
    recs, bad = [], 0
    try:
        known = targets()
        with io.open(path, encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                ok, _why = validate(obj, known_targets=known)
                if ok:
                    recs.append(obj)
                else:
                    bad += 1
    except OSError:
        pass
    return recs, bad


def _result(status, reason="", **extra):
    out = {"status": status, "reason": reason}
    out.update(extra)
    return out


def submit(target, rationale, proposed_text, author, campaign="", base_hash="",
           settings_path=None, now=None, path=None):
    """File one proposal. Returns {"status": queued|duplicate|refused|error, "reason": ...}.

    Never raises, never changes a definition, and writes nothing unless the setting is on and
    the record is valid. `base_hash` is optional; when given it must be the definition's hash
    now, so a proposal written against an older text is refused rather than filed as current."""
    try:
        if mode(settings_path) != "on":
            with _LOCK:
                _STATE["refused"] += 1
            return _result(REFUSED, "org_proposals is off")
        if not isinstance(proposed_text, str) or not proposed_text.strip():
            return _result(REFUSED, "proposed_text is empty")
        if len(proposed_text) > MAX_PROPOSED_CHARS:
            return _result(REFUSED, "proposed_text over %d characters" % MAX_PROPOSED_CHARS)
        known = targets()
        if not isinstance(target, str) or target not in known:
            return _result(REFUSED, "unknown target; known: " + ", ".join(sorted(known)))
        current = known[target]
        if not current:
            return _result(REFUSED, "that definition could not be read, so there is no base")
        if base_hash and str(base_hash) != current:
            return _result(REFUSED, "stale base_hash; the definition has changed")
        rec = {"ts": float(time.time() if now is None else now), "target": target,
               "base_hash": current, "proposed_hash": text_hash(proposed_text),
               "proposed_len": len(proposed_text), "rationale": rationale,
               "author": str(author or "unplaced"), "campaign": str(campaign or "")}
        ok, why = validate(rec, known_targets=known)
        if not ok:
            with _LOCK:
                _STATE["refused"] += 1
            return _result(REFUSED, why)
        path = path or LOG_PATH
        with _LOCK:
            existing, _bad = read(path)
            for e in existing:
                if (e["target"], e["base_hash"], e["proposed_hash"]) == \
                        (rec["target"], rec["base_hash"], rec["proposed_hash"]):
                    return _result(DUPLICATE, "already proposed", target=target)
            if sum(1 for e in existing if e["author"] == rec["author"]) >= MAX_PER_AUTHOR:
                _STATE["refused"] += 1
                return _result(REFUSED, "this author already has %d proposals" % MAX_PER_AUTHOR)
            data = json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n"
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            if size + len(data.encode("utf-8")) > MAX_BYTES:
                _STATE["skipped_full"] += 1
                return _result(REFUSED, "the proposals log is full")
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with io.open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(data)
            _STATE["queued"] += 1
        _queue_card(rec, proposed_text)
        return _result(SUBMITTED, "", target=target, proposed_hash=rec["proposed_hash"])
    except Exception:
        try:
            with _LOCK:
                _STATE["write_errors"] += 1
        except Exception:
            pass
        return _result(ERROR, "the proposal could not be recorded")


def card_reason(rec) -> str:
    """The one line a decision card carries; also what identifies the card."""
    return "org change proposal %s %s->%s by %s: %s" % (
        rec["target"], rec["base_hash"], rec["proposed_hash"], rec["author"], rec["rationale"])


def _queue_card(rec, proposed_text):
    """Show the proposal through the pending-decision queue, the same card the dashboard lists.

    No toast (notify=False), no `action`: approving it records a decision and applies nothing.
    Never raises; a failure leaves the log line, which the report script still lists."""
    try:
        from relay.selfimprove import pending
        pending.add(
            [rec["target"]],
            card_reason(rec),
            diff=str(proposed_text)[:CARD_TEXT_CHARS],
            detail=("Proposed replacement text for %s (%d characters; the card shows at most %d). "
                    "Approving records a decision only. Nothing applies it." % (
                        rec["target"], rec["proposed_len"], CARD_TEXT_CHARS)),
            notify=False)
    except Exception:
        pass


def listing(path=None):
    """Read-only: [{record fields..., "card": status}] newest last, plus the unreadable count.

    `card` is the pending-queue status of the proposal's card ("" when it has none)."""
    recs, bad = read(path)
    cards = {}
    try:
        from relay.selfimprove import pending
        for it in pending.items(include_resolved=True):
            cards[it.get("id")] = it.get("status") or ""
    except Exception:
        pass
    out = []
    for r in recs:
        try:
            from relay.selfimprove import pending
            pid = pending._key([r["target"]], card_reason(r))
        except Exception:
            pid = ""
        out.append(dict(r, card=cards.get(pid, "")))
    return out, bad
