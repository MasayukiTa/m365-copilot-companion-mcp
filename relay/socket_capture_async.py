"""Refresh the fleet socket credential without freezing the fleet sweep.

The expensive part of a socket refresh is browser work (sync Playwright).  Moving that work to
another THREAD is invalid: sync Playwright objects belong to the thread/event-loop that created
them.  Moving it to another PROCESS is different.  The helper process connects to the same Edge
through CDP, creates its own Playwright connection on its own thread, captures, closes the page,
and returns only plain token/template data through a private stdout pipe.

The fleet sweep never waits for that process.  While a refresh is in flight an already-live token
may still serve sockets; with no usable token goals WAIT (relay/token_provider.py) -- there is no
tab path, and the capture never sends a Copilot turn.  The sweep kicks this on every lap while any
goal is unfinished (not only while the admission queue is non-empty), and a failed capture is
retried with a bounded backoff (30, 60, 120 s ... never longer while a goal is waiting).
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import time

from tools import childproc

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: Raised from 90 s: the stages inside one capture can add up past it (a cold Edge plus the
#: page's own token request), so 90 s timed out a capture that was working.
DEFAULT_TIMEOUT_S = float(os.environ.get("MCP_SOCKET_CAPTURE_TIMEOUT_S", "180"))


def _cap_state():
    """The process-wide capture counters that status.json `token_wait` reports."""
    from relay.token_provider import capture_state
    return capture_state()


class AsyncCaptureManager:
    """One non-blocking capture launcher per SocketRoute object.

    The manager itself never receives a Playwright context/page.  It only owns child processes,
    plain strings, and the route's thread-safe install method.
    """

    def __init__(self, *, popen=subprocess.Popen, interval_s=None, timeout_s=None,
                 now=time.time, log=None):
        from relay.capture_floor import MIN_CAPTURE_INTERVAL_S
        self.interval_s = MIN_CAPTURE_INTERVAL_S if interval_s is None else float(interval_s)
        self.timeout_s = DEFAULT_TIMEOUT_S if timeout_s is None else float(timeout_s)
        self._popen = popen
        self._now = now
        # Exp-aware pacing: same-exp results and failures lengthen the interval (see
        # relay/token_provider.py). The base is this manager's own interval_s.
        from relay.token_provider import NO_TOKEN_BASE_S, CapturePolicy
        self.policy = CapturePolicy(base_s=self.interval_s, now=now,
                                    no_token_base_s=min(self.interval_s, NO_TOKEN_BASE_S))
        self._log = log or (lambda m: print(m, flush=True))
        self._lock = threading.Lock()
        self._inflight = {}       # agent key -> process
        self._last_attempt = {}   # agent key -> timestamp; applies to failures too (no spawn storm)
        self._completed = 0
        self._failed = 0
        self._discarded = 0

    def consider(self, route, agent_url, cdp_url=None) -> bool:
        """Start a helper if this route needs one; return immediately.

        True means a helper was started, not that capture already succeeded.  Repeated calls for
        the same agent while a helper is live, or inside the capture floor, are cheap no-ops.
        """
        try:
            if not route.open() or not route.needs_refresh(agent_url):
                return False
        except Exception:
            return False
        key = str(agent_url or "")
        now = float(self._now())
        try:
            have_token = bool(route.ready(key))
        except Exception:
            have_token = False
        with self._lock:
            if key in self._inflight:
                return False
            # The flat capture floor protects an idle fleet that already has a token. When NO
            # token is in hand a goal is waiting on this capture, and the policy's own bounded
            # backoff (30/60/120 s) is the only pacing -- a flat 120 s floor here would make the
            # first retry after a transient failure four times slower than it needs to be.
            previous = float(self._last_attempt.get(key) or 0.0)
            if have_token and previous and now - previous < self.interval_s:
                return False
            if not self.policy.allow(key, now, held=not have_token):
                return False
            self._last_attempt[key] = now
            self.policy.on_attempt(key, now)
        _cap_state().attempt()

        # Snapshot the route generation BEFORE process launch. A synchronous Research/Refuter
        # refresh can complete while Popen is constructing the helper; reading the generation
        # afterwards would incorrectly bless that newer state as this older helper's baseline.
        try:
            revision = route.capture_revision(key)
        except Exception:
            revision = 0

        env = os.environ.copy()
        env["MCP_CAPTURE_WORKER_AGENT"] = key
        env["MCP_CAPTURE_WORKER_CDP"] = str(cdp_url or env.get("MCP_CDP_URL") or
                                            "http://localhost:9222")
        argv = [sys.executable, "-m", "relay.socket_capture_async", "--worker"]
        kw = dict(cwd=REPO, env=env, stdin=subprocess.DEVNULL,
                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
        kw.update(childproc.tree_popen_kwargs(headless=True))
        try:
            proc = self._popen(argv, **kw)
        except Exception as exc:
            with self._lock:
                self._failed += 1
            why = "launch failed: %s: %s" % (type(exc).__name__, str(exc)[:160])
            delay = self.policy.on_failure(key, have_token=have_token, why=why)
            _cap_state().fail(why, delay)
            try:
                route.note_capture_failure("async capture " + why)
            except Exception:
                pass
            return False

        with self._lock:
            self._inflight[key] = proc
        thread = threading.Thread(target=self._wait_one,
                                  args=(route, key, revision, proc),
                                  name="socket-capture-waiter", daemon=True)
        thread.start()
        return True

    def _wait_one(self, route, key, revision, proc):
        try:
            try:
                raw_out, raw_err = proc.communicate(timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                # Only OUR helper process tree is killed -- never the Edge. Keep the tail of its
                # stderr so a timeout says which stage it was in (it used to be discarded).
                childproc.kill_tree(proc, wait_s=2.0)
                tail = ""
                try:
                    _o, _e = proc.communicate(timeout=2.0)
                    tail = childproc.decode(_e).strip().replace("\r", " ").replace("\n", " ")[-200:]
                except Exception:
                    pass
                raise RuntimeError("capture helper exceeded %.0fs%s"
                                   % (self.timeout_s, (" (last: %s)" % tail) if tail else ""))
            rc = getattr(proc, "returncode", None)
            if rc not in (0, None):
                err = childproc.decode(raw_err).strip().replace("\r", " ").replace("\n", " ")
                raise RuntimeError("capture helper exit %s: %s" % (rc, err[-300:]))
            text = childproc.decode(raw_out).strip()
            data = json.loads(text)
            token = str(data.get("token") or "")
            from relay.chathub import RequestTemplate
            template = RequestTemplate(data.get("query") or {}, data.get("frame") or {})
            if not token or not template.gpt_id:
                raise ValueError("capture helper returned no usable token/agent template")
            installed = bool(route.install_capture(token, template, key,
                                                   expected_revision=revision))
            try:
                have = bool(route.ready(key))
            except Exception:
                have = True
            delay = self.policy.on_success(key, token, have_token=have)
            if installed:
                _cap_state().ok(delay)
                with self._lock:
                    self._completed += 1
                try:
                    from relay.capture_status import record_success
                    record_success(token, template, key)
                except Exception:
                    pass
                self._log("[socket_capture_async] refresh installed for agent %s"
                          % ((template.gpt_id or "(none)")[:28],))
            else:
                # Not an error: the route may have closed, or another route user installed a
                # newer generation while this helper was away. Never roll that state backward.
                with self._lock:
                    self._discarded += 1
                _cap_state().done()
                _cap_state().set_next(delay)
                self._log("[socket_capture_async] stale/closed capture discarded for %s"
                          % (key[:40] or "(default)"))
        except Exception as exc:
            with self._lock:
                self._failed += 1
            try:
                have = bool(route.ready(key))
            except Exception:
                have = False
            delay = self.policy.on_failure(key, have_token=have, why="%s: %s" % (
                type(exc).__name__, str(exc)[:120]))
            _cap_state().fail("%s: %s" % (type(exc).__name__, str(exc)[:160]), delay)
            try:
                from relay.capture_status import record_failure
                record_failure(exc, key)
            except Exception:
                pass
            try:
                route.note_capture_failure("async capture failed for %s: %s: %s" %
                                   (key[:40] or "(default)", type(exc).__name__, str(exc)[:180]))
            except Exception:
                pass
            self._log("[socket_capture_async] refresh failed: %s: %s"
                      % (type(exc).__name__, str(exc)[:180]))
        finally:
            with self._lock:
                if self._inflight.get(key) is proc:
                    self._inflight.pop(key, None)

    def stats(self) -> dict:
        with self._lock:
            return {"inflight": len(self._inflight), "completed": self._completed,
                    "failed": self._failed, "discarded": self._discarded,
                    "interval_s": self.interval_s,
                    "policy": {k: self.policy.snapshot(k) for k in self._last_attempt}}


def _worker_main() -> int:
    """Child side: own Playwright connection, one capture, JSON result to stdout only."""
    cdp_url = os.environ.get("MCP_CAPTURE_WORKER_CDP") or "http://localhost:9222"
    agent_url = os.environ.get("MCP_CAPTURE_WORKER_AGENT") or ""
    if not agent_url:
        print("capture worker: missing agent url", file=sys.stderr, flush=True)
        return 2
    try:
        # Redirect every incidental capture print away from stdout. stdout is a private protocol
        # carrying the credential back to the parent; mixing diagnostics into it would both break
        # parsing and make accidental token logging easier to overlook.
        with contextlib.redirect_stdout(sys.stderr):
            from playwright.sync_api import sync_playwright
            from relay.profile_token import capture_fn, capture_via_profile
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(cdp_url, timeout=20000)
                if not browser.contexts:
                    raise RuntimeError("CDP browser has no context")
                context = browser.contexts[0]
                fn = capture_fn()
                if fn is capture_via_profile:
                    token, template = fn(context, agent_url,
                                         log=lambda m: print(m, file=sys.stderr, flush=True))
                else:
                    token, template = fn(context, agent_url)
        payload = {"token": token, "query": dict(template.query), "frame": template.frame}
        sys.stdout.write(json.dumps(payload, ensure_ascii=False))
        sys.stdout.flush()
        return 0
    except Exception as exc:
        print("capture worker failed: %s: %s" % (type(exc).__name__, str(exc)[:500]),
              file=sys.stderr, flush=True)
        return 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--worker"]:
        return _worker_main()
    print("usage: python -m relay.socket_capture_async --worker", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
