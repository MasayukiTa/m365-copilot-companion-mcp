# -*- coding: utf-8 -*-
"""Recover a socket-route goal that was cut after unknown delivery, WITHOUT executing anything twice.

THE PROBLEM, MEASURED (job eb0a78560880, 2026-10-19). A bridged goal ran 50+ tool calls, then one
turn died with `turn deadline exceeded before a completion frame`. The retry policy refuses to
resend an acting goal whose turn may have landed, so the worker ended STUCK although its work was
done. Design and decision table: Documents\\フリート設計見直し_20261014\\roadmap\\
設計_続きのターンによるSTUCK回復_20261020.md.

WHY A CONTINUE TURN CAN BE SAFE HERE. Tools run on THIS side: `Conversation.ask` executes a call
only from a reply that reached its completion frame. A reply that was cut off is never read, so it
never executed anything. What can go wrong is therefore a short list, and each item has a check:

  * a call this side already ran is asked for again      -> its result is re-quoted in the
                                                            continue text, and the first reply's
                                                            repeats are answered from the record
                                                            (`ReplayCache`) instead of executed;
  * a call is still running, or may have run (timeout)  -> `plan` refuses;
  * the old turn thread is still alive                   -> `plan` refuses;
  * the conversation no longer exists (an unused id is   -> a probe turn asks for the `[ref:...]`
    accepted by the backend and starts an EMPTY chat)       of the last payload; only a model that
                                                            holds the history can answer;
  * the model acted with its own tools (mail, calendar)  -> goal class check + the probe's
    -- invisible to the ledger                              NATIVE= line. Not fully closable; see
                                                            the design note, section 4.

DEFAULT OFF (`socket_continue_recovery`). With it off nothing in this module runs on the wire:
`Conversation.ask` keeps no journal, adds no ref, wraps no tool.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid

KEY = "socket_continue_recovery"
DEFAULT = "off"

#: A refusal by the continue turn itself starts with this. The fleet reads it to stop: no resend,
#: no new conversation.
REFUSED_PREFIX = "continue refused"

#: Continues allowed for one goal. A continue that is itself cut gets one more chance, not a loop.
MAX_CONTINUES_PER_GOAL = 2

#: Bounds, so a long goal cannot make the journal the thing that fills memory.
RAW_CAP = 200000
RESULT_CAP = 16000
MAX_ASKS_KEPT = 4

#: How much of the server ledger's tail is read to look for calls that never returned.
LEDGER_TAIL_BYTES = 8 * 1024 * 1024

#: Failure reasons a continue may follow: the turn was cut by time or by the connection. A backend
#: that DECLINED the request, an expired token or a round limit is a different fault.
_CONTINUABLE = re.compile(
    r"deadline exceeded|turn ceiling|went silent|no close frame|ConnectionClosed", re.I)

#: What `make_run_tool` writes when a call timed out or raised: the server may still have run it.
_UNKNOWN_EFFECT = re.compile(
    r"^\s*(?:TimeoutError|[A-Za-z_.]*(?:Error|Exception|Timeout|Disconnected))\b\s*:")

#: Goals whose acts the model can perform with its own (Work IQ) tools. Such an act never passes
#: through `run_tool`, so no journal and no ledger on this side would see it.
_NATIVE_VOCABULARY = re.compile(
    r"メール|mail|予定|カレンダー|calendar|会議|meeting|teams|チャット|chat|outlook|sharepoint|"
    r"onedrive|planner|招待|invite|返信|reply|宛先", re.I)

#: Positive requirement: the act is repository work done through tools (git, tests, files).
_REPO_WORK = re.compile(
    r"git|worktree|commit|コミット|push|pytest|テスト|test|ファイル|\.py\b|\.md\b|リポジトリ|repo",
    re.I)


# ---- the setting -----------------------------------------------------------------------------

_cache = {}


def _settings_path(path=None):
    if path:
        return str(path)
    from tools.settings_path import settings_file
    return settings_file()


def mode(path=None) -> str:
    """`on` or `off`, from settings.txt. Anything but `on` is `off`. Never raises.

    One os.stat per read and a re-read only when the file changed, so a change made in the cockpit
    reaches the next decision with no restart (declared each_gate in tools/settings_keys.py).
    """
    try:
        p = _settings_path(path)
        st = os.stat(p)
        key = (p, st.st_mtime_ns, st.st_size)
        hit = _cache.get(p)
        if hit is not None and hit[0] == key:
            return hit[1]
        val = DEFAULT
        with open(p, encoding="utf-8-sig") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln.startswith("socket_continue_recovery="):
                    v = ln.split("=", 1)[1].strip().lower()
                    val = "on" if v == "on" else "off"
                    break
        _cache[p] = (key, val)
        return val
    except Exception:
        return DEFAULT


def recovery_on(path=None) -> bool:
    return mode(path) == "on"


# ---- the journal -----------------------------------------------------------------------------

def ref_suffix(ref: str) -> str:
    """What is appended to every payload while the journal is on. The model can quote it back."""
    return "\n\n[ref:%s]" % ref


def signature(name, args) -> str:
    try:
        body = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        body = repr(args)
    return "%s\x00%s" % (name, body)


def _cap(text, limit):
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit]


class TurnJournal:
    """What THIS side did in the current conversation: which payloads went out, which calls ran.

    Written only by the turn thread (inside `Conversation.ask`) and read by the fleet after that
    thread has ended, so the lock exists for the stall-watchdog case, where both are alive.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.asks = []
        #: Ref of the last round whose exchange COMPLETED: the model certainly received it.
        self.last_ref = ""
        #: Exchanges the backend completed in this conversation (it knows the id).
        self.completed_exchanges = 0
        self.recoveries = 0

    # -- one ask
    def begin_ask(self):
        with self._lock:
            self.asks.append({"started": time.time(), "rounds": [], "calls": [], "finished": False})
            del self.asks[:-MAX_ASKS_KEPT]

    def end_ask(self):
        with self._lock:
            if self.asks:
                self.asks[-1]["finished"] = True

    # -- rounds
    def begin_round(self, n, raw) -> str:
        ref = uuid.uuid4().hex[:12]
        with self._lock:
            if not self.asks:
                self.asks.append({"started": time.time(), "rounds": [], "calls": [],
                                  "finished": False})
            self.asks[-1]["rounds"].append({"n": n, "ref": ref, "raw": _cap(raw, RAW_CAP),
                                            "sent": False, "completed": False})
        return ref

    def _open_round(self):
        if not self.asks or not self.asks[-1]["rounds"]:
            return None
        r = self.asks[-1]["rounds"][-1]
        return None if r["completed"] else r

    def mark_sent(self):
        with self._lock:
            r = self._open_round()
            if r is not None:
                r["sent"] = True

    def mark_completed(self):
        with self._lock:
            r = self._open_round()
            if r is not None:
                r["completed"] = True
                self.last_ref = r["ref"]
                self.completed_exchanges += 1

    # -- calls
    def call_started(self, name, args) -> int:
        with self._lock:
            if not self.asks:
                self.asks.append({"started": time.time(), "rounds": [], "calls": [],
                                  "finished": False})
            calls = self.asks[-1]["calls"]
            rounds = self.asks[-1]["rounds"]
            calls.append({"n": len(calls), "round": rounds[-1]["n"] if rounds else 0,
                          "name": str(name), "args": args, "sig": signature(name, args),
                          "state": "running", "ok": None, "text": ""})
            return len(calls) - 1

    def call_finished(self, idx, ok, text):
        with self._lock:
            c = self.asks[-1]["calls"][idx]
            c["state"], c["ok"], c["text"] = "done", bool(ok), _cap(text, RESULT_CAP)

    def call_raised(self, idx):
        with self._lock:
            self.asks[-1]["calls"][idx]["state"] = "raised"


def executed_calls_in_failed_ask(journal) -> int:
    """How many calls this side finished in the ask that has not ended. 0 when there is none."""
    try:
        with journal._lock:
            if not journal.asks or journal.asks[-1]["finished"]:
                return 0
            return sum(1 for c in journal.asks[-1]["calls"] if c["state"] == "done")
    except Exception:
        return 0


def recording(journal, run_tool, replay=None):
    """`run_tool` wrapped so the journal sees every call. With `replay`, a call this side already
    ran in the interrupted ask is answered from the record instead of being run again."""
    def run(name, args):
        if replay is not None:
            hit = replay.take(name, args)
            if hit is not None:
                return hit
        idx = journal.call_started(name, args)
        try:
            ok, text = run_tool(name, args)
        except BaseException:
            journal.call_raised(idx)
            raise
        journal.call_finished(idx, ok, text)
        return ok, text
    return run


class ReplayCache:
    """Results of calls already run, handed back once each to an identical request."""

    def __init__(self, entries):
        self._d = {}
        for sig, ok, text in entries:
            self._d.setdefault(sig, []).append((bool(ok), str(text)))
        self.replayed = 0

    def take(self, name, args):
        lst = self._d.get(signature(name, args))
        if not lst:
            return None
        ok, text = lst.pop(0)
        self.replayed += 1
        return ok, "[再実行せず、実行済みの結果を返した]\n" + text


# ---- the decision ----------------------------------------------------------------------------

class Plan:
    def __init__(self, ok, why="", recovery=None):
        self.ok, self.why, self.recovery = bool(ok), str(why), recovery


def _no(code, detail=""):
    return Plan(False, code + (": " + detail if detail else ""))


def read_ledger_tail(path=None, max_bytes=LEDGER_TAIL_BYTES):
    """Rows of the server's tool ledger (its tail), or None when it cannot be read."""
    try:
        if path is None:
            from tools import tool_ledger
            path = tool_ledger.LEDGER_PATH
        if not os.path.isfile(path):
            return None
        from relay.mcp_attribution import read_jsonl_tail
        rows, _first = read_jsonl_tail(path, max_bytes)
        return rows
    except Exception:
        return None


def goal_class_ok(goal) -> bool:
    """Repository work through tools, and nothing the model could do with its own tools."""
    text = goal or ""
    return bool(_REPO_WORK.search(text)) and not _NATIVE_VOCABULARY.search(text)


def plan(journal, *, goal, reason, used=0, bridged=True, thread_alive=False, conv_id="",
         worker="", ledger_rows=None) -> Plan:
    """Whether the interrupted ask may be continued, and with what. Every `False` names its cause."""
    if not recovery_on():
        return _no("mode_off")
    if not bridged:
        return _no("not_bridged", "the tools are not run on this side")
    if journal is None or not getattr(journal, "asks", None):
        return _no("no_journal", "the conversation kept no record")
    if not _CONTINUABLE.search(str(reason or "")):
        return _no("reason_not_continuable", str(reason)[:120])
    if thread_alive:
        return _no("old_turn_thread_alive")
    if int(used) >= MAX_CONTINUES_PER_GOAL:
        return _no("cap", "%d continues already used" % int(used))
    if not goal_class_ok(goal):
        return _no("goal_class", "not plain repository work, or it names mail/calendar/chat")
    if not conv_id or journal.completed_exchanges < 1:
        return _no("conversation_unknown", "the backend never completed an exchange in it")
    with journal._lock:
        ask = journal.asks[-1]
        if ask["finished"]:
            return _no("no_failed_round", "the last ask ended normally")
        failed = next((r for r in reversed(ask["rounds"]) if not r["completed"]), None)
        if failed is None:
            return _no("no_failed_round")
        calls = [dict(c) for c in ask["calls"]]
        failed = dict(failed)
        started = ask["started"]
        last_ref = journal.last_ref
        recoveries = journal.recoveries
        completed_exchanges = journal.completed_exchanges
    if any(c["state"] == "running" for c in calls):
        return _no("call_in_flight", "a call has started and not returned")
    if any(c["state"] == "raised" or (c["state"] == "done" and not c["ok"]
                                      and _UNKNOWN_EFFECT.search(c["text"] or ""))
           for c in calls):
        return _no("call_effect_unknown", "a call timed out or raised; the server may have run it")
    done = [c for c in calls if c["state"] == "done"]
    if not done:
        return _no("no_executed_calls", "nothing was executed in this ask; nothing to reconcile")
    if not last_ref:
        return _no("no_ref", "no payload of this conversation carried a ref")
    rows = ledger_rows if ledger_rows is not None else read_ledger_tail()
    if rows is None:
        return _no("ledger_unreadable", "cannot rule out a call that never returned")
    seen = {r.get("id") for r in rows if r.get("event") == "outcome"}
    orphans = [r for r in rows
               if r.get("event") == "call" and r.get("worker") == worker
               and float(r.get("ts") or 0) >= started - 1.0 and r.get("id") not in seen]
    if orphans:
        return _no("ledger_orphan", "%d call(s) entered the server and never returned: %s"
                   % (len(orphans), ",".join(str(o.get("tool")) for o in orphans[:3])))

    accept = {last_ref}
    if failed["sent"]:
        accept.add(failed["ref"])
    carry = TurnJournal()
    carry.completed_exchanges = completed_exchanges
    carry.recoveries = recoveries + 1
    rec = Recovery(
        accept_refs=frozenset(accept),
        continue_text=build_continue_text(failed["raw"], done),
        replay=ReplayCache([(c["sig"], c["ok"], c["text"]) for c in done]),
        job_note="【続き】接続が切れたため、同じ会話で続きを依頼した(実行済みの呼び出し %d 件)。"
                 % len(done),
        carry=carry, executed=len(done))
    return Plan(True, "", rec)


class Recovery:
    def __init__(self, *, accept_refs, continue_text, replay, job_note, carry, executed):
        self.accept_refs = accept_refs
        self.continue_text = continue_text
        self.replay = replay
        self.job_note = job_note
        self.carry = carry
        self.executed = executed
        self.probe_text = PROBE_TEXT


# ---- the texts and the probe -----------------------------------------------------------------

PROBE_TEXT = (
    "接続が途中で切れた。ツールは呼ばず、次の2行だけを答えること。\n"
    "REF=この会話で直前に受け取った [ref:…] の12桁(なければ なし)\n"
    "NATIVE=あなたが call_tool 以外の手段(メール・予定・ファイルなどを自分の機能で操作)で"
    "実行したことがあれば あり、なければ なし")


def build_continue_text(failed_payload, done_calls) -> str:
    lines = []
    for c in done_calls[-40:]:
        label = ""
        if isinstance(c["args"], dict) and isinstance(c["args"].get("name"), str):
            label = " [%s]" % " ".join(c["args"]["name"].split())[:40]
        lines.append("#%d %s%s → %s" % (c["n"] + 1, c["name"], label, "ok" if c["ok"] else "失敗"))
    return (
        "接続が途中で切れたため、同じ会話の続きとして送る。直前の応答は受け取れていない。\n"
        "こちら側で実行した呼び出しは下に記録してある。同じ呼び出しを書いても再実行されず、"
        "実行済みの結果がそのまま返る。\n"
        "実行済みの作業は繰り返さず、続きだけを行うこと。結果が足りなければ、別の呼び出しを書くこと。\n\n"
        "--- 直前に送った実行結果(再掲) ---\n" + str(failed_payload) +
        "\n--- 実行済みの呼び出し(この依頼の分、古い順) ---\n" + "\n".join(lines) +
        "\n--- ここまで ---")


_REF_LINE = re.compile(r"REF\s*[=:：]\s*(.*)")
_NATIVE_LINE = re.compile(r"NATIVE\s*[=:：]\s*(あり|なし)")


def check_probe_reply(reply, accept_refs):
    """(ok, why). The model must name a ref it can only know from the history, and say it acted
    with no tool of its own."""
    text = reply or ""
    m = _REF_LINE.search(text)
    if not m:
        return False, "the reply has no REF line"
    tokens = re.findall(r"[0-9a-f]{12}", m.group(1))
    if not tokens:
        return False, "the model could not name the last ref (an empty conversation?)"
    bad = [t for t in tokens if t not in accept_refs]
    if bad:
        return False, "the ref named (%s) is not one this conversation sent last" % bad[0]
    n = _NATIVE_LINE.search(text)
    if not n:
        return False, "the reply has no NATIVE line"
    if n.group(1) != "なし":
        return False, "the model reports an action outside call_tool"
    return True, ""


def verify(conv, rec, *, connect):
    """Run the probe turn on `conv` (no tools). (True, "") or (False, why). Never raises."""
    try:
        reply = conv.probe(rec.probe_text, connect=connect)
    except Exception as exc:
        return False, "the probe turn failed: %s: %s" % (type(exc).__name__, str(exc)[:160])
    return check_probe_reply(reply, rec.accept_refs)
