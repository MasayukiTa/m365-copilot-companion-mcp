"""Page-level probe for the Copilot agent page, through the dedicated capture browser over CDP.

Opens ONE new tab on the agent page, sends nothing (no typing, no turn), watches for 30 seconds
and then closes only the tab it opened. It records the final host, whether the title is
non-empty, request counts per host, and the host and path (never the query string) of each
request still pending at the end.

Verdict (page_verdict):
  cdp_unreachable  the browser could not be reached over CDP
  signed_out       the final page is on a sign-in host (redirect)
  page_complete    the title is non-empty and no request is pending
  page_stalled     anything else: requests still pending, or no title

Exit codes: page_complete=0, signed_out=2, page_stalled=3, cdp_unreachable=4.

Usage:
  python -I -X utf8 tools/provider_page_probe.py [--cdp http://localhost:9222] [--agent-url URL] [--json]
The agent URL defaults to the MCP_CAPTURE_WORKER_AGENT environment variable.
Each run appends one JSON line to .fleet/provider_health.jsonl (append only).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
HEALTH_LOG = ROOT / ".fleet" / "provider_health.jsonl"
DEFAULT_CDP = "http://localhost:9222"
SIGNIN_HOSTS = frozenset({"login.microsoftonline.com", "login.live.com"})
WATCH_SECONDS = 30

EXIT_CODES = {"page_complete": 0, "signed_out": 2, "page_stalled": 3, "cdp_unreachable": 4}


def page_verdict(cdp_ok: bool, final_host: str, title: str, pending_hosts: list[str],
                 signin_hosts: frozenset[str] = SIGNIN_HOSTS) -> str:
    """Pure verdict logic. Takes plain data so it can be tested without a browser.

    Order matters: an unreachable browser says nothing about the page; a redirect to the
    sign-in host means signed out whatever else happened; a complete page has a title and no
    pending requests; every other outcome is reported as stalled.
    """
    if not cdp_ok:
        return "cdp_unreachable"
    if final_host in signin_hosts:
        return "signed_out"
    if title.strip() and not pending_hosts:
        return "page_complete"
    return "page_stalled"


def pending_summary(pending: dict[str, tuple[str, str]]) -> list[str]:
    """Hosts of the requests still pending. Only host and path are kept."""
    return sorted({host for host, _path in pending.values()})


def strip_url(url: str) -> tuple[str, str]:
    """Return (host, path) with the query string and fragment removed."""
    parts = urlsplit(url)
    return parts.netloc, parts.path


def watch_page(cdp_url: str, agent_url: str, seconds: int = WATCH_SECONDS) -> dict:
    """Open one tab, watch it, close it. Requires Playwright; imported here so the module loads without it."""
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    started: dict[str, int] = {}
    finished: dict[str, int] = {}
    failed: dict[str, int] = {}
    pending: dict[int, tuple[str, str]] = {}
    result = {"cdp_ok": False, "final_host": "", "title": "", "requests": {}, "pending_hosts": [],
              "pending": [], "error": None}

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(cdp_url, timeout=20000)
        except Exception as exc:  # noqa: BLE001
            result["error"] = type(exc).__name__
            return result
        result["cdp_ok"] = True
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()  # the one tab this probe owns
        try:
            def on_request(req):
                host, path = strip_url(req.url)
                started[host] = started.get(host, 0) + 1
                pending[id(req)] = (host, path)

            def on_done(req):
                host, _ = strip_url(req.url)
                finished[host] = finished.get(host, 0) + 1
                pending.pop(id(req), None)

            def on_failed(req):
                host, _ = strip_url(req.url)
                failed[host] = failed.get(host, 0) + 1
                pending.pop(id(req), None)

            page.on("request", on_request)
            page.on("requestfinished", on_done)
            page.on("requestfailed", on_failed)
            try:
                page.goto(agent_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:  # noqa: BLE001
                result["error"] = "goto:" + type(exc).__name__
            page.wait_for_timeout(seconds * 1000)
            result["final_host"] = urlsplit(page.url).netloc
            try:
                result["title"] = page.title()
            except Exception:  # noqa: BLE001
                result["title"] = ""
        finally:
            page.close()  # closes only the tab this probe opened

    hosts = set(started) | set(failed)
    result["requests"] = {h: {"started": started.get(h, 0), "finished": finished.get(h, 0),
                              "failed": failed.get(h, 0)} for h in sorted(hosts)}
    result["pending"] = sorted(set(pending.values()))
    result["pending_hosts"] = pending_summary({i: p for i, p in enumerate(result["pending"])})
    return result


def append_evidence(record: dict, path: Path = HEALTH_LOG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--cdp", default=DEFAULT_CDP)
    parser.add_argument("--agent-url", default=os.environ.get("MCP_CAPTURE_WORKER_AGENT", ""))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not args.agent_url:
        print("no agent URL: pass --agent-url or set MCP_CAPTURE_WORKER_AGENT", file=sys.stderr)
        return EXIT_CODES["page_stalled"]

    t0 = time.monotonic()
    try:
        data = watch_page(args.cdp, args.agent_url)
    except Exception as exc:  # noqa: BLE001
        data = {"cdp_ok": False, "final_host": "", "title": "", "requests": {}, "pending": [],
                "pending_hosts": [], "error": type(exc).__name__}
    verdict = page_verdict(data["cdp_ok"], data["final_host"], data["title"], data["pending_hosts"])
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": "page_probe",
              "verdict": verdict, "seconds": round(time.monotonic() - t0, 1),
              "final_host": data["final_host"], "title_present": bool(data["title"].strip()),
              "requests": data["requests"], "pending": data["pending"], "error": data["error"]}
    append_evidence(record)
    if args.json:
        print(json.dumps(record, ensure_ascii=False))
    else:
        print("verdict=%s final_host=%s title_present=%s pending_hosts=%s error=%s" % (
            verdict, record["final_host"] or "-", record["title_present"],
            ",".join(data["pending_hosts"]) or "-", record["error"] or "-"))
    return EXIT_CODES[verdict]


if __name__ == "__main__":
    sys.exit(main())
