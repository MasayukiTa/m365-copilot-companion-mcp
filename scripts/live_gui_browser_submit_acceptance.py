#!/usr/bin/env python3
"""Live acceptance for the real Cockpit -> Fleet -> M365 Copilot browser submission path.

This is intentionally NOT a hermetic CI test. It proves the environment-level part of
R5-NOTE-1 that source/unit tests cannot: a fresh M365 conversation receives one non-empty user
turn, exactly once, through the real Playwright/CDP DOM path.

Requirements:
  * Windows, FleetCockpit running.
  * Companion Edge on CDP 9222 and already authenticated.
  * Fleet idle and fanout=off (fresh one-worker conversation is part of the assertion).

The submitted task is read-only and asks the agent to echo a unique marker + DONE. No local
file/tool mutation is requested by this acceptance run.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def judge_question_texts(question_texts: list[str], marker: str) -> dict:
    """Pure verdict for a *fresh* Copilot conversation's visible user-turn DOM."""
    texts = [str(t or "") for t in question_texts]
    marker_hits = [t for t in texts if marker in t]
    empty = [t for t in texts if not t.strip()]
    reasons: list[str] = []
    if len(texts) != 1:
        reasons.append(f"fresh conversation has {len(texts)} user questions, expected exactly 1")
    if len(marker_hits) != 1:
        reasons.append(f"marker appears in {len(marker_hits)} user-question containers, expected 1")
    if empty:
        reasons.append(f"fresh conversation contains {len(empty)} empty user question(s)")
    return {
        "ok": not reasons,
        "question_count": len(texts),
        "marker_question_count": len(marker_hits),
        "empty_question_count": len(empty),
        "marker_question_lengths": [len(t) for t in marker_hits],
        "marker_occurrences": sum(t.count(marker) for t in marker_hits),
        "reasons": reasons,
    }


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _fresh_idle_reason(state_dir: Path) -> str:
    """Return why this state dir is not safe for a *fresh-conversation* acceptance run.

    ``status.running`` alone has a launch gap: task_router can already own work in for_fleet/
    while the new coordinator has not published status yet.  R5 needs a fresh chat, so fail
    closed on every durable in-flight signal we own.
    """
    p = state_dir / "status.json"
    if p.is_file():
        try:
            if bool(_read_json(p).get("running")):
                return "status.json says a Fleet run is active"
        except Exception:
            return "status.json cannot be read safely"
    if (state_dir / "fleet_run_active.json").exists():
        return "fleet_run_active.json still has an owner"
    pending_dir = state_dir / "tasks" / "for_fleet"
    if pending_dir.is_dir() and any(x.is_file() for x in pending_dir.iterdir()):
        return "tasks/for_fleet still contains pending work"
    commands_dir = state_dir / "commands.d"
    if commands_dir.is_dir() and any(x.is_file() and x.suffix.lower() == ".json" for x in commands_dir.iterdir()):
        return "commands.d still contains a pending live command"
    return ""


def _require_stable_fresh_idle(state_dir: Path, quiet_s: float = 2.0) -> None:
    reason = _fresh_idle_reason(state_dir)
    if reason:
        raise RuntimeError(reason)
    # Close the router-launch gap: a state that is merely between durable handoff and coordinator
    # publication is not fresh idle.  Re-check after a short quiet window.
    time.sleep(max(0.0, quiet_s))
    reason = _fresh_idle_reason(state_dir)
    if reason:
        raise RuntimeError("state stopped being idle during the quiet window: " + reason)


def _fanout_setting() -> str:
    from tools.settings_path import settings_file
    p = Path(settings_file())
    if not p.is_file():
        return ""
    for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if line.lower().startswith("fanout="):
            return line.split("=", 1)[1].strip().lower()
    return ""


def _worker_for_marker(status: dict, marker: str) -> dict | None:
    hits = [w for w in status.get("workers", []) if marker in str(w.get("goal") or "")]
    if len(hits) > 1:
        raise RuntimeError(f"marker matched {len(hits)} workers; expected one")
    return hits[0] if hits else None


def _wait_worker_conversation(state_dir: Path, marker: str, timeout_s: float) -> dict:
    deadline = time.time() + timeout_s
    last: dict | None = None
    while time.time() < deadline:
        try:
            st = _read_json(state_dir / "status.json")
            w = _worker_for_marker(st, marker)
            if w:
                last = dict(w)
                if w.get("conv_url"):
                    return dict(w)
                if str(w.get("status") or "").lower() in {"stuck", "error", "cancelled"}:
                    raise RuntimeError(f"worker terminated before conversation URL: {w.get('status')} {w.get('reason','')}")
        except FileNotFoundError:
            pass
        time.sleep(0.10)
    raise TimeoutError(f"worker conversation URL did not appear within {timeout_s:.0f}s; last={last}")


def _wait_page(contexts, conv_url: str, timeout_s: float):
    needle = conv_url.rstrip("/").split("/")[-1]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for ctx in contexts:
            for page in ctx.pages:
                try:
                    if page.url == conv_url or needle in page.url:
                        return page
                except Exception:
                    continue
        time.sleep(0.10)
    raise TimeoutError(f"M365 page for conversation {needle} did not appear within {timeout_s:.0f}s")


def _wait_visible_questions(page, marker: str, timeout_s: float) -> tuple[list[str], dict]:
    deadline = time.time() + timeout_s
    last: list[str] = []
    while time.time() < deadline:
        try:
            loc = page.locator('[data-testid="chatQuestion"]')
            last = [loc.nth(i).inner_text() for i in range(loc.count())]
            if any(marker in t for t in last):
                composer = page.locator('#m365-chat-editor-target-element').first
                composer_text = composer.inner_text() if composer.count() else ""
                return last, {
                    "assistant_count": page.locator('.fai-CopilotMessage').count(),
                    "composer_len": len(composer_text or ""),
                    "conversation_url": page.url,
                }
        except Exception:
            pass
        time.sleep(0.10)
    raise TimeoutError(f"marker did not appear in visible chatQuestion DOM within {timeout_s:.0f}s; questions={len(last)}")


def _wait_terminal(state_dir: Path, marker: str, timeout_s: float) -> dict | None:
    if timeout_s <= 0:
        return None
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            st = _read_json(state_dir / "status.json")
            w = _worker_for_marker(st, marker)
            if w:
                last = dict(w)
                if str(w.get("status") or "").lower() in {"done", "stuck", "error", "cancelled"}:
                    return dict(w)
        except Exception:
            pass
        time.sleep(0.25)
    return last


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Live R5 GUI/browser submission acceptance")
    ap.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    ap.add_argument("--marker", default="")
    ap.add_argument("--submit-timeout", type=int, default=45)
    ap.add_argument("--browser-timeout", type=int, default=45)
    ap.add_argument("--terminal-timeout", type=int, default=90)
    args = ap.parse_args(argv)

    if os.name != "nt":
        print("REFUSED: this live acceptance requires Windows UI Automation", file=sys.stderr)
        return 2
    state_dir = REPO / ".fleet"
    try:
        _require_stable_fresh_idle(state_dir)
    except RuntimeError as exc:
        print("REFUSED: R5 acceptance requires stable fresh idle: %s" % exc, file=sys.stderr)
        return 2
    fanout = _fanout_setting()
    if fanout not in {"off", "0", "false"}:
        print(f"REFUSED: fanout must be off for a one-worker fresh-conversation proof (current={fanout!r})", file=sys.stderr)
        return 2

    marker = args.marker.strip() or ("R5LIVE-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:22])
    goal = (
        f"{marker} READ-ONLY browser submission acceptance. Do not call local tools and do not modify anything. "
        f"Include {marker} in your reply, and put DONE on the final line."
    )
    submitter = REPO / "scripts" / "win" / "submit_via_ui.ps1"
    cmd = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(submitter),
        "-Goal", goal, "-TimeoutSeconds", str(args.submit_timeout),
    ]
    started = time.time()
    proc = subprocess.run(cmd, cwd=str(REPO), text=True, capture_output=True,
                          timeout=max(args.submit_timeout + 30, 75))
    submit_text = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        print(json.dumps({"ok": False, "marker": marker, "phase": "cockpit_submit",
                          "returncode": proc.returncode, "output_tail": submit_text[-2000:]},
                         ensure_ascii=False, indent=2))
        return 1
    required = ["goal box: found by AutomationId", "start button: found by AutomationId", "submitted:"]
    missing = [x for x in required if x not in submit_text]
    if missing:
        print(json.dumps({"ok": False, "marker": marker, "phase": "cockpit_receipt",
                          "missing": missing, "output_tail": submit_text[-2000:]},
                         ensure_ascii=False, indent=2))
        return 1

    worker = _wait_worker_conversation(state_dir, marker, args.browser_timeout)
    conv_url = str(worker.get("conv_url") or "")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(args.cdp_url)
        page = _wait_page(browser.contexts, conv_url, args.browser_timeout)
        questions, dom_meta = _wait_visible_questions(page, marker, args.browser_timeout)
        verdict = judge_question_texts(questions, marker)

    terminal = _wait_terminal(state_dir, marker, args.terminal_timeout)
    result = {
        **verdict,
        "marker": marker,
        "worker": worker.get("name"),
        "worker_turn_at_dom_probe": worker.get("turn"),
        "worker_status_at_dom_probe": worker.get("status"),
        "goal_hash": worker.get("goal_hash"),
        "conversation_url_has_id": "/conversation/" in conv_url.lower(),
        "assistant_count_at_probe": dom_meta["assistant_count"],
        "composer_len_at_probe": dom_meta["composer_len"],
        "submit_elapsed_s": round(time.time() - started, 3),
        "terminal_status": (terminal or {}).get("status"),
        "terminal_outcome": (terminal or {}).get("outcome"),
        "terminal_turn": (terminal or {}).get("turn"),
    }
    if not result["conversation_url_has_id"]:
        result["ok"] = False
        result["reasons"].append("worker conversation URL never became a /conversation/ URL")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
