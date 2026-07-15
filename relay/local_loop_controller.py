"""LOCAL_LOOP coordinator that never reads Copilot assistant response content.

The browser remains an input/control surface: send a short RUN trigger, wait for
the Stop control to disappear, handle auth/consent, and rotate heavy sessions.
The only semantic result channel is :mod:`relay.local_job_store`.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import time
from pathlib import Path

from relay.acceptance import Check, normalize_checks
from relay.local_job_store import (
    INTERACTION_WAIT_STATUSES,
    JobStoreError,
    LocalJobStore,
    TERMINAL_JOB_STATUSES,
)


PAUSED_STATUSES = frozenset({
    "WAITING_USER", "WAITING_EXTERNAL", "NEEDS_ROUTING", "WAITING_AUTH", "WAITING_CONSENT",
})


def probe_browser_interaction(driver) -> str:
    """Handle safe single-choice auth/consent UI without inspecting assistant content."""
    page = driver.page
    from relay.edge_reconnect import click_through_consent

    if click_through_consent(page):
        return "CLEAR"

    url = str(getattr(page, "url", "") or "").lower()
    auth_markers = (
        "login.microsoftonline.com", "/adfs/", "/oauth2/", "/signin", "/auth/",
    )
    if any(marker in url for marker in auth_markers):
        selectors = (
            'button:has-text("Sign in")', 'button:has-text("サインイン")',
            'button:has-text("Continue")', 'button:has-text("続行")',
            '[data-test-id="accountTile"]', '[role="button"][data-test-id*="account"]',
        )
        visible = []
        for selector in selectors:
            try:
                locator = page.locator(selector)
                for index in range(locator.count()):
                    item = locator.nth(index)
                    if item.is_visible():
                        visible.append(item)
            except Exception:
                continue
        if len(visible) == 1:
            try:
                visible[0].click()
                page.wait_for_timeout(3000)
                url = str(getattr(page, "url", "") or "").lower()
                if not any(marker in url for marker in auth_markers):
                    return "CLEAR"
            except Exception:
                pass
        return "WAITING_AUTH"

    try:
        pending = page.locator(
            'button:has-text("Allow"), button:has-text("許可"), '
            'a:has-text("connection manager")'
        )
        if pending.count() and any(pending.nth(i).is_visible() for i in range(pending.count())):
            return "WAITING_CONSENT"
    except Exception:
        pass
    return "CLEAR"


def _write_atomic(path: str | os.PathLike, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)


def run_acceptance_checks(job: dict) -> tuple[bool, str]:
    checks = normalize_checks(job.get("acceptance_checks"))
    if not checks:
        return True, "No machine acceptance checks configured."
    constraints = job.get("constraints") if isinstance(job.get("constraints"), dict) else {}
    cwd = job.get("workspace") or constraints.get("allowed_base") or None
    details = []
    for spec in checks:
        check = Check(spec, cwd=cwd).start()
        result = check.poll()
        while result is None:
            time.sleep(0.2)
            result = check.poll()
        passed, detail = result
        details.append(f"{check.describe()}: {'PASS' if passed else 'FAIL'}\n{detail}")
        if not passed:
            return False, "\n\n".join(details)
    return True, "\n\n".join(details)


def collect_browser_metrics(page, edge_mb_fn=None) -> dict:
    """Collect load signals without reading assistant text or conversation DOM content."""
    metrics = {}
    session = None
    try:
        session = page.context.new_cdp_session(page)
        heap = session.send("Runtime.getHeapUsage")
        dom = session.send("Memory.getDOMCounters")
        metrics.update({
            "js_heap_mb": round(float(heap.get("usedSize", 0)) / (1024 * 1024), 2),
            "js_heap_total_mb": round(float(heap.get("totalSize", 0)) / (1024 * 1024), 2),
            "dom_nodes": int(dom.get("nodes", 0)),
            "dom_documents": int(dom.get("documents", 0)),
            "dom_listeners": int(dom.get("jsEventListeners", 0)),
        })
    except Exception as exc:
        metrics["cdp_metric_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            if session is not None:
                session.detach()
        except Exception:
            pass
    if edge_mb_fn is not None:
        try:
            metrics["edge_mb"] = round(float(edge_mb_fn()), 2)
        except Exception as exc:
            metrics["edge_metric_error"] = f"{type(exc).__name__}: {exc}"
    return metrics


class LocalLoopController:
    def __init__(self, store: LocalJobStore, job_id: str, driver,
                 status_path: str | os.PathLike | None = None,
                 commands_path: str | os.PathLike | None = None,
                 poll_seconds: float = 1.0, turn_timeout_seconds: float = 1800,
                 ui_idle_timeout_seconds: float = 300,
                 rotate_after_turns: int = 5,
                 js_heap_limit_mb: float = 0,
                 dom_node_limit: int = 0,
                 edge_mb_limit: float = 0,
                 rotate_driver=None, consent_probe=None, metrics_probe=None,
                 acceptance_runner=run_acceptance_checks,
                 sleep_fn=time.sleep, monotonic_fn=time.monotonic):
        self.store = store
        self.job_id = job_id
        self.driver = driver
        self.status_path = Path(status_path) if status_path else None
        self.commands_path = Path(commands_path) if commands_path else None
        self.poll_seconds = max(0.01, float(poll_seconds))
        self.turn_timeout_seconds = max(1.0, float(turn_timeout_seconds))
        self.ui_idle_timeout_seconds = max(1.0, float(ui_idle_timeout_seconds))
        self.rotate_after_turns = max(0, int(rotate_after_turns))
        self.js_heap_limit_mb = max(0.0, float(js_heap_limit_mb))
        self.dom_node_limit = max(0, int(dom_node_limit))
        self.edge_mb_limit = max(0.0, float(edge_mb_limit))
        self.rotate_driver = rotate_driver
        self.consent_probe = consent_probe
        self.metrics_probe = metrics_probe or (lambda drv: {})
        self.acceptance_runner = acceptance_runner
        self.sleep = sleep_fn
        self.monotonic = monotonic_fn
        self.turns_in_conversation = 0
        self.worker_id = self._new_worker_id()
        self.rotation_count = 0
        self._answer_reads_at_attach = int(getattr(driver, "answer_content_reads", 0))

    @staticmethod
    def _new_worker_id() -> str:
        return f"local_{os.getpid()}_{secrets.token_hex(6)}"

    def _assert_no_answer_content_read(self):
        reads = int(getattr(self.driver, "answer_content_reads", 0))
        if reads != self._answer_reads_at_attach:
            raise RuntimeError("LOCAL_LOOP invariant violated: assistant response content was read")

    def _project(self):
        if not self.status_path:
            return
        snapshot = self.store.console_snapshot()
        status = self.store.get_job_status(self.job_id)["status"]
        snapshot["open_tabs"] = 0 if status in TERMINAL_JOB_STATUSES else 1
        snapshot["local_loop_answer_content_reads"] = (
            int(getattr(self.driver, "answer_content_reads", 0)) - self._answer_reads_at_attach
        )
        _write_atomic(self.status_path, snapshot)

    def _drain_commands(self) -> bool:
        if not self.commands_path or not self.commands_path.is_file():
            return False
        try:
            command = json.loads(self.commands_path.read_text(encoding="utf-8-sig"))
        finally:
            try:
                self.commands_path.unlink()
            except OSError:
                pass
        stop = bool(command.get("stop")) or self.job_id in command.get("close", [])
        if stop:
            self.store.cancel_job(self.job_id, "operator stop from console")
            self._project()
        return stop

    def _probe_consent(self):
        if self.consent_probe is None:
            return None
        try:
            result = self.consent_probe(self.driver)
            if result in INTERACTION_WAIT_STATUSES:
                self.store.mark_waiting_interaction(
                    self.job_id, result, "browser interaction requires operator attention",
                )
            elif result not in (None, False, "CLEAR"):
                self.store.record_event(self.job_id, "CONSENT_HANDLED")
            return result
        except Exception as exc:
            self.store.record_event(
                self.job_id, "CONSENT_PROBE_ERROR", {"error": f"{type(exc).__name__}: {exc}"},
            )
            return None

    def _wait_for_commit(self, seq: int) -> dict | None:
        deadline = self.monotonic() + self.turn_timeout_seconds
        next_consent_probe = self.monotonic()
        while self.monotonic() < deadline:
            if self._drain_commands():
                return None
            commit = self.store.get_turn_commit(self.job_id, seq)
            if commit is not None:
                return commit
            if self.monotonic() >= next_consent_probe:
                self._probe_consent()
                if self.store.get_job_status(self.job_id)["status"] in INTERACTION_WAIT_STATUSES:
                    return None
                next_consent_probe = self.monotonic() + 5.0
            self._project()
            self.sleep(self.poll_seconds)
        self.store.record_event(self.job_id, "TURN_COMMIT_TIMEOUT", {"timeout_s": self.turn_timeout_seconds}, seq)
        return None

    def _wait_ui_idle(self) -> tuple[bool, float]:
        started = self.monotonic()
        try:
            generating = bool(self.driver._is_generating())
            idle = (not generating) or bool(
                self.driver._wait_generation_idle(timeout_s=self.ui_idle_timeout_seconds)
            )
            if idle and hasattr(self.driver, "_page_alive"):
                idle = bool(self.driver._page_alive())
        except Exception:
            idle = False
        return idle, max(0.0, self.monotonic() - started)

    def _must_rotate(self, metrics: dict, ui_idle_latency_s: float) -> tuple[bool, str]:
        if self.rotate_after_turns and self.turns_in_conversation >= self.rotate_after_turns:
            return True, "turn threshold"
        if self.js_heap_limit_mb and float(metrics.get("js_heap_mb", 0)) >= self.js_heap_limit_mb:
            return True, "JS heap threshold"
        if self.dom_node_limit and int(metrics.get("dom_nodes", 0)) >= self.dom_node_limit:
            return True, "DOM node threshold"
        if self.edge_mb_limit and float(metrics.get("edge_mb", 0)) >= self.edge_mb_limit:
            return True, "Edge memory threshold"
        if ui_idle_latency_s >= self.ui_idle_timeout_seconds:
            return True, "UI idle timeout"
        return False, ""

    def _rotate(self, reason: str) -> bool:
        if self.rotate_driver is None:
            return False
        old_reads = int(getattr(self.driver, "answer_content_reads", 0))
        self._assert_no_answer_content_read()
        replacement = self.rotate_driver(self.driver, reason)
        if replacement is None:
            return False
        self.driver = replacement
        self._answer_reads_at_attach = int(getattr(replacement, "answer_content_reads", 0))
        self.turns_in_conversation = 0
        self.worker_id = self._new_worker_id()
        self.rotation_count += 1
        self.store.record_event(self.job_id, "CONVERSATION_ROTATED", {
            "reason": reason, "rotation_count": self.rotation_count,
            "prior_answer_content_reads": old_reads,
        })
        return True

    def _verify(self) -> dict:
        job = self.store.get_job(self.job_id)
        passed, detail = self.acceptance_runner(job)
        return self.store.verify_candidate(self.job_id, passed, detail)

    def run(self) -> str:
        sent_turns = 0
        while True:
            self._assert_no_answer_content_read()
            status = self.store.get_job_status(self.job_id)
            if status["status"] == "WAITING_RUNTIME":
                self.store.resume_runtime(self.job_id)
                status = self.store.get_job_status(self.job_id)
            if status["status"] in INTERACTION_WAIT_STATUSES:
                interaction = self._probe_consent()
                if interaction == "CLEAR":
                    self.store.resume_interaction(self.job_id)
                    continue
                self._project()
                return status["status"]
            if status["status"] in TERMINAL_JOB_STATUSES:
                self._project()
                self.store.checkpoint()
                return status["status"]
            if status["status"] in PAUSED_STATUSES:
                self._project()
                return status["status"]
            if status["status"] == "VERIFYING":
                self._verify()
                self._project()
                continue
            if self._drain_commands():
                return "CANCELLED"

            job = self.store.get_job(self.job_id)
            constraints = job.get("constraints") if isinstance(job.get("constraints"), dict) else {}
            max_turns = int(constraints.get("max_turns", 1000))
            if sent_turns >= max_turns or int(status["current_seq"]) > max_turns:
                self.store.cancel_job(self.job_id, f"max_turns={max_turns} reached")
                self._project()
                return "CANCELLED"

            seq = int(status["current_seq"])
            trigger = f"RUN {self.job_id} seq={seq} worker={self.worker_id}"
            self.driver.send(trigger, track_answer=False)
            self.store.record_event(self.job_id, "UI_TRIGGER_SENT", {
                "seq": seq, "worker_id": self.worker_id,
            }, seq)
            sent_turns += 1
            self.turns_in_conversation += 1

            commit = self._wait_for_commit(seq)
            if commit is None:
                current_status = self.store.get_job_status(self.job_id)["status"]
                if current_status == "CANCELLED":
                    return "CANCELLED"
                if current_status in PAUSED_STATUSES:
                    self._project()
                    return current_status
                if not self._rotate("commit timeout"):
                    self.store.mark_waiting_runtime(self.job_id, "commit timeout; no replacement conversation")
                    self._project()
                    return "WAITING_RUNTIME"
                continue

            idle, idle_latency = self._wait_ui_idle()
            metrics = dict(self.metrics_probe(self.driver) or {})
            metrics["ui_idle_latency_s"] = round(idle_latency, 3)
            metrics["answer_content_reads"] = (
                int(getattr(self.driver, "answer_content_reads", 0)) - self._answer_reads_at_attach
            )
            self.store.record_event(self.job_id, "BROWSER_METRICS", metrics, seq)
            self._assert_no_answer_content_read()

            rotated_for_idle = False
            if not idle:
                if not self._rotate("commit received but UI did not become idle"):
                    self.store.mark_waiting_runtime(
                        self.job_id, "commit received but UI did not become idle",
                    )
                    self._project()
                    return "WAITING_RUNTIME"
                rotated_for_idle = True

            if commit["status"] == "CANDIDATE_DONE":
                self._verify()

            status = self.store.get_job_status(self.job_id)
            if status["status"] in TERMINAL_JOB_STATUSES | PAUSED_STATUSES:
                self._project()
                if status["status"] in TERMINAL_JOB_STATUSES:
                    self.store.checkpoint()
                return status["status"]

            rotate, reason = self._must_rotate(metrics, idle_latency)
            if rotate and not rotated_for_idle:
                self._rotate(reason)
            self._project()


def _open_driver(context, agent_url):
    from relay.copilot_autopilot_relay import CopilotWebDriver
    from relay.edge_reconnect import click_through_consent
    from relay.relay_fleet import _open_fresh

    page = _open_fresh(context, agent_url)
    try:
        click_through_consent(page)
    except Exception:
        pass
    return CopilotWebDriver(page)


def main(argv=None):
    from dotenv import load_dotenv

    load_dotenv()
    ap = argparse.ArgumentParser(description="Response-content-independent M365 LOCAL_LOOP")
    ap.add_argument("--job-id")
    ap.add_argument("--job-file", help="create/resume a LOCAL_LOOP job from this JSON file")
    ap.add_argument("--db", default=os.environ.get("MCP_LOCAL_JOB_DB"))
    ap.add_argument("--cdp-url", default=os.environ.get("MCP_CDP_URL", "http://localhost:9222"))
    ap.add_argument("--agent-url", default=os.environ.get("MCP_FLEET_AGENT_URL") or
                    os.environ.get("MCP_IMPL_AGENT_URL"))
    ap.add_argument("--state-dir", default=".fleet")
    ap.add_argument("--poll-seconds", type=float, default=1.0)
    ap.add_argument("--turn-timeout", type=float, default=1800)
    ap.add_argument("--ui-idle-timeout", type=float, default=300)
    ap.add_argument("--rotate-after-turns", type=int, default=5)
    ap.add_argument("--js-heap-limit-mb", type=float, default=0)
    ap.add_argument("--dom-node-limit", type=int, default=0)
    ap.add_argument("--edge-mb-limit", type=float, default=0)
    args = ap.parse_args(argv)
    if not args.agent_url:
        ap.error("--agent-url or MCP_FLEET_AGENT_URL/MCP_IMPL_AGENT_URL is required")

    store = LocalJobStore(args.db)
    job_id = args.job_id
    if args.job_file:
        job = json.loads(Path(args.job_file).read_text(encoding="utf-8"))
        job_id = str(job.get("job_id") or job_id or "")
        try:
            store.create_job(job)
        except JobStoreError as exc:
            if exc.code != "JOB_EXISTS":
                raise
    if not job_id:
        ap.error("--job-id or --job-file is required")

    from playwright.sync_api import sync_playwright
    from relay.edge_recover import companion_edge_mb

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(args.cdp_url, timeout=20000)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        driver = _open_driver(context, args.agent_url)

        def rotate(old, reason):
            old_page = getattr(old, "page", None)
            replacement = _open_driver(context, args.agent_url)
            try:
                if old_page is not None and not old_page.is_closed():
                    old_page.close()
            except Exception:
                pass
            return replacement

        controller = LocalLoopController(
            store, job_id, driver,
            status_path=Path(args.state_dir) / "status.json",
            commands_path=Path(args.state_dir) / "commands.json",
            poll_seconds=args.poll_seconds,
            turn_timeout_seconds=args.turn_timeout,
            ui_idle_timeout_seconds=args.ui_idle_timeout,
            rotate_after_turns=args.rotate_after_turns,
            js_heap_limit_mb=args.js_heap_limit_mb,
            dom_node_limit=args.dom_node_limit,
            edge_mb_limit=args.edge_mb_limit,
            rotate_driver=rotate,
            consent_probe=probe_browser_interaction,
            metrics_probe=lambda drv: collect_browser_metrics(drv.page, companion_edge_mb),
        )
        result = controller.run()
        print(f"LOCAL_LOOP {job_id}: {result}")
        return 0 if result == "DONE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
