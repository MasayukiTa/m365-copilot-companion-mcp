# -*- coding: utf-8 -*-
"""A stuck worker's cause, read from what the tools actually returned.

WHY THIS EXISTS (owner requirement, 2026-10-15). When the DB host is unreachable the stuck
reason must say NETWORK in its first words. Last night's run had 17 stuck goals and the reason
depended on which code path happened to settle the worker:

  * STUCK     "worker reached the same conclusion on consecutive turns -> settling on its own
              stated reason ...: <the model's words>"  -- right only if the model said so.
              Three workers said "locked / session IP not authorised"; five said 10060.
  * REFUSED   "this was a refusal of the instruction, reword it" -- printed for workers whose
              very first reply was Copilot's canned non-answer, while the ledger showed the DB
              host timing out under every sibling.
  * INFRA_STUCK "tool calls did not reach the gateway" -- a different fault, same shelf.

All three are decided from the model's prose or from a single marker. The tool ledger
(.fleet/tool_events.jsonl) holds the real outcomes: the ODBC error text with its 10060/08001,
the lock refusal with the IP it refused. This module reads those, deterministically, and returns
one typed cause that the harness puts FIRST in the reason.

KINDS: "network" | "locked" | "gateway" | "refused".  `refused` is never produced here from
tool evidence -- it is the name of what is left when nothing above applies, kept in the vocabulary
so a consumer has one closed set to switch on.

ATTRIBUTION IS NOT ALWAYS POSSIBLE AND THE TEXT SAYS SO. Most ledger rows carry an empty worker
(attr "ambiguous": several workers' turn windows overlap). A cause found only on such rows is
real for the machine and probable for the worker; `Cause.attributed` is False and the headline
says "同時間帯" instead of pretending to a certainty the ledger does not have.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime

KINDS = ("network", "locked", "gateway", "tools", "procedure", "refused")

#: Heads of the Japanese headline, one per kind. A reason that already starts with one of these
#: has been stamped; stamping again would stack them.
HEADS = {"network": "ネットワーク:", "locked": "ロック:", "gateway": "ゲートウェイ未到達:",
         "tools": "ツール未接続:", "procedure": "手順未実施:"}

#: How much of the ledger tail to read. 1.7 KB per row measured, so 12 MB is several thousand
#: rows: hours of a busy fleet, one seek, never the whole 68 MB file.
TAIL_BYTES = 12 * 1024 * 1024

#: A worker whose start time is unknown is judged on this much history.
DEFAULT_LOOKBACK_S = 3 * 3600.0

# --- patterns -----------------------------------------------------------------------------
# Applied ONLY to outcomes the ledger itself judged failed (tool_ledger.row_ok is False). The
# ledger holds 3,000+ rows that contain the word "timeout" because a file being read quotes
# it; a failure shape is what separates a tool reporting an error from content that mentions one.
_LOCK_RE = re.compile(r"locked: no valid unlock token for '([^']*)'|not unlocked|"
                      r"refused \(locked\)|セッションIP未認可", re.I)
_LOCK_IP_RE = re.compile(r"unlock token for '([^']*)'")
_NET_RE = re.compile(
    r"\b08001\b|\b08S01\b|\bHYT00\b|DBNETLIB|SQL Server (?:が存在しない|does not exist)|"
    r"does not exist or access denied|WinError 1006[01]|\((?:10060|10061|10065|10051|10054|11001)\)|"
    r"timed out|Connection (?:refused|timed out|reset)|TCP Provider|getaddrinfo|"
    r"No route to host|Network is unreachable|Name or service not known|ConnectionOpen", re.I)
_CODE_NUM_RE = re.compile(r"\((10060|10061|10065|10051|10054|11001)\)|WinError (1006[01])")
_CODE_STATE_RE = re.compile(r"\b(08001|08S01|HYT00)\b")
_HOST_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})(?:[:,](\d{2,5}))?")

#: Goal text that can plausibly depend on the DB. A network cause seen only on UNATTRIBUTED rows
#: is attached to a worker whose own words do not mention a network problem only if its goal is
#: about data the DB holds -- otherwise an unrelated worker would be headlined with a fault that
#: merely happened beside it.
_DB_GOAL_RE = re.compile(r"DB|ＤＢ|ADAMS|mcl|odbc|SQL|データベース|不良|検査|ロット|集計|ワニス|銅箔", re.I)
#: The worker's own stuck reason already speaks of the matching fault.
_NET_WORDS_RE = re.compile(r"ネットワーク|network|10060|08001|到達|タイムアウト|timeout|VPN|接続", re.I)
_LOCK_WORDS_RE = re.compile(r"ロック|解錠|unlock|未認可|locked", re.I)

#: Generic wrappers the harness puts in front of the model's own sentence.
_GENERIC_PREFIXES = (
    re.compile(r"^worker reached the same conclusion on consecutive turns -> settling on its "
               r"own stated reason instead of nudging again:\s*"),
    re.compile(r"^agent reported STUCK \(after \d+ retries\):\s*"),
)
#: A reason that is the canned/refusal diagnosis rather than something the worker said.
_CANNED_REASON_RE = re.compile(r"定型の無回答が継続")


@dataclass
class Cause:
    kind: str                       # one of KINDS
    code: str = ""                  # "10060", "08001", "timeout" ...
    host: str = ""
    port: str = ""
    tool: str = ""
    ip: str = ""                    # locked: the session IP the gateway refused
    since: float = 0.0              # first failure of the current streak
    last: float = 0.0
    count: int = 0                  # failures in the streak
    attributed: bool = True         # False: seen on overlapping-window rows, not this worker's own
    detail: str = ""
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["headline"] = self.headline()
        return d

    def headline(self) -> str:
        """The first words of the stuck reason. Concrete: host, code, since when."""
        when = ""
        if self.since:
            when = "%s から%d回" % (datetime.fromtimestamp(self.since).strftime("%m/%d %H:%M"),
                                    max(1, self.count))
        shared = "" if self.attributed else "・同時間帯の本機MCP全体の観測(このワーカー固有の呼び出しは特定できず)"
        tail = ("〔%s%s〕" % (when, shared)) if (when or shared) else ""
        if self.kind == "network":
            target = self.host + (":" + self.port if self.port else "")
            if self.tool.startswith("odbc"):
                what = "DBサーバ %s へ到達できません" % target if target else "DBサーバへ到達できません"
            else:
                what = ("%s の接続先 %s へ到達できません" % (self.tool or "ツール", target)).replace("  ", " ")
            return "%s %s(%s)%s" % (HEADS["network"], what, self.code or "接続不能", tail)
        if self.kind == "locked":
            ip = "(セッションIP %s 未認可)" % self.ip if self.ip else ""
            return "%s ツールが未解錠のため実行できません%s%s" % (HEADS["locked"], ip, tail)
        if self.kind == "gateway":
            return "%s %s%s" % (HEADS["gateway"], self.detail or "ツール呼び出しが本機のMCPに着弾していません", tail)
        if self.kind == "tools":
            return "%s %s%s" % (HEADS["tools"], self.detail or "会話にMCPコネクタのツールが付いていません", tail)
        if self.kind == "procedure":
            return "%s %s%s" % (HEADS["procedure"], self.detail or "承認済み手順が前提とするツールを呼んでいません", tail)
        return "拒否: この指示が拒否された可能性があります"


# --- ledger access -------------------------------------------------------------------------
def _ledger_path() -> str:
    try:
        from tools import tool_ledger
        return tool_ledger._repo_path()
    except Exception:
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            ".fleet", "tool_events.jsonl")


def read_tail(path: str = "", nbytes: int = TAIL_BYTES) -> list:
    """Parsed rows from the end of the ledger. Never raises."""
    import io
    import json
    rows: list = []
    try:
        p = path or _ledger_path()
        size = os.path.getsize(p)
        with io.open(p, "rb") as fh:
            if size > nbytes:
                fh.seek(size - nbytes)
                fh.readline()
            for raw in fh:
                try:
                    rows.append(json.loads(raw.decode("utf-8", "replace")))
                except ValueError:
                    continue
    except Exception:
        return rows
    return rows


def _outcome_text(o: dict) -> str:
    res = o.get("result")
    if isinstance(res, dict):
        res = res.get("text")
    return "%s %s" % (o.get("error") or "", res if isinstance(res, str) else "")


def _row_failed(o: dict) -> bool:
    """The ledger's own verdict (refusals and self-reported error shapes corrected on read)."""
    try:
        from tools import tool_ledger
        return not tool_ledger.row_ok(o)
    except Exception:
        return not o.get("ok", True)


def _pairs(rows) -> list:
    """[(call, outcome)] for every call that has an outcome, in call order."""
    outs = {r.get("id"): r for r in rows if r.get("event") == "outcome"}
    return [(r, outs[r.get("id")]) for r in rows
            if r.get("event") == "call" and r.get("id") in outs]


def _arg_text(call: dict, key: str) -> str:
    a = (call.get("args") or {}).get(key)
    if isinstance(a, dict):
        a = a.get("text")
    return a if isinstance(a, str) else ""


# --- connection target ---------------------------------------------------------------------
def parse_server(conn: str):
    """('10.8.67.28', '50379') from a connection string's SERVER=... part. ('', '') if absent."""
    m = re.search(r"\b(?:server|host|data source)\s*=\s*([^;]+)", conn or "", re.I)
    if not m:
        return "", ""
    s = m.group(1).strip()
    s = re.sub(r"^(?:tcp:)", "", s, flags=re.I)
    port = ""
    if "," in s:
        s, port = s.rsplit(",", 1)
    s = s.split("\\", 1)[0]
    if ":" in s and s.count(":") == 1:
        s, port = s.split(":", 1)
    return s.strip(), port.strip()


def target_of(call: dict, text: str, env=None):
    """(host, port) the failing call was aiming at: the error text first, then the named
    connection's configuration. Neither is guessed from anything else."""
    env = os.environ if env is None else env
    m = _HOST_RE.search(text or "")
    if m:
        return m.group(1), m.group(2) or ""
    name = _arg_text(call, "connection").strip().lower()
    conns = {k[len("MCP_DB_"):].lower(): v for k, v in env.items()
             if k.startswith("MCP_DB_") and v}
    if name and name in conns:
        return parse_server(conns[name])
    if not name and len(conns) == 1:
        return parse_server(next(iter(conns.values())))
    return "", ""


# --- classification ------------------------------------------------------------------------
def classify_text(tool: str, text: str):
    """('locked'|'network'|None, code, ip) for ONE failed outcome's text."""
    if _LOCK_RE.search(text or ""):
        m = _LOCK_IP_RE.search(text)
        return "locked", "locked", (m.group(1) if m else "")
    if _NET_RE.search(text or ""):
        m = _CODE_NUM_RE.search(text)
        code = (m.group(1) or m.group(2)) if m else ""
        if not code:
            m = _CODE_STATE_RE.search(text)
            code = m.group(1) if m else ""
        if not code:
            code = "timeout"
        return "network", code, ""
    return None, "", ""


def _streaks(pairs, env=None):
    """Open causes among these pairs: {kind: Cause}. 'Open' = no later success of the same
    tool (or, for a lock, a later successful `unlock`) closed it."""
    open_: dict = {}
    for call, out in pairs:
        tool = str(call.get("tool") or "")
        ts = float(out.get("ts") or call.get("ts") or 0.0)
        if not _row_failed(out):
            # A success closes what it disproves.
            for kind in list(open_):
                c = open_[kind]
                if tool == c.tool or (kind == "locked" and tool == "unlock") \
                        or (kind == "network" and c.tool.startswith("odbc")
                            and tool.startswith("odbc")):
                    del open_[kind]
            continue
        text = _outcome_text(out)
        kind, code, ip = classify_text(tool, text)
        if not kind:
            continue
        c = open_.get(kind)
        if c is None:
            host, port = target_of(call, text, env) if kind == "network" else ("", "")
            c = open_[kind] = Cause(kind=kind, code=code, host=host, port=port, tool=tool,
                                    ip=ip, since=ts, last=ts, count=0,
                                    detail=text.strip()[:240])
        c.count += 1
        c.last = ts
        if kind == "network" and (c.code == "timeout" or c.code.startswith("0")) and code \
                and code not in ("timeout",) and not code.startswith("0"):
            c.code = code            # prefer the socket error number over the SQLSTATE
        if kind == "network" and not c.host:
            c.host, c.port = target_of(call, text, env)
        if kind == "locked" and ip and not c.ip:
            c.ip = ip
    return open_


def _pick(open_: dict):
    if not open_:
        return None
    # Latest failure wins; on a tie the network is named (a lock is fixable from this end).
    return sorted(open_.values(), key=lambda c: (c.last, c.kind == "network"))[-1]


def classify_rows(rows, *, worker: str = "", since: float = 0.0, until: float = 0.0,
                  env=None):
    """The typed cause for one worker from ledger `rows`, or None.

    Own rows (the ledger attributed the call to this worker) are consulted first; only if they
    hold no cause are the unattributed (empty-worker) rows, and then the result is marked
    `attributed=False`. Rows attributed to a DIFFERENT worker are never used."""
    until = until or time.time()
    own, shared = [], []
    for call, out in _pairs(rows):
        ts = float(call.get("ts") or 0.0)
        if ts < since or ts > until:
            continue
        w = str(call.get("worker") or "")
        if w and w == worker:
            own.append((call, out))
        elif not w:
            shared.append((call, out))
    c = _pick(_streaks(own, env))
    if c:
        c.attributed = True
        return c
    c = _pick(_streaks(shared, env))
    if c:
        c.attributed = False
        return c
    return None


def gateway_cause(rows, *, since: float, unlanded: int, until: float = 0.0):
    """The tool call the model wrote never reached us. `rows` says whether anything at all did."""
    until = until or time.time()
    landed = [c for c, _o in _pairs(rows) if since <= float(c.get("ts") or 0.0) <= until]
    if landed:
        d = ("返信に書かれたツール呼び出しが%dターン連続で本機のMCPに着弾していません"
             "(同時間帯に他の呼び出しは%d件着弾=ゲートウェイは生存・コネクタ/ツール名/経路の問題)"
             % (unlanded, len(landed)))
    else:
        d = ("ツール呼び出しが%dターン連続で本機のMCPに1件も着弾していません"
             "(同時間帯の台帳も0件=ゲートウェイ/トンネルに届いていない)" % unlanded)
    return Cause(kind="gateway", since=since, count=unlanded, attributed=True, detail=d,
                 extra={"landed_in_window": len(landed)})


def tools_unattached_cause(*, since: float, attempts: int, via: str = "") -> Cause:
    """Every fresh conversation the worker was given came up WITHOUT the MCP connector tools
    (no `initialize` reached the server after the first send, or the model said it has no
    call_tool). The connector is bound when a conversation is created (relay/mcp_ready.py)."""
    d = ("新しい会話を%d回作り直しましたがMCPコネクタのツールが付きませんでした"
         "(最初の送信後にinitializeが本機のMCPへ着弾せず)%s"
         % (attempts, ("・検出: " + via) if via else ""))
    return Cause(kind="tools", since=since, count=attempts, attributed=True, detail=d)


def plausible(cause: Cause, goal: str, reason: str) -> bool:
    """May an UNATTRIBUTED cause be put on this worker? Attributed ones always may."""
    if cause.attributed or cause.kind in ("gateway", "tools", "procedure"):
        return True
    words = _NET_WORDS_RE if cause.kind == "network" else _LOCK_WORDS_RE
    if not _CANNED_REASON_RE.search(reason or "") and words.search(reason or ""):
        return True
    if cause.kind == "network" and not cause.tool.startswith("odbc"):
        # A web fetch timing out somewhere says nothing about a worker that never fetched.
        return False
    return bool(_DB_GOAL_RE.search(goal or ""))


# --- composing the reason ------------------------------------------------------------------
def strip_generic(reason: str) -> str:
    r = (reason or "").strip()
    for rx in _GENERIC_PREFIXES:
        r = rx.sub("", r)
    return r.strip()


def already_stamped(reason: str) -> bool:
    r = (reason or "").lstrip("⚠ ").lstrip()
    return any(r.startswith(h) for h in HEADS.values())


def compose_reason(cause: Cause, old_reason: str) -> str:
    """Cause first; whatever was said before follows, with the generic wrapper removed and the
    'refusal of this instruction' verdict withdrawn (it is not known to be one)."""
    head = cause.headline()
    old = strip_generic(old_reason)
    if not old:
        return head
    if _CANNED_REASON_RE.search(old):
        return ("%s / 定型の無回答が継続しているが、原因は上記。**この指示が拒否されたとは"
                "判定していない**(言い換えは不要・原因の解消後に再投入)。" % head)
    return "%s / %s: %s" % (head, "ワーカーの所見" if old_reason != old else "従来の診断", old)


def procedure_unfollowed_cause(*, skill: str, required, steers: int, since: float,
                               attributed: bool = False, detail: str = "") -> Cause:
    """The worker kept claiming DONE without calling any tool the matched skill declares as
    required, through `steers` bounded steers. Domain-free: `skill` and `required` come from the
    skill's own metadata (relay/skill_procedure.py)."""
    d = ("skill procedure not followed: `%s` が前提とするツール(%s)を呼ばないまま、%d回の差し戻し後も"
         "完了を主張しました%s"
         % (skill, ", ".join(required or []), steers, ("・" + detail) if detail else ""))
    return Cause(kind="procedure", since=since, count=max(1, steers), attributed=attributed,
                 detail=d, extra={"skill": skill, "required": list(required or [])})

