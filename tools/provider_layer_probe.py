"""Layered reachability probe for the model provider hosts.

For each host, one round measures the four layers in order:
dns -> tcp443 -> tls handshake -> http first byte. The first failing layer and the
exception class name are recorded. The probe sends no credentials and no Copilot turn;
it only opens a socket, completes a TLS handshake and sends a bare HEAD request.

Verdict (classify):
  control_down         the neutral control host fails: our network is the problem
  provider_unreachable the control host works but a provider host fails at dns or tcp
  provider_unresponsive a provider host fails at tls or http while tcp works
  ok                   every host answers

Exit codes: ok=0, control_down=2, provider_unreachable=3, provider_unresponsive=4,
no rows=5.

Usage:
  python -I -X utf8 tools/provider_layer_probe.py [--rounds N] [--interval S] [--json]
  --provider-host H     add a provider host (repeatable; default: host of relay/chathub.py WS_BASE)
  --agent-host H        add the Copilot agent page host (default: host of MCP_CAPTURE_WORKER_AGENT)

Each run appends one JSON line per round to .fleet/provider_health.jsonl (append only).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
HEALTH_LOG = ROOT / ".fleet" / "provider_health.jsonl"
CHATHUB = ROOT / "relay" / "chathub.py"
SIGNIN_HOST = "login.microsoftonline.com"
CONTROL_HOST = "www.iana.org"
TIMEOUT_S = 6.0

EXIT_OK = 0
EXIT_CONTROL_DOWN = 2
EXIT_UNREACHABLE = 3
EXIT_UNRESPONSIVE = 4
EXIT_NO_ROWS = 5
EXIT_CODES = {
    "ok": EXIT_OK,
    "control_down": EXIT_CONTROL_DOWN,
    "provider_unreachable": EXIT_UNREACHABLE,
    "provider_unresponsive": EXIT_UNRESPONSIVE,
    "no_rows": EXIT_NO_ROWS,
}


def probe_host(host: str, role: str, timeout: float = TIMEOUT_S) -> dict:
    """Measure the four layers for one host. Returns one row; fail_layer is None when all pass."""
    row = {"host": host, "role": role, "dns_ms": None, "tcp_ms": None, "tls_ms": None,
           "http_first_byte_ms": None, "http_status": None, "fail_layer": None, "error": None}
    t0 = time.monotonic()
    try:
        ip = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except Exception as exc:  # noqa: BLE001
        row.update(fail_layer="dns", error=type(exc).__name__)
        return row
    row["dns_ms"] = round((time.monotonic() - t0) * 1000)

    t0 = time.monotonic()
    try:
        sock = socket.create_connection((ip, 443), timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        row.update(fail_layer="tcp", error=type(exc).__name__)
        return row
    row["tcp_ms"] = round((time.monotonic() - t0) * 1000)

    t0 = time.monotonic()
    try:
        tls_sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    except Exception as exc:  # noqa: BLE001
        sock.close()
        row.update(fail_layer="tls", error=type(exc).__name__)
        return row
    row["tls_ms"] = round((time.monotonic() - t0) * 1000)

    t0 = time.monotonic()
    try:
        tls_sock.settimeout(timeout)
        request = "HEAD / HTTP/1.1\r\nHost: %s\r\nConnection: close\r\nUser-Agent: layer-probe\r\n\r\n" % host
        tls_sock.sendall(request.encode("ascii"))
        data = tls_sock.recv(64)
        row["http_first_byte_ms"] = round((time.monotonic() - t0) * 1000)
        parts = data.split(b" ")
        row["http_status"] = parts[1].decode("ascii", "replace") if len(parts) > 1 else None
    except Exception as exc:  # noqa: BLE001
        row.update(fail_layer="http", error=type(exc).__name__)
    finally:
        tls_sock.close()
    return row


def classify(rows: list[dict]) -> str:
    """Turn one round of rows into a verdict. Rows carry role (control / provider / agent) and fail_layer."""
    if not rows:
        return "no_rows"
    controls = [r for r in rows if r.get("role") == "control"]
    if any(r.get("fail_layer") for r in controls):
        return "control_down"
    providers = [r for r in rows if r.get("role") in ("provider", "agent", "signin")]
    if any(r.get("fail_layer") in ("dns", "tcp") for r in providers):
        return "provider_unreachable"
    if any(r.get("fail_layer") in ("tls", "http") for r in providers):
        return "provider_unresponsive"
    return "ok"


def default_provider_host() -> str:
    """Host of WS_BASE in relay/chathub.py, read as text so that importing the relay is never needed."""
    try:
        text = CHATHUB.read_text(encoding="utf-8")
    except OSError:
        return "substrate.office.com"
    match = re.search(r'^WS_BASE\s*=\s*"([^"]+)"', text, re.M)
    if not match:
        return "substrate.office.com"
    return urlsplit(match.group(1)).hostname or "substrate.office.com"


def agent_host_from_env() -> str | None:
    url = os.environ.get("MCP_CAPTURE_WORKER_AGENT") or ""
    return urlsplit(url).hostname if url else None


def run_round(hosts: list[tuple[str, str]]) -> list[dict]:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for host, role in hosts:
        row = probe_host(host, role)
        row["ts"] = stamp
        rows.append(row)
    return rows


def append_evidence(rows: list[dict], verdict: str, path: Path = HEALTH_LOG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": rows[0]["ts"] if rows else None, "kind": "layer_probe", "verdict": verdict, "rows": rows}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--interval", type=float, default=0.0)
    parser.add_argument("--provider-host", action="append", default=[])
    parser.add_argument("--agent-host", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    hosts: list[tuple[str, str]] = [(CONTROL_HOST, "control"), (SIGNIN_HOST, "signin")]
    provider = args.provider_host or [default_provider_host()]
    hosts += [(h, "provider") for h in provider]
    agent = args.agent_host or agent_host_from_env()
    if agent:
        hosts.append((agent, "agent"))

    last_verdict, last_rows = "no_rows", []
    for i in range(max(1, args.rounds)):
        rows = run_round(hosts)
        verdict = classify(rows)
        append_evidence(rows, verdict)
        last_verdict, last_rows = verdict, rows
        if args.json:
            print(json.dumps({"verdict": verdict, "rows": rows}, ensure_ascii=False))
        else:
            print("round %d verdict=%s" % (i + 1, verdict))
            for r in rows:
                print("  %-9s %-40s fail=%-5s err=%s dns=%s tcp=%s tls=%s http=%s" % (
                    r["role"], r["host"], r["fail_layer"] or "-", r["error"] or "-",
                    r["dns_ms"], r["tcp_ms"], r["tls_ms"], r["http_first_byte_ms"]))
        if args.interval and i + 1 < args.rounds:
            time.sleep(args.interval)
    return EXIT_CODES.get(last_verdict, EXIT_NO_ROWS)


if __name__ == "__main__":
    sys.exit(main())
