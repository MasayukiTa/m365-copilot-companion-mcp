"""Key-injecting localhost proxy for the Minecraft-bot "brain" path.

Chain (the eval host bot has NO key on its side -- exfiltration guard forbids sending
MCP_API_KEY to the eval host, so the key stays HERE and this proxy injects it):

    the eval host bot -> the eval host 127.0.0.1:8012 --[ssh -R reverse tunnel]-->
    <user> 127.0.0.1:8012 (THIS proxy, adds Authorization: Bearer <MCP_API_KEY>)
    -> 127.0.0.1:8011 relay.openai_endpoint_server (OpenAI-compatible)
    -> CDP :9222 headless Edge -> M365 Copilot

Design constraints:
  * Python STDLIB ONLY (http.server + http.client) -- zero dependencies, so it
    runs under any python without the venv if ever needed.
  * Blocking JSON round-trips, NO streaming. Upstream timeout is generous
    (300 s) because a single Copilot "thinking" turn legitimately runs
    15-40 s and sometimes longer.
  * The key value is NEVER logged. Logs carry method/path/status/duration only.

Usage:
    python relay\brain_proxy.py                # 127.0.0.1:8012 -> 127.0.0.1:8011
    BRAIN_PROXY_PORT / BRAIN_UPSTREAM_HOST / BRAIN_UPSTREAM_PORT override defaults.
"""
from __future__ import annotations

import http.client
import json
import os
import posixpath
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("BRAIN_PROXY_PORT", "8012"))
UPSTREAM_HOST = os.environ.get("BRAIN_UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("BRAIN_UPSTREAM_PORT", "8011"))
UPSTREAM_TIMEOUT_S = float(os.environ.get("BRAIN_PROXY_TIMEOUT_S", "300"))

# Hop-by-hop headers must not be forwarded (RFC 7230 sec 6.1); Host is rebuilt
# by http.client, and Authorization is REPLACED with our injected key.
_DROP_HEADERS = {
    "host", "authorization", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
    "content-length",
}

_CHAT_COMPLETIONS_TARGET = "/v1/chat/completions"
_MODELS_TARGET = "/v1/models"


def _load_api_key() -> str:
    """MCP_API_KEY from the environment, else parsed out of the repo .env.

    The .env is read with utf-8-sig so a BOM (the PS5.1 Set-Content trap) does
    not corrupt the first key. Stdlib-only on purpose: no python-dotenv.
    """
    key = os.environ.get("MCP_API_KEY", "").strip()
    if key:
        return key
    env_path = Path(__file__).resolve().parent.parent / ".env"
    try:
        for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() == "MCP_API_KEY":
                return value.strip().strip('"').strip("'")
    except OSError as exc:
        print("[brain-proxy] could not read %s: %s" % (env_path, exc), file=sys.stderr)
    return ""


API_KEY = _load_api_key()


class BrainProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BrainProxy/1.0"

    # -- helpers ---------------------------------------------------------- #
    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _validated_target(self) -> str | None:
        """Return a safe origin-form request target or None.

        This proxy injects MCP_API_KEY, so it must not be a general-purpose
        authenticated forward proxy. Only the local OpenAI-compatible endpoint
        surface is allowed.
        """
        raw = self.path or ""
        if "\r" in raw or "\n" in raw:
            return None
        parsed = urlsplit(raw)
        if parsed.scheme or parsed.netloc:
            return None
        normalized = posixpath.normpath(parsed.path or "/")
        if not normalized.startswith("/"):
            normalized = "/" + normalized
        if normalized != parsed.path:
            return None
        if parsed.query:
            return None
        if self.command == "POST" and normalized == _CHAT_COMPLETIONS_TARGET:
            return _CHAT_COMPLETIONS_TARGET
        if self.command == "GET" and normalized == _MODELS_TARGET:
            return _MODELS_TARGET
        return None

    @staticmethod
    def _safe_content_type(value: str | None) -> str:
        ctype = (value or "application/json").split(";", 1)[0].strip().lower()
        if "\r" in (value or "") or "\n" in (value or ""):
            return "application/octet-stream"
        if ctype == "application/json":
            return "application/json"
        if ctype == "text/event-stream":
            return "text/event-stream"
        return "application/octet-stream"

    def _proxy(self) -> None:
        started = time.monotonic()
        if not API_KEY:
            self._send_json(500, {"error": {
                "message": "brain_proxy has no MCP_API_KEY (env or repo .env)",
                "type": "configuration_error"}})
            return
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            self._send_json(411, {"error": {
                "message": "chunked request bodies are not supported; send Content-Length",
                "type": "invalid_request_error"}})
            return
        target = self._validated_target()
        if target is None:
            self._send_json(404, {"error": {
                "message": "unsupported brain proxy endpoint",
                "type": "not_found"}})
            return

        body = self._read_body()
        fwd_headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in _DROP_HEADERS}
        fwd_headers["Authorization"] = "Bearer " + API_KEY
        fwd_headers["Content-Length"] = str(len(body))
        fwd_headers.setdefault("Content-Type", "application/json")

        conn = http.client.HTTPConnection(
            UPSTREAM_HOST, UPSTREAM_PORT, timeout=UPSTREAM_TIMEOUT_S)
        try:
            conn.request(self.command, target, body=body, headers=fwd_headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            self.send_response(resp.status)
            ctype = self._safe_content_type(resp.getheader("Content-Type"))
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
            status = resp.status
        except (ConnectionError, OSError, http.client.HTTPException) as exc:
            status = 502
            try:
                self._send_json(502, {"error": {
                    "message": "upstream %s:%d unreachable or timed out: %s"
                               % (UPSTREAM_HOST, UPSTREAM_PORT, exc.__class__.__name__),
                    "type": "upstream_error"}})
            except OSError:
                pass  # client already gone
        finally:
            conn.close()
        print("[brain-proxy] %s %s -> %d (%.1fs)"
              % (self.command, target or "<blocked>", status, time.monotonic() - started),
              flush=True)

    # -- verb dispatch ----------------------------------------------------- #
    do_GET = _proxy
    do_POST = _proxy
    do_PUT = _proxy
    do_DELETE = _proxy
    do_PATCH = _proxy
    do_OPTIONS = _proxy

    def log_message(self, fmt, *args):  # default access log duplicates ours
        pass


def main() -> None:
    if not API_KEY:
        print("[brain-proxy] WARNING: MCP_API_KEY not found; requests will 500",
              file=sys.stderr, flush=True)
    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), BrainProxyHandler)
    print("[brain-proxy] listening on %s:%d -> forwarding to %s:%d "
          "(key injected, timeout %.0fs)"
          % (LISTEN_HOST, LISTEN_PORT, UPSTREAM_HOST, UPSTREAM_PORT,
             UPSTREAM_TIMEOUT_S), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
