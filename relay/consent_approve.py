# -*- coding: utf-8 -*-
"""Press the Work IQ tool-approval cards for the connector-less (socket) route.

WHY THIS EXISTS. A new user's first question that touches Work IQ (Calendar, Copilot, Mail,
OneDrive, SharePoint, Teams, User) is answered with a chain of approval cards, one per tool.
On the old tab route `edge_reconnect._click_consent_chain` pressed them from the worker's own
page. The socket route has no page (relay/no_tab.py forbids opening one for Copilot work), so the
card arrives as a completed turn with no text (relay/socket_driver.py records "carried no text")
and nobody pressed it. The owner's standing requirement is that nobody clicks these by hand.
Design and the facts it rests on: Documents\\フリート設計見直し_20261014\\roadmap\\
同意カード_自動承認の現状_20261020.md (section "最小の案").

WHAT IT DOES. When a CONNECTOR-LESS worker's turn ends without text, `on_socket_failure` starts a
helper process (same shape as relay/socket_capture_async.py: its own CDP connection to the
product's Edge, plain data back on stdout). The helper opens a light page (like
relay/profile_token.py's), goes to the conversation that showed the card, and calls the existing
`_click_consent_chain` with a gate that lets it press a card ONLY when the tool name shown in the
card is one of the 7 known Work IQ tools. No Copilot turn is ever sent: this module cannot reach
a driver, and the page it opens is closed again.

WHY THE SCOPE IS NARROW (the owner's rule for new constraints: a written background). Pressing a
card grants the user's mail, calendar, Teams and files to that tool. The event this prevents is a
card for some OTHER tool (or another agent's conversation) being approved because it happens to
carry an Allow button -- the old chain pressed any visible Allow. So: known names only, the
configured agent's conversation only, and everything else is recorded as `unknown_card` /
`wrong_conversation` and left for a person.

WHAT IS ASSUMED, NOT VERIFIED (checked by the live step in 同意カード_実機検証手順_20261020.md):
  * a card ends the turn as a completed turn with no text (socket_driver's "carried no text");
    which frame type carries it is unknown, so detection uses that observable fact and a worker
    whose model simply said nothing also triggers the helper -- which then finds no card;
  * the card's heading is the tool name (e.g. "Mail" or "Work IQ Mail");
  * <agent url>/conversation/<id> shows the card, for the server or the client conversation id.
If a fact is wrong the helper presses nothing and records why; it never widens.

DEFAULT ON for the connector-less route (`consent_auto_approve`); the connector route and
`off` run none of this.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import threading
import time

KEY = "consent_auto_approve"
DEFAULT = "on"

#: The Work IQ tools whose cards may be pressed (measured chain, relay/edge_reconnect.py).
WORK_IQ_TOOLS = ("Calendar", "Copilot", "Mail", "OneDrive", "SharePoint", "Teams", "User")

#: Words that may surround the tool name in a card heading without changing which tool it is.
_FILLER = frozenset(("work", "iq", "workiq", "microsoft", "365", "m365", "mcp", "server",
                     "tool", "tools"))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(REPO, ".fleet", "consent_approvals.jsonl")

#: The record is rotated to `<LOG>.1` at this size (the idiom of relay/load_routing.py). Background:
#: an append-only file on a machine whose C: has filled before (2026-10-19) must not grow without
#: end; a row is ~300 bytes, so 1 MiB holds thousands of cards.
MAX_BYTES = 1024 * 1024

#: A heading longer than this is not a tool name; it is recorded cut to this length.
HEADING_CAP = 80


# ---- the setting -----------------------------------------------------------------------------

_cache = {}


def _settings_path(path=None):
    if path:
        return str(path)
    from tools.settings_path import settings_file
    return settings_file()


def mode(path=None) -> str:
    """`on` or `off`, from settings.txt. Anything but `off` is `on`. Never raises.

    One os.stat per read and a re-read only when the file changed (declared each_gate in
    tools/settings_keys.py).
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
                if ln.startswith("consent_auto_approve="):
                    v = ln.split("=", 1)[1].strip().lower()
                    val = "off" if v == "off" else "on"
                    break
        _cache[p] = (key, val)
        return val
    except Exception:
        return DEFAULT


# ---- recognising a card ----------------------------------------------------------------------

_NO_TEXT = re.compile(r"carried no text", re.I)


def is_card_turn(failed) -> bool:
    """Whether a driver's `failed` text says a completed turn carried no text (relay/socket_driver.py).
    That is the only fact the code records for a card today (the frame type is unverified)."""
    return bool(_NO_TEXT.search(str(failed or "")))


def classify_heading(heading) -> tuple:
    """(`known`, <tool>) when the card heading names exactly one of the 7 Work IQ tools,
    otherwise (`unknown`, <heading cut to HEADING_CAP>).

    Filler words around the name (Work IQ, Microsoft 365, MCP, tool) are ignored; anything else
    left over makes it unknown, so "Mail Merge Pro" is not "Mail".
    """
    text = " ".join(str(heading or "").split())
    shown = text[:HEADING_CAP]
    tokens = [t for t in re.findall(r"[A-Za-z0-9]+", text.lower()) if t not in _FILLER]
    for name in WORK_IQ_TOOLS:
        if tokens == [name.lower()]:
            return "known", name
    return "unknown", shown


#: Runs in the page on the Allow button. The heading is the first line of the nearest ancestor
#: that has any text besides the button labels.
_HEADING_JS = """el => {
  const labels = new Set(['許可', 'Allow', 'キャンセル', 'Cancel', 'Deny', '拒否', '拒否する']);
  let n = el;
  for (let i = 0; i < 8 && n && n.parentElement; i++) {
    n = n.parentElement;
    const lines = (n.innerText || '').split('\\n').map(s => s.trim())
        .filter(s => s && !labels.has(s));
    if (lines.length) return lines[0];
  }
  return '';
}"""


def card_heading(target) -> str:
    """The heading of the card that owns the Allow button `target` ("" when unreadable)."""
    try:
        return str(target.evaluate(_HEADING_JS) or "")
    except Exception:
        return ""


# ---- what the helper does in a page ----------------------------------------------------------

def _agent_base(agent_url: str) -> str:
    return str(agent_url or "").split("/conversation/", 1)[0].rstrip("/")


def _scoped_chain(page, gate=None, on_click=None) -> bool:
    """relay/edge_reconnect._click_consent_chain with a gate before each press.

    A separate function rather than a parameter on the original: that one is pinned by
    bridge/test_consent_chain.py and is what the tab route and the bridge run, so it stays as it
    was. The rule is the same (press the LAST visible Allow, stop when the visible-Allow count
    stops growing, bounded by CONSENT_CHAIN_MAX); the only addition is that ``gate(target)``
    may refuse a press and ``on_click(target, grew)`` is told about each one.
    """
    from relay.edge_reconnect import CONSENT_CHAIN_MAX, _visible_allow
    clicked = False
    for _ in range(CONSENT_CHAIN_MAX):
        groups = _visible_allow(page)
        if not groups:
            break
        before = sum(n for _, n in groups)
        try:
            target = groups[0][0].last
            if gate is not None and not gate(target):
                break
            target.click()
        except Exception:
            break
        clicked = True
        try:
            page.wait_for_timeout(4000)
        except Exception:
            pass
        after = sum(n for _, n in _visible_allow(page))
        if on_click is not None:
            try:
                on_click(target, after > before)
            except Exception:
                pass
        if after <= before:
            break
    return clicked


def approve_in_page(page, *, agent_url, conversation, chain=None) -> list:
    """Press the known Work IQ cards of ONE conversation page. Returns result rows
    [{"event": pressed|unknown_card|wrong_conversation|no_card, ...}] -- plain data, no content.

    `chain` is the click function (default edge_reconnect._click_consent_chain); tests pass fakes.
    """
    rows = []
    base = _agent_base(agent_url)
    url = str(getattr(page, "url", "") or "")
    if not base or not url.startswith(base + "/") or str(conversation) not in url:
        return [{"event": "wrong_conversation", "conversation": str(conversation),
                 "page": url.split("?", 1)[0][:200]}]
    if chain is None:
        chain = _scoped_chain
    state = {"last": None}

    def gate(target):
        kind, name = classify_heading(card_heading(target))
        if kind == "known":
            state["last"] = name
            return True
        rows.append({"event": "unknown_card", "tool": name, "conversation": str(conversation)})
        return False

    def on_click(target, grew):
        rows.append({"event": "pressed", "tool": state["last"] or "",
                     "conversation": str(conversation), "chain_grew": bool(grew)})

    clicked = chain(page, gate=gate, on_click=on_click)
    if not clicked and not rows:
        rows.append({"event": "no_card", "conversation": str(conversation)})
    return rows


# ---- the record ------------------------------------------------------------------------------

_lock = threading.Lock()
_counts = {"launched": 0, "pressed": 0, "unknown_card": 0, "wrong_conversation": 0,
           "no_card": 0, "error": 0}
_last = {}
_inflight = {}          # conversation id -> process


def _append(row) -> bool:
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        try:
            if os.path.getsize(LOG_PATH) >= MAX_BYTES:
                os.replace(LOG_PATH, LOG_PATH + ".1")
        except OSError:
            pass
        with io.open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        return True
    except Exception:
        with _lock:
            _counts["error"] += 1
        return False


def record(event, **fields) -> None:
    """One line: event, tool name, conversation id, time. Never content. Never raises."""
    now = time.time()
    row = {"ts": round(now, 3), "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
           "event": str(event)}
    row.update({k: v for k, v in fields.items() if v is not None})
    with _lock:
        if event in _counts:
            _counts[event] += 1
        _last.clear()
        _last.update({k: row[k] for k in ("at", "event", "tool", "conversation") if k in row})
    _append(row)


def status_block() -> dict:
    """For status.json `consent_approvals`: counts since this process started, the last row."""
    try:
        with _lock:
            return {"enabled": mode() == "on", **_counts, "in_flight": len(_inflight),
                    "last": dict(_last)}
    except Exception:
        return {}


# ---- starting the helper (parent side) -------------------------------------------------------

def _timeout_s() -> float:
    """The helper's bound is the one the product already gives its CDP helper processes."""
    from relay.socket_capture_async import DEFAULT_TIMEOUT_S
    return float(DEFAULT_TIMEOUT_S)


def on_socket_failure(worker, failed, *, popen=subprocess.Popen, cdp_url=None) -> bool:
    """Called by the fleet when a socket worker's turn failed. Starts the helper iff this is a
    connector-less worker whose turn ended without text and the setting is on. Never raises,
    never blocks; True means a helper was started."""
    try:
        if mode() != "on" or not is_card_turn(failed):
            return False
        from relay import tool_bridge
        if not tool_bridge.worker_is_bridged(worker):        # the connector route: nothing runs
            return False
        agent_url = str(getattr(worker, "_agent_url", "") or "")
        ids = worker.drv.conversation_ids()
        candidates = []
        for k in ("server", "client"):
            v = str((ids or {}).get(k) or "")
            if v and v not in candidates:
                candidates.append(v)
        if not agent_url or not candidates:
            return False
        key = candidates[0]
        with _lock:
            if key in _inflight:
                return False
            _inflight[key] = None
        env = os.environ.copy()
        env["MCP_CONSENT_WORKER_AGENT"] = agent_url
        env["MCP_CONSENT_WORKER_CONVERSATIONS"] = ",".join(candidates)
        env["MCP_CONSENT_WORKER_CDP"] = str(cdp_url or env.get("MCP_CDP_URL")
                                            or "http://localhost:9222")
        env["MCP_CONSENT_WORKER_DEADLINE_S"] = "%.0f" % max(10.0, _timeout_s() - 20.0)
        argv = [sys.executable, "-m", "relay.consent_approve", "--worker"]
        kw = dict(cwd=REPO, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                  stderr=subprocess.PIPE, close_fds=True)
        from tools import childproc
        kw.update(childproc.tree_popen_kwargs(headless=True))
        try:
            proc = popen(argv, **kw)
        except Exception:
            with _lock:
                _inflight.pop(key, None)
            raise
        with _lock:
            _inflight[key] = proc
            _counts["launched"] += 1
        threading.Thread(target=_wait_one, args=(key, str(getattr(worker, "name", "")), proc),
                         name="consent-approve-waiter", daemon=True).start()
        return True
    except Exception:
        return False


def _wait_one(key, worker_name, proc) -> None:
    from tools import childproc
    try:
        try:
            raw_out, raw_err = proc.communicate(timeout=_timeout_s())
        except subprocess.TimeoutExpired:
            childproc.kill_tree(proc, wait_s=2.0)          # our helper only, never the Edge
            record("error", worker=worker_name, conversation=key, why="helper timed out")
            return
        if getattr(proc, "returncode", 0) not in (0, None):
            err = childproc.decode(raw_err).strip().replace("\n", " ")[-200:]
            record("error", worker=worker_name, conversation=key,
                   why="helper exit %s: %s" % (proc.returncode, err))
            return
        data = json.loads(childproc.decode(raw_out).strip() or "{}")
        for row in data.get("rows") or []:
            ev = str(row.pop("event", "error"))
            record(ev, worker=worker_name, **{k: v for k, v in row.items()
                                              if k in ("tool", "conversation", "chain_grew", "page")})
    except Exception as exc:
        record("error", worker=worker_name, conversation=key,
               why="%s: %s" % (type(exc).__name__, str(exc)[:120]))
    finally:
        with _lock:
            _inflight.pop(key, None)


# ---- the helper process (child side) ---------------------------------------------------------

def _worker_main() -> int:
    """Own CDP connection, one light page, JSON rows to stdout only. Sends no Copilot turn."""
    import contextlib
    cdp_url = os.environ.get("MCP_CONSENT_WORKER_CDP") or "http://localhost:9222"
    agent_url = os.environ.get("MCP_CONSENT_WORKER_AGENT") or ""
    convs = [c for c in (os.environ.get("MCP_CONSENT_WORKER_CONVERSATIONS") or "").split(",") if c]
    deadline = time.time() + float(os.environ.get("MCP_CONSENT_WORKER_DEADLINE_S") or "160")
    if not agent_url or not convs:
        print("consent worker: missing agent url or conversation", file=sys.stderr, flush=True)
        return 2
    rows = []
    try:
        with contextlib.redirect_stdout(sys.stderr):
            from playwright.sync_api import sync_playwright
            from relay.profile_token import _claim, _release
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(cdp_url, timeout=20000)
                if not browser.contexts:
                    raise RuntimeError("CDP browser has no context")
                page = browser.contexts[0].new_page()
                _claim(page, "consent approve")
                try:
                    for i, cid in enumerate(convs):
                        if time.time() >= deadline:
                            break
                        # The time left is shared by the candidates still to try, so a first id
                        # that shows nothing cannot use up the second one's turn.
                        slot_end = time.time() + (deadline - time.time()) / (len(convs) - i)
                        try:
                            page.goto(_agent_base(agent_url) + "/conversation/" + cid,
                                      wait_until="domcontentloaded", timeout=30000)
                        except Exception:
                            pass
                        # The cards render after the load event; wait for one, within the deadline.
                        while time.time() < slot_end:
                            if page.locator('button:has-text("許可"), button:has-text("Allow")'
                                            ).locator("visible=true").count():
                                break
                            page.wait_for_timeout(500)
                        got = approve_in_page(page, agent_url=agent_url, conversation=cid)
                        rows.extend(got)
                        if any(r["event"] in ("pressed", "unknown_card") for r in got):
                            break
                finally:
                    _release(page)
        sys.stdout.write(json.dumps({"rows": rows}, ensure_ascii=False))
        sys.stdout.flush()
        return 0
    except Exception as exc:
        print("consent worker failed: %s: %s" % (type(exc).__name__, str(exc)[:500]),
              file=sys.stderr, flush=True)
        return 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--worker"]:
        return _worker_main()
    print("usage: python -m relay.consent_approve --worker", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
