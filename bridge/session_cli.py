"""session_cli.py -- terminal REPL for the local Copilot bridge.

Talks HTTP to bridge/copilot_bridge.py (a stdlib http.server on 127.0.0.1).
Pure stdlib only, so this imports cleanly on the ubuntu CI box (no playwright
there).

Wire format, from Handler._sse()/_stream() in copilot_bridge.py:
    chunk = (f"event: {event}\n" if event else "") + f"data: {json.dumps(data)}\n\n"
Classic SSE: optional "event: <name>" line, then "data: <json>", blank line
terminator. data shapes: {"delta": "..."} appends, {"replace": "..."} replaces
the running reply; a bare {} with event "done" ends the turn. A raw ": ping"
comment line (server disconnect probe) carries no data and is ignored.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_PORT = 8765
HELP_TEXT = """\
Commands:
  /sessions          list known sessions (chat + fleet, unless --chat-only)
  /resume <n|sid>    switch active session; picking a [fleet] row adopts it
  /new [title]       start a new session
  /goal <text>       work mode: autonomous multi-turn loop. Type while it
                     runs to steer (injected at the next turn boundary);
                     Ctrl+C asks it to stop after the current turn.
  /goal <text> :: <acceptance criteria>
                     same, but adds a verification phase: after the work
                     loop finishes, a critic checks the AC and can send it
                     back for fixes (up to max_loops, default 3).
  /help              show this help
  /quit              exit
Anything else is sent to Copilot as a single message.
"""


def bridge_base_url() -> str:
    """Same env var + default the bridge server itself binds to."""
    port = int(os.environ.get("MCP_BRIDGE_PORT", str(DEFAULT_PORT)))
    return f"http://127.0.0.1:{port}"


# Pure helpers (unit-tested without network)

def format_relative_time(ts: float, now: float | None = None) -> str:
    """Render a unix timestamp as a short relative string like '2h ago'."""
    if now is None:
        now = time.time()
    delta = max(0, now - ts)
    if delta < 5:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    if delta < 86400 * 30:
        return f"{int(delta // 86400)}d ago"
    return f"{int(delta // (86400 * 30))}mo ago"


def session_label(sess: dict) -> str:
    """Title if present, else the first user line, else the sid."""
    title = (sess.get("title") or "").strip()
    if title:
        return title
    return sess.get("sid", "?")


def is_adoptable(sess: dict) -> bool:
    """A [fleet] row needs a non-empty conv_url before it can be adopted."""
    if sess.get("source") != "fleet":
        return True
    return bool(sess.get("conv_url"))


def render_picker(sessions: list, now: float | None = None) -> str:
    """Render the numbered picker text for --resume / /sessions.

    One line per session: index, source tag (when present), label, relative
    time, turn count, status. Fleet rows without a conv_url are flagged as
    not adoptable. Index is 1-based to match what a user types back.
    """
    if not sessions:
        return "(no sessions yet)"
    lines = []
    for i, sess in enumerate(sessions, start=1):
        label = session_label(sess)
        rel = format_relative_time(sess.get("last_active_ts", 0), now)
        turns = sess.get("turns")
        turns_str = f"{turns} turns" if turns is not None else "? turns"
        status = sess.get("status", "?")
        source = sess.get("source")
        tag = f"[{source}] " if source else ""
        suffix = "" if is_adoptable(sess) else "  (not adoptable)"
        lines.append(f"  {i}. {tag}{label}  ({rel}, {turns_str}, {status}){suffix}")
    return "\n".join(lines)


def resolve_picker_choice(token: str, sessions: list):
    """Map a user's typed token (number or sid) to a session dict, or None."""
    token = token.strip()
    if not token:
        return None
    if token.isdigit():
        idx = int(token)
        if 1 <= idx <= len(sessions):
            return sessions[idx - 1]
        return None
    for sess in sessions:
        if sess.get("sid") == token:
            return sess
    return None


class SSEEvent:
    __slots__ = ("event", "data")

    def __init__(self, event, data):
        self.event = event
        self.data = data


def parse_sse_lines(lines):
    """Parse raw SSE lines (trailing newline optional) into SSEEvent objects.

    "event: <name>" sets the pending event name for the next data line;
    "data: <json>" closes a record; ": ping" (keepalive comment, no data) and
    blank separator lines are skipped.
    """
    events = []
    pending_event = None
    for raw in lines:
        line = raw.rstrip("\n").rstrip("\r")
        if line == "":
            pending_event = None
            continue
        if line.startswith(":"):
            continue  # comment/ping, no payload
        if line.startswith("event:"):
            pending_event = line[len("event:"):].strip()
            continue
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            try:
                data = json.loads(payload) if payload else {}
            except ValueError:
                data = {}
            events.append(SSEEvent(pending_event, data))
            pending_event = None
        # else: unrecognized line shape -- ignore rather than raise
    return events


def apply_stream_event(current_text: str, ev: "SSEEvent"):
    """Fold one parsed SSE event into the running assistant text.

    Returns (new_text, is_done).
    """
    if ev.event == "done":
        return current_text, True
    data = ev.data or {}
    if "replace" in data:
        return data["replace"], False
    if "delta" in data:
        return current_text + data["delta"], False
    return current_text, False


def goal_summary(data: dict) -> str:
    """Render the goal_done payload as the final summary line."""
    outcome = data.get("outcome", "?")
    turns = data.get("turns", 0)
    if outcome == "done":
        return f"goal done ({turns} turns)"
    if outcome == "stopped":
        return f"stopped after {turns} turns"
    if outcome == "max_turns":
        return f"max turns reached ({turns} turns)"
    if outcome == "done_verified":
        return f"goal done + verified ({turns} turns)"
    if outcome == "verify_failed":
        return f"verification FAILED after max loops ({turns} turns)"
    if outcome == "escalate_oscillation":
        return f"escalated: same ACs failing repeatedly - human review needed ({turns} turns)"
    return f"goal ended: {outcome} after {turns} turns"


def split_goal_arg(arg: str):
    """Split a /goal argument on the first literal ' :: ' separator.

    Returns (text, ac) where ac is "" when no separator is present. Only the
    first occurrence splits; the AC side keeps any further " :: " verbatim.
    Both sides are stripped of surrounding whitespace.
    """
    sep = " :: "
    idx = arg.find(sep)
    if idx == -1:
        return arg.strip(), ""
    text = arg[:idx].strip()
    ac = arg[idx + len(sep):].strip()
    return text, ac


def render_verify_start(loop_n) -> str:
    return f"--- verify (loop {loop_n}) ---"


def render_verdict(verdict: dict) -> str:
    """Render a {"verdict": {...}} payload as a one-line status."""
    passed = verdict.get("pass")
    if passed:
        return "verdict: PASS"
    failed_ac = verdict.get("failed_ac") or []
    reasons = verdict.get("reasons") or []
    reason_str = "; ".join(reasons)
    if len(reason_str) > 120:
        reason_str = reason_str[:120]
    ac_str = ", ".join(failed_ac)
    return f"verdict: FAIL [{ac_str}] {reason_str}".rstrip()


# HTTP layer (thin; mocked/stubbed in tests)

class BridgeClient:
    def __init__(self, base_url: str | None = None, timeout: float = 10.0):
        self.base_url = base_url or bridge_base_url()
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=self.timeout) as resp:
            return resp.read()

    def is_up(self) -> bool:
        try:
            self._get("/")
            return True
        except Exception:
            return False

    def list_sessions(self) -> list:
        body = self._get("/sessions")
        obj = json.loads(body.decode("utf-8"))
        return obj.get("sessions", [])

    def list_sessions_all(self) -> list:
        """Unified chat+fleet listing (GET /sessions?all=1)."""
        body = self._get("/sessions", {"all": "1"})
        obj = json.loads(body.decode("utf-8"))
        return obj.get("sessions", [])

    def resume(self, sid: str) -> dict:
        body = self._get("/resume", {"sid": sid})
        return json.loads(body.decode("utf-8"))

    def adopt(self, url: str, title: str = "") -> dict:
        body = self._get("/adopt", {"url": url, "title": title})
        return json.loads(body.decode("utf-8"))

    def new_session(self, title: str = "") -> dict:
        body = self._get("/new", {"title": title} if title else None)
        return json.loads(body.decode("utf-8"))

    def _sse_lines(self, path: str, params: dict):
        """Yield raw decoded text lines from an SSE endpoint."""
        url = self.base_url + path + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(urllib.request.Request(url), timeout=None) as resp:
            for raw in resp:
                yield raw.decode("utf-8", errors="replace")

    def stream(self, msg: str):
        return self._sse_lines("/stream", {"msg": msg})

    def goal(self, text: str, ac: str = "", max_loops: int | None = None):
        params = {"text": text}
        if ac:
            params["ac"] = ac
            params["max_loops"] = max_loops if max_loops is not None else 3
        return self._sse_lines("/goal", params)

    def send(self, sid: str, msg: str) -> dict:
        body = self._get("/send", {"sid": sid, "msg": msg})
        return json.loads(body.decode("utf-8"))

    def stop(self) -> dict:
        body = self._get("/stop")
        return json.loads(body.decode("utf-8"))


# Bridge autostart

def autostart_bridge(repo_root: str) -> None:
    script = os.path.join(repo_root, "scripts", "start_bridge.ps1")
    cmd = ["powershell", "-NoProfile", "-File", script, "-Keepalive"]
    subprocess.Popen(
        cmd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )


def wait_for_bridge(client: "BridgeClient", timeout_s: int = 120, out=sys.stdout) -> bool:
    start = time.time()
    dot_count = 0
    while time.time() - start < timeout_s:
        if client.is_up():
            out.write("\rbridge is up.                    \n")
            out.flush()
            return True
        dot_count += 1
        out.write(f"\rwaiting for bridge to start{'.' * (dot_count % 4):<4}")
        out.flush()
        time.sleep(1)
    out.write("\rbridge did not come up within timeout.\n")
    out.flush()
    return False


# CLI argument parsing

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m bridge.session_cli",
        description="Terminal REPL for the local Copilot bridge (session resume/continue).",
    )
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--continue", "-c", dest="cont", action="store_true",
                      help="auto-resume the most recent session immediately")
    grp.add_argument("--resume", "-r", dest="resume", action="store_true",
                      help="show a numbered picker of sessions to resume")
    p.add_argument("--chat-only", dest="chat_only", action="store_true",
                    help="restore the old bridge-only listing (plain /sessions, no fleet rows)")
    return p


# REPL

def _stdout_reconfigure():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _ensure_bridge_up(client: BridgeClient, repo_root: str) -> bool:
    if client.is_up():
        return True
    print("bridge not reachable; starting it now...")
    autostart_bridge(repo_root)
    return wait_for_bridge(client, timeout_s=120)


def _print_stream(client: BridgeClient, msg: str) -> None:
    text = ""
    printed_len = 0
    try:
        for line in client.stream(msg):
            for ev in parse_sse_lines([line]):
                text, done = apply_stream_event(text, ev)
                if len(text) > printed_len:
                    sys.stdout.write(text[printed_len:])
                    sys.stdout.flush()
                    printed_len = len(text)
                elif len(text) < printed_len:
                    # a "replace" shrank/rewrote the text; reprint the tail delta
                    sys.stdout.write("\n" + text)
                    sys.stdout.flush()
                    printed_len = len(text)
                if done:
                    print()
                    return
    except KeyboardInterrupt:
        print("\n[stopped]")
        return
    print()


def run_goal(client, text: str, kbd_q, out=None, stop_grace: float = 60.0,
             ac: str = "", max_loops: int | None = None) -> None:
    """Work mode: stream /goal, multiplexing SSE chunks with keyboard lines.

    kbd_q is a queue of lines typed while streaming; each is sent immediately
    via /send (steering, injected server-side at the next turn boundary).
    First Ctrl+C -> /stop and keep consuming until goal_done (or grace runs
    out); second Ctrl+C -> hard-return to the prompt.

    When ac is non-empty, the server runs a VERIFICATION phase after the work
    loop reaches DONE: {"verify_start": n} and {"verdict": {...}} payloads are
    interleaved in the stream and rendered as render-only events.
    """
    out = out or sys.stdout
    sid = ""
    try:
        sessions = client.list_sessions()
        if sessions:
            sid = sessions[0].get("sid", "")
    except Exception:
        pass
    sse_q: queue.Queue = queue.Queue()

    def _pump():
        try:
            for ln in client.goal(text, ac=ac, max_loops=max_loops):
                sse_q.put(ln)
        except Exception as e:
            sse_q.put('data: {"delta": "[goal stream error: %s]"}\n' % str(e).replace('"', "'"))
        sse_q.put(None)                     # end-of-stream sentinel

    threading.Thread(target=_pump, daemon=True).start()
    acc, printed = "", 0
    stop_deadline = None
    while True:
        try:
            while True:                     # drain keyboard lines first
                try:
                    k = kbd_q.get_nowait()
                except queue.Empty:
                    break
                if k is None:               # stdin EOF: hand it back to the REPL
                    kbd_q.put(None)
                    break
                k = k.strip()
                if not k:
                    continue
                try:
                    client.send(sid, k)
                    out.write("[queued for next turn]\n")
                except Exception as e:
                    out.write(f"[send failed: {e}]\n")
                out.flush()
            try:
                line = sse_q.get(timeout=0.2)
            except queue.Empty:
                if stop_deadline is not None and time.time() > stop_deadline:
                    out.write("[gave up waiting for the goal to stop]\n")
                    return
                continue
            if line is None:
                return
            for ev in parse_sse_lines([line]):
                data = ev.data or {}
                if ev.event == "done":
                    return
                if "turn_done" in data:
                    out.write(f"\n--- turn {data['turn_done']} ---\n")
                    acc, printed = "", 0
                elif "steered" in data:
                    out.write(f"[steered: {data['steered']}]\n")
                elif "verify_start" in data:
                    out.write(f"\n{render_verify_start(data['verify_start'])}\n")
                    acc, printed = "", 0
                elif "verdict" in data:
                    out.write(render_verdict(data["verdict"]) + "\n")
                elif "goal_done" in data:
                    out.write(goal_summary(data) + "\n")
                elif "replace" in data or "delta" in data:
                    acc, _ = apply_stream_event(acc, ev)
                    if len(acc) > printed:
                        out.write(acc[printed:])
                    elif len(acc) < printed:
                        out.write("\n" + acc)
                    printed = len(acc)
                out.flush()
        except KeyboardInterrupt:
            if stop_deadline is not None:
                out.write("\n[returning to prompt]\n")
                return
            try:
                client.stop()
            except Exception:
                pass
            out.write("\n[stop requested; finishing current turn]\n")
            out.flush()
            stop_deadline = time.time() + stop_grace


class StdinReader:
    """Single daemon thread reading stdin into a queue.

    The REPL reads prompt lines from the same queue, so lines typed during
    /goal streaming are steered instead of being swallowed by a blocked
    input() call. The poll loop keeps Ctrl+C deliverable to the main thread.
    """

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in sys.stdin:
            self.q.put(line.rstrip("\r\n"))
        self.q.put(None)                    # EOF sentinel

    def readline(self, prompt: str = "") -> str:
        if prompt:
            sys.stdout.write(prompt)
            sys.stdout.flush()
        while True:
            try:
                item = self.q.get(timeout=0.3)
            except queue.Empty:
                continue
            if item is None:
                raise EOFError
            return item


def _list_sessions_for(client: BridgeClient, chat_only: bool) -> list:
    if chat_only:
        return client.list_sessions()
    return client.list_sessions_all()


def _do_sessions(client: BridgeClient, chat_only: bool = False) -> list:
    try:
        sessions = _list_sessions_for(client, chat_only)
    except Exception as e:
        print(f"[error listing sessions: {e}]")
        return []
    print(render_picker(sessions))
    return sessions


def _do_resume(client: BridgeClient, token: str, sessions_cache: list, chat_only: bool = False) -> None:
    if not sessions_cache:
        sessions_cache[:] = _list_sessions_for(client, chat_only)
    chosen = resolve_picker_choice(token, sessions_cache)
    if chosen is None:
        print(f"[no such session: {token}]")
        return
    if not is_adoptable(chosen):
        print(f"[{session_label(chosen)} has no conversation url yet; not adoptable]")
        return
    if chosen.get("source") == "fleet":
        try:
            result = client.adopt(chosen.get("conv_url", ""), session_label(chosen))
        except Exception as e:
            print(f"[error adopting: {e}]")
            return
        if result.get("ok"):
            print(f"[adopted {session_label(chosen)}]")
        else:
            print(f"[adopt failed: {result.get('error', 'unknown error')}]")
        return
    try:
        result = client.resume(chosen["sid"])
    except Exception as e:
        print(f"[error resuming: {e}]")
        return
    if result.get("ok"):
        print(f"[resumed {session_label(chosen)}]")
    else:
        print(f"[resume failed: {result.get('error', 'unknown error')}]")


def _do_new(client: BridgeClient, title: str) -> None:
    try:
        result = client.new_session(title)
    except Exception as e:
        print(f"[error creating session: {e}]")
        return
    if result.get("ok", True):
        print("[new session started]")
    else:
        print(f"[new session failed: {result.get('error', 'unknown error')}]")


def repl(client: BridgeClient, reader: StdinReader | None = None, chat_only: bool = False) -> None:
    reader = reader or StdinReader()
    print(HELP_TEXT)
    empty_ctrl_c = False
    while True:
        try:
            line = reader.readline("> ")
        except EOFError:
            print()
            return
        except KeyboardInterrupt:
            if empty_ctrl_c:
                print()
                return
            empty_ctrl_c = True
            print("\n[press Ctrl+C again to exit]")
            continue
        empty_ctrl_c = False
        line = line.strip()
        if not line:
            continue
        if line.startswith("/"):
            cmd, _, arg = line[1:].partition(" ")
            cmd = cmd.lower()
            arg = arg.strip()
            if cmd in ("quit", "exit", "q"):
                return
            if cmd == "help":
                print(HELP_TEXT)
            elif cmd == "sessions":
                _do_sessions(client, chat_only=chat_only)
            elif cmd == "resume":
                _do_resume(client, arg, [], chat_only=chat_only)
            elif cmd == "new":
                _do_new(client, arg)
            elif cmd == "goal":
                if not arg:
                    print("usage: /goal <text> [:: acceptance criteria]")
                else:
                    goal_text, ac = split_goal_arg(arg)
                    if not goal_text:
                        print("usage: /goal <text> [:: acceptance criteria]")
                    elif ac:
                        run_goal(client, goal_text, reader.q, ac=ac, max_loops=3)
                    else:
                        run_goal(client, goal_text, reader.q)
            else:
                print(f"[unknown command /{cmd}. Type /help for a list.]")
            continue
        _print_stream(client, line)


def _print_banner_and_maybe_resume(client: BridgeClient, auto_continue: bool) -> None:
    try:
        sessions = client.list_sessions()
    except Exception:
        sessions = []
    if not sessions:
        print("(starting a fresh session)")
        return
    latest = sessions[0]
    if auto_continue:
        _do_resume(client, latest.get("sid", ""), sessions)
        return
    print(f"latest session: {session_label(latest)} ({format_relative_time(latest.get('last_active_ts', 0))})")
    print("Type /resume to switch, or just start typing to continue where you left off.")


def main(argv=None) -> int:
    _stdout_reconfigure()
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    client = BridgeClient()

    if not _ensure_bridge_up(client, repo_root):
        print("[could not reach bridge; giving up]")
        return 1

    chat_only = args.chat_only

    if args.resume:
        sessions = _list_sessions_for(client, chat_only)
        print(render_picker(sessions))
        if sessions:
            token = input("resume which? (number, blank to cancel): ").strip()
            if token:
                _do_resume(client, token, sessions, chat_only=chat_only)
    elif args.cont:
        _print_banner_and_maybe_resume(client, auto_continue=True)
    else:
        _print_banner_and_maybe_resume(client, auto_continue=False)

    repl(client, chat_only=chat_only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
