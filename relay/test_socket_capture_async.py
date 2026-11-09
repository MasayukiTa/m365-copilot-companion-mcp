from __future__ import annotations

import json
import time

import pytest
from pathlib import Path

from relay.chathub import RequestTemplate
from relay.socket_route import SocketRoute


@pytest.fixture(autouse=True)
def _no_live_capture_status(monkeypatch):
    import relay.capture_status as cs
    monkeypatch.setattr(cs, "record_success", lambda *_a, **_k: None)
    monkeypatch.setattr(cs, "record_failure", lambda *_a, **_k: None)


def _wait_done(mgr, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline and mgr.stats()["inflight"]:
        time.sleep(0.01)
    assert mgr.stats()["inflight"] == 0


def _token(seconds=3600):
    import base64
    body = json.dumps({"oid":"o","tid":"t","exp":time.time()+seconds}).encode()
    return "x." + base64.urlsafe_b64encode(body).decode().rstrip("=") + ".y"


def _payload():
    tpl = RequestTemplate({"gptId":"x"}, {"threadLevelGptId":{"id":"T_agent.x"}})
    return json.dumps({"token":_token(), "query":tpl.query, "frame":tpl.frame}).encode("utf-8")


class _SlowProc:
    def __init__(self, delay=0.25, rc=0, out=None, err=b""):
        self.delay = delay
        self.returncode = rc
        self._out = _payload() if out is None else out
        self._err = err
        self.pid = 4242
        self.killed = False

    def communicate(self, timeout=None):
        time.sleep(self.delay)
        return self._out, self._err

    def poll(self):
        return self.returncode


class _Factory:
    def __init__(self, delay=0.25):
        self.delay = delay
        self.calls = []
        self.procs = []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), dict(kw)))
        p = _SlowProc(self.delay)
        self.procs.append(p)
        return p


def _route():
    return SocketRoute(enabled=True, capture_fn=lambda *_: (_token(), None), connect_fn=object())


def test_consider_returns_without_waiting_for_capture_process():
    from relay.socket_capture_async import AsyncCaptureManager
    fac = _Factory(delay=0.35)
    mgr = AsyncCaptureManager(popen=fac, interval_s=120, timeout_s=2, log=lambda _m: None)
    r = _route()
    t0 = time.perf_counter()
    assert mgr.consider(r, "https://agent.invalid", "http://localhost:9222") is True
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.10, elapsed
    assert len(fac.calls) == 1
    _wait_done(mgr)


def test_same_agent_gets_only_one_inflight_capture():
    from relay.socket_capture_async import AsyncCaptureManager
    fac = _Factory(delay=0.25)
    mgr = AsyncCaptureManager(popen=fac, interval_s=120, timeout_s=2, log=lambda _m: None)
    r = _route()
    assert mgr.consider(r, "A", "http://localhost:9222") is True
    assert mgr.consider(r, "A", "http://localhost:9222") is False
    assert len(fac.calls) == 1
    _wait_done(mgr)


def test_completed_helper_installs_plain_token_and_template_on_route():
    from relay.socket_capture_async import AsyncCaptureManager
    fac = _Factory(delay=0.02)
    mgr = AsyncCaptureManager(popen=fac, interval_s=120, timeout_s=2, log=lambda _m: None)
    r = _route()
    assert not r.ready("A")
    assert mgr.consider(r, "A", "http://localhost:9222")
    deadline = time.time() + 2
    while time.time() < deadline and not r.ready("A"):
        time.sleep(0.01)
    assert r.ready("A")
    assert r.template_for("A").gpt_id == "T_agent.x"
    assert r.token_life("A") > 3000
    _wait_done(mgr)


def test_manager_uses_windowless_child_and_never_puts_token_on_argv():
    from relay.socket_capture_async import AsyncCaptureManager
    fac = _Factory(delay=0.01)
    mgr = AsyncCaptureManager(popen=fac, interval_s=120, timeout_s=2, log=lambda _m: None)
    r = _route()
    mgr.consider(r, "https://agent.invalid", "http://localhost:9222")
    argv, kw = fac.calls[0]
    assert argv[-1] == "--worker"
    assert "https://agent.invalid" not in " ".join(argv)
    assert "http://localhost:9222" not in " ".join(argv)
    assert kw["env"]["MCP_CAPTURE_WORKER_AGENT"] == "https://agent.invalid"
    assert kw["env"]["MCP_CAPTURE_WORKER_CDP"] == "http://localhost:9222"
    assert kw.get("stdout") is not None and kw.get("stderr") is not None
    if __import__("os").name == "nt":
        assert kw.get("creationflags", 0) != 0
    _wait_done(mgr)


def test_admission_never_runs_sync_capture_on_the_sweep_thread():
    src = Path("relay/relay_fleet.py").read_text(encoding="utf-8")
    start = src.index("def run_relay_fleet(")
    end = src.index("# IS THE ROUTE STILL RIGHT TO BE SHUT?", start)
    admission = src[start:end]
    assert "route.refresh(context, agent_url)" not in admission
    assert "_consider_socket_refresh(route, agent_url)" in admission


def test_socket_admission_counts_as_socket_only_when_credentials_are_ready():
    src = Path("relay/relay_fleet.py").read_text(encoding="utf-8")
    i = src.index("def _socket_open_now():")
    block = src[i:i+500]
    assert "_socket_route().ready(agent_url)" in block
    assert "_socket_route().open()" not in block

def test_late_async_capture_cannot_overwrite_newer_sync_capture():
    from relay.socket_capture_async import AsyncCaptureManager
    fac = _Factory(delay=0.20)
    mgr = AsyncCaptureManager(popen=fac, interval_s=120, timeout_s=2, log=lambda _m: None)
    r = _route()
    assert mgr.consider(r, "A", "http://localhost:9222")
    # A different route user performs a newer synchronous capture while the helper is still out.
    newer = RequestTemplate({"gptId":"new"}, {"threadLevelGptId":{"id":"T_agent.new"}})
    newer_token = _token(7200)
    assert r.install_capture(newer_token, newer, "A")
    _wait_done(mgr)
    assert r.token_for("A") == newer_token
    assert r.template_for("A").gpt_id == "T_agent.new"


def test_route_closed_while_async_capture_is_inflight_stays_closed_and_unmodified():
    from relay.socket_capture_async import AsyncCaptureManager
    fac = _Factory(delay=0.12)
    mgr = AsyncCaptureManager(popen=fac, interval_s=120, timeout_s=2, log=lambda _m: None)
    r = _route()
    before = r.capture_revision("A")
    assert mgr.consider(r, "A", "http://localhost:9222")
    r.close_route("test close")
    _wait_done(mgr)
    assert not r.open()
    assert not r.ready("A")
    assert r.capture_revision("A") == before
    assert r.token_for("A") == ""


def test_revision_is_snapshotted_before_process_launch():
    from relay.socket_capture_async import AsyncCaptureManager
    r = _route()
    newer = RequestTemplate({"gptId":"new"}, {"threadLevelGptId":{"id":"T_agent.new"}})
    newer_token = _token(7200)

    class _InstallingFactory(_Factory):
        def __call__(self, argv, **kw):
            # This occurs INSIDE Popen construction. A manager that snapshots revision after
            # spawning will incorrectly treat this newer sync install as the helper's baseline.
            proc = super().__call__(argv, **kw)
            assert r.install_capture(newer_token, newer, "A")
            return proc

    fac = _InstallingFactory(delay=0.06)
    mgr = AsyncCaptureManager(popen=fac, interval_s=120, timeout_s=2, log=lambda _m: None)
    assert mgr.consider(r, "A", "http://localhost:9222")
    _wait_done(mgr)
    assert r.token_for("A") == newer_token
    assert r.template_for("A").gpt_id == "T_agent.new"
    assert mgr.stats()["discarded"] == 1


# ---- 期限の前に更新する（やること3-a）------------------------------------------------------
#
# 2026-10-26 10:34 の一斉失効は、艦隊全員が同じ page の 1 本のトークンを共有し exp がほぼ同一
# だったために起きた。だから「失効してから捕まえ直す」では全員が同時に落ちる。refresh margin
# （既定 600s）を切った時点で、まだ余命があるうちに更新を始めていなければならない。consider は
# no-turn の light capture を独立プロセスで起動するだけで、艦隊 sweep を一切ブロックしない。


def _install(route, agent, seconds):
    """余命 seconds のトークンと本物の形のテンプレートを route に入れる。"""
    tpl = RequestTemplate({"gptId": "x"}, {"threadLevelGptId": {"id": "T_agent.x"}})
    assert route.install_capture(_token(seconds), tpl, agent)


def test_a_token_inside_the_refresh_margin_is_renewed_before_it_expires():
    """余命はまだあるが margin を切ったトークンでは、失効を待たず更新を起動する。"""
    from relay.socket_capture_async import AsyncCaptureManager
    fac = _Factory(delay=0.02)
    mgr = AsyncCaptureManager(popen=fac, interval_s=120, timeout_s=2, log=lambda _m: None)
    r = _route()                                   # refresh_margin_s は既定 600
    _install(r, "A", 500)                           # margin 未満。ただし未失効（余命 500s）
    assert r.needs_refresh("A") is True
    assert r.token_life("A") > 0, "まだ失効していない＝『期限の前』であることの保証"
    assert mgr.consider(r, "A", "http://localhost:9222") is True
    assert len(fac.calls) == 1, "margin を切ったのに更新を起動していない"
    _wait_done(mgr)


def test_a_token_with_ample_life_is_not_renewed():
    """対照群。margin より十分に余命があるトークンは更新しない（無駄打ちしない）。
    『期限の前に更新』が『常に更新』に広がっていないことの保証。"""
    from relay.socket_capture_async import AsyncCaptureManager
    fac = _Factory(delay=0.02)
    mgr = AsyncCaptureManager(popen=fac, interval_s=120, timeout_s=2, log=lambda _m: None)
    r = _route()
    _install(r, "A", 1800)                          # margin(600) より十分長い
    assert r.needs_refresh("A") is False
    assert mgr.consider(r, "A", "http://localhost:9222") is False
    assert fac.calls == [], "余命が十分あるのに更新を起動している"


# ---- per-stage timing and a stage name on failure (connect / page found / token read / write) --
#
# A worker capture that times out at 180 s historically logged only 'exceeded 180s' -- no stage,
# no timing -- so an operator could not tell whether the CDP connect hung, the page was never
# found, the token read stalled, or the write failed. The worker now times each stage and names
# the in-flight one; the parent lifts that stage onto its failure message and result. These tests
# pin all four behaviours and do not open a browser.


def test_stage_timer_records_each_stage_and_names_the_inflight_one():
    from relay.socket_capture_async import _StageTimer, CAPTURE_STAGES
    clock = [0.0]
    t = _StageTimer(now=lambda: clock[0])
    t.enter("connect"); clock[0] = 0.4
    t.enter("page found"); clock[0] = 1.6
    t.enter("token read"); clock[0] = 1.9     # still in flight here
    # The in-flight stage is named, and every stage that began has a timing.
    assert t.stage == "token read"
    snap = t.snapshot()
    assert set(snap) == {"connect", "page found", "token read"}
    assert abs(snap["connect"] - 0.4) < 1e-6
    assert abs(snap["page found"] - 1.2) < 1e-6
    assert abs(snap["token read"] - 0.3) < 1e-6       # measured up to now, not closed
    # The summary string carries every stage name.
    assert "connect=" in t.summary() and "token read=" in t.summary()
    # The declared stage order is exactly the four the task names.
    assert CAPTURE_STAGES == ("connect", "page found", "token read", "write")


def test_stage_name_is_recovered_from_a_worker_stderr_tail():
    from relay.socket_capture_async import _stage_from_tail
    tail = "capture worker failed: STAGE=token read timings[connect=0.400s page found=1.200s " \
           "token read=0.300s]: TimeoutError: waiting for selector"
    assert _stage_from_tail(tail) == "token read"
    # A connect-stage failure, and the first (shortest) prefix form.
    assert _stage_from_tail("x STAGE=connect timings[connect=0.100s]: RuntimeError: no context") \
        == "connect"
    # No marker at all (worker killed before it could print) -> a named unknown, never a crash.
    assert _stage_from_tail("") == "(unknown)"
    assert _stage_from_tail("some unrelated stderr noise") == "(unknown)"


class _StagedTailProc:
    """A proc whose stderr carries a STAGE= marker, like the real worker on failure."""
    def __init__(self, *, rc=1, stage="page found"):
        self.returncode = rc
        self.pid = 7777
        self._err = ("capture worker failed: STAGE=%s timings[connect=0.400s %s=5.000s]: "
                     "RuntimeError: boom" % (stage, stage)).encode("utf-8")
    def communicate(self, timeout=None):
        return b"", self._err
    def poll(self):
        return self.returncode


def test_parent_failure_result_names_the_stage_from_a_nonzero_exit(monkeypatch):
    from relay.socket_capture_async import AsyncCaptureManager
    captured = {}

    def _fac(argv, **kw):
        return _StagedTailProc(rc=1, stage="page found")

    mgr = AsyncCaptureManager(popen=_fac, interval_s=120, timeout_s=2, log=lambda _m: None)
    r = _route()
    # Record what the route is told the failure was.
    monkeypatch.setattr(r, "note_capture_failure", lambda msg: captured.setdefault("why", msg))
    assert mgr.consider(r, "A", "http://localhost:9222")
    _wait_done(mgr)
    assert mgr.stats()["failed"] == 1
    # The stage name reached the failure log/result, not just a bare exit code.
    assert "page found" in captured["why"], captured["why"]
