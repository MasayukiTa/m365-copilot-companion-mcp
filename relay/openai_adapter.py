"""OpenAI-compatible chat-completions shim over the M365-Copilot relay.

Purpose
-------
Expose an *OpenAI-compatible* HTTP surface (`POST /v1/chat/completions`,
`GET /v1/models`) so ANY external harness that speaks the OpenAI chat API
(OpenHands / TheAgentCompany / litellm / the openai SDK, ...) can use the
Copilot-routed Opus 4.8 as its LLM backend -- no Anthropic API spend.

It does this by flattening the OpenAI `messages` array into ONE prompt, typing
that prompt into a live Copilot conversation via the existing relay primitives
(`CopilotWebDriver.send` / `.wait_for_idle` / `.read_last_response`), and
wrapping the streamed reply back into an OpenAI `chat.completion` JSON.

Design constraints honoured here
--------------------------------
* ADDITIVE + FLAG-GATED. `register_openai_routes(app)` only mounts the routes
  when `OPENAI_COMPAT=1`; otherwise it is a no-op and the existing `/mcp`
  server is byte-for-byte unchanged.
* NO import-time side effects. Importing this module builds nothing -- no
  Playwright, no Edge, no CDP. The relay is acquired LAZILY on the first
  completion (see `_get_relay`), and a failure there degrades to HTTP 503,
  never a server crash.
* SINGLE-THREADED relay. The underlying Copilot conversation (and the
  Playwright *sync* objects that drive it) are single-threaded and must be
  touched only from the thread that owns them. We therefore confine the whole
  CDP browser + driver to ONE dedicated worker thread and marshal every
  send/wait/read onto it through a queue. An asyncio.Lock additionally
  serialises completions so only one round-trip is ever in flight; queued
  requests simply await their turn.
* AUTH reuses the same Bearer token the /mcp side uses (`MCP_API_KEY`).

This module is pure stdlib + Starlette/anyio (already present via
fastmcp/uvicorn). No new dependencies.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from typing import Any, Optional

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

# Model id advertised to clients. Arbitrary but stable -- harnesses pin to it.
MODEL_ID = "m365-copilot-opus"

# How long (seconds) to wait for a single Copilot turn to finish before giving
# up on a completion. Mirrors run_relay's generous per-turn budget: slow
# django/sympy-style turns legitimately run minutes.
DEFAULT_TURN_TIMEOUT_S = int(os.environ.get("OPENAI_COMPAT_TURN_TIMEOUT_S", "1800"))


# --------------------------------------------------------------------------- #
# Request parsing / prompt flattening
# --------------------------------------------------------------------------- #
def _content_to_text(content: Any) -> str:
    """Normalise an OpenAI message `content` to a plain string.

    Robust to the two shapes the spec allows:
      * a plain string, or
      * a list of parts like [{"type": "text", "text": "..."}, ...]
        (non-text parts -- images etc. -- are skipped with a placeholder so a
        multimodal client doesn't silently lose a turn's structure).
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    out.append(part["text"])
                elif "text" in part and isinstance(part["text"], str):
                    out.append(part["text"])
                else:
                    # image_url or other modality the text relay can't carry.
                    ptype = part.get("type", "unknown")
                    out.append(f"[non-text content part: {ptype}]")
            elif isinstance(part, str):
                out.append(part)
        return "\n".join(out)
    # Anything else: stringify defensively.
    return str(content)


_ROLE_HEADERS = {
    "system": "## System",
    "user": "## User",
    "assistant": "## Assistant",
    "tool": "## Tool",
    "function": "## Tool",
}


def flatten_messages(messages: list[dict]) -> str:
    """Render the OpenAI `messages` array into ONE Copilot-consumable prompt.

    Each turn is emitted under a clear role marker ("## System", "## User",
    "## Assistant", ...). We append a trailing empty "## Assistant" header so
    the model understands it should produce the next assistant turn -- i.e. it
    answers the LATEST user message rather than continuing the transcript.
    """
    blocks = []
    for msg in messages or []:
        role = (msg.get("role") or "user").lower()
        header = _ROLE_HEADERS.get(role, f"## {role.capitalize()}")
        text = _content_to_text(msg.get("content"))
        blocks.append(f"{header}\n{text}".rstrip())
    # Trailing cue so Copilot answers as the assistant.
    blocks.append("## Assistant")
    return "\n\n".join(blocks).strip()


# --------------------------------------------------------------------------- #
# Token accounting (ESTIMATE ONLY)
# --------------------------------------------------------------------------- #
def _estimate_tokens(text: str) -> int:
    """CHEAP, NON-AUTHORITATIVE token estimate (~chars/4).

    This is NOT a real BPE count -- Copilot does not expose token usage. It is a
    rough heuristic so the OpenAI `usage` block is populated with plausible
    numbers for harnesses that read it. Do not bill or budget against it.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


def build_completion_response(reply: str, prompt: str, model: str) -> dict:
    """Wrap a relay reply into an OpenAI `chat.completion` JSON object."""
    prompt_tokens = _estimate_tokens(prompt)        # ESTIMATE (chars/4)
    completion_tokens = _estimate_tokens(reply)     # ESTIMATE (chars/4)
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


# --------------------------------------------------------------------------- #
# Lazy, thread-confined relay worker
# --------------------------------------------------------------------------- #
# Playwright sync objects are NOT thread-safe and must be driven from the thread
# that created them. We therefore own the CDP browser + CopilotWebDriver inside
# ONE dedicated worker thread and post (send -> wait_for_idle -> read) jobs to it
# over a queue. The HTTP handler (running in the event loop) hands a job to the
# worker via `anyio.to_thread.run_sync` and blocks the threadpool thread on the
# reply -- never the event loop.
class RelayUnavailable(RuntimeError):
    """Raised when no live Copilot relay can be acquired (-> HTTP 503)."""


class _RelayWorker:
    """Owns the CDP browser + driver on a single private thread."""

    def __init__(self) -> None:
        self._jobs: "queue.Queue[tuple]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._start_error: Optional[BaseException] = None
        self._started = False

    # ---- public API (called from a threadpool thread, NOT the event loop) ----
    def ensure_started(self) -> None:
        if self._started:
            if self._start_error is not None:
                raise RelayUnavailable(str(self._start_error))
            return
        self._started = True
        self._thread = threading.Thread(
            target=self._run, name="openai-relay-worker", daemon=True
        )
        self._thread.start()
        # Wait (bounded) for the browser/driver to come up.
        if not self._ready.wait(timeout=120):
            raise RelayUnavailable(
                "relay worker did not become ready within 120s "
                "(is the dedicated Edge running with CDP open?)"
            )
        if self._start_error is not None:
            raise RelayUnavailable(str(self._start_error))

    def complete(self, prompt: str, timeout_s: int) -> str:
        """Submit ONE prompt and return the reply text. Blocking (call off-loop)."""
        self.ensure_started()
        reply_box: "queue.Queue" = queue.Queue(maxsize=1)
        self._jobs.put((prompt, timeout_s, reply_box))
        kind, payload = reply_box.get()
        if kind == "ok":
            return payload
        raise RelayUnavailable(payload) if kind == "unavailable" else RuntimeError(payload)

    # ---- private: runs entirely on the worker thread -------------------------
    def _run(self) -> None:
        driver = None
        playwright_cm = None
        try:
            driver, playwright_cm = self._build_driver()
            self._ready.set()
        except BaseException as e:  # noqa: BLE001 - surface any bringup failure
            self._start_error = e
            self._ready.set()
            return
        try:
            while True:
                prompt, timeout_s, reply_box = self._jobs.get()
                if prompt is None:  # shutdown sentinel
                    break
                try:
                    driver.send(prompt)
                    got = driver.wait_for_idle(timeout_s=timeout_s)
                    if not got:
                        reply_box.put(("error", "timed out waiting for Copilot reply"))
                        continue
                    # Prefer the chrome-free reply extractor (reply-div only); fall back to the
                    # full-bubble reader on older drivers so this never breaks.
                    reader = getattr(driver, "read_last_reply_clean", driver.read_last_response)
                    reply = reader()
                    reply_box.put(("ok", reply))
                except Exception as e:  # noqa: BLE001
                    reply_box.put(("error", f"{type(e).__name__}: {e}"))
        finally:
            if playwright_cm is not None:
                try:
                    playwright_cm.__exit__(None, None, None)
                except Exception:
                    pass

    def _build_driver(self):
        """Connect to the dedicated Edge over CDP and bind a CopilotWebDriver.

        Mirrors bridge/copilot_bridge.py:main exactly:
            connect_over_cdp(MCP_CDP_URL) -> contexts[0] -> find/open agent page
            -> CopilotWebDriver(page)

        Returns (driver, playwright_context_manager) so the worker can tear the
        Playwright runtime down on shutdown. Raises on any failure; the caller
        records it as a start_error and every completion then yields a 503.
        """
        # Imported HERE (not at module top) so `import relay.openai_adapter` has
        # NO side effects and does not require playwright to be installed unless
        # the OpenAI-compat surface is actually exercised.
        from playwright.sync_api import sync_playwright

        from relay.copilot_autopilot_relay import CopilotWebDriver

        cdp = os.environ.get("MCP_CDP_URL", "http://localhost:9222")
        pw = sync_playwright().start()
        try:
            br = pw.chromium.connect_over_cdp(cdp)
            ctx = br.contexts[0] if br.contexts else br.new_context()
            page = self._find_agent_page(ctx)
            driver = CopilotWebDriver(page)
        except Exception:
            try:
                pw.stop()
            except Exception:
                pass
            raise

        # Wrap pw.stop() in an object with __exit__ for uniform teardown.
        class _PWGuard:
            def __exit__(self, *a):
                try:
                    pw.stop()
                except Exception:
                    pass

        return driver, _PWGuard()

    @staticmethod
    def _find_agent_page(ctx):
        """Open MCP_IMPL_AGENT_URL, else reuse any open agent tab.

        Same selection logic as the bridge's `_find_or_open_agent`, duplicated
        here (rather than imported) so this module has no hard dependency on the
        bridge package layout. Uses the shared COPILOT_SELECTORS.
        """
        from relay.copilot_autopilot_relay import COPILOT_SELECTORS

        url = os.environ.get("MCP_IMPL_AGENT_URL", "").strip()
        if url:
            pg = ctx.new_page()
            pg.goto(url, wait_until="domcontentloaded")
            for _ in range(40):
                pg.wait_for_timeout(1000)
                if pg.locator(COPILOT_SELECTORS["composer"]).count() > 0:
                    return pg
            return pg  # return anyway; send() will surface a clear error if dead
        for pg in ctx.pages:
            if "/chat/agent/" in (pg.url or "") and \
                    pg.locator(COPILOT_SELECTORS["composer"]).count() > 0:
                return pg
        raise RelayUnavailable(
            "no Copilot agent page found: set MCP_IMPL_AGENT_URL in .env or open "
            "an agent tab in the dedicated Edge (CDP on MCP_CDP_URL)"
        )


# Module-level singletons. The worker is built lazily on first completion.
_worker: Optional[_RelayWorker] = None
_worker_init_lock = threading.Lock()
# Serialises completions: only ONE round-trip in flight; others await their turn.
_completion_lock = anyio.Lock()


def _get_worker() -> _RelayWorker:
    global _worker
    if _worker is None:
        with _worker_init_lock:
            if _worker is None:
                _worker = _RelayWorker()
    return _worker


def _run_completion_blocking(prompt: str, timeout_s: int) -> str:
    """Blocking entry point run inside anyio.to_thread (NOT the event loop)."""
    worker = _get_worker()
    return worker.complete(prompt, timeout_s)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def _expected_key() -> Optional[str]:
    return os.environ.get("MCP_API_KEY")


def _check_auth(request: Request) -> Optional[JSONResponse]:
    """Return a 401 JSONResponse if the Bearer token is missing/wrong, else None."""
    expected = _expected_key()
    if not expected:
        # No key configured -> we cannot authenticate. Fail closed.
        return JSONResponse(
            {"error": {"message": "server auth not configured (MCP_API_KEY unset)",
                       "type": "configuration_error"}},
            status_code=500,
        )
    header = request.headers.get("authorization", "")
    token = ""
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
    if token != expected:
        return JSONResponse(
            {"error": {"message": "invalid or missing bearer token",
                       "type": "invalid_request_error", "code": "invalid_api_key"}},
            status_code=401,
        )
    return None


# --------------------------------------------------------------------------- #
# Route handlers
# --------------------------------------------------------------------------- #
async def list_models(request: Request) -> Response:
    auth_err = _check_auth(request)
    if auth_err is not None:
        return auth_err
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {
                    "id": MODEL_ID,
                    "object": "model",
                    "created": 0,
                    "owned_by": "m365-copilot-companion",
                }
            ],
        }
    )


async def chat_completions(request: Request) -> Response:
    auth_err = _check_auth(request)
    if auth_err is not None:
        return auth_err

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": {"message": "request body must be valid JSON",
                       "type": "invalid_request_error"}},
            status_code=400,
        )

    if not isinstance(body, dict):
        return JSONResponse(
            {"error": {"message": "request body must be a JSON object",
                       "type": "invalid_request_error"}},
            status_code=400,
        )

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return JSONResponse(
            {"error": {"message": "'messages' must be a non-empty array",
                       "type": "invalid_request_error", "param": "messages"}},
            status_code=400,
        )

    # Streaming: not implemented in the non-streaming-first cut. SSE could be
    # wired through bridge/copilot_bridge.py's token streamer later. TODO(stream).
    if body.get("stream"):
        return JSONResponse(
            {"error": {"message": "stream=true is not supported yet (TODO); "
                                  "request a non-streaming completion",
                       "type": "invalid_request_error", "param": "stream"}},
            status_code=400,
        )

    model = body.get("model") or MODEL_ID
    prompt = flatten_messages(messages)

    # Serialise: one Copilot round-trip at a time; queued requests await here.
    async with _completion_lock:
        try:
            reply = await anyio.to_thread.run_sync(
                _run_completion_blocking, prompt, DEFAULT_TURN_TIMEOUT_S
            )
        except RelayUnavailable as e:
            return JSONResponse(
                {"error": {"message": f"Copilot relay unavailable: {e}",
                           "type": "service_unavailable"}},
                status_code=503,
            )
        except Exception as e:  # noqa: BLE001 - never crash the server on a turn
            return JSONResponse(
                {"error": {"message": f"completion failed: {type(e).__name__}: {e}",
                           "type": "internal_error"}},
                status_code=502,
            )

    return JSONResponse(build_completion_response(reply, prompt, model))


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def openai_routes() -> list[Route]:
    """The Starlette routes for the OpenAI-compatible surface."""
    return [
        Route("/v1/models", list_models, methods=["GET"]),
        Route("/v1/chat/completions", chat_completions, methods=["POST"]),
    ]


def register_openai_routes(app) -> bool:
    """Mount the OpenAI-compatible routes onto an existing Starlette `app`.

    FLAG-GATED: only mounts when OPENAI_COMPAT=1. Returns True if mounted, False
    if skipped, so main.py can log which path it took. Additive only -- it
    appends to app.router.routes and never touches the existing /mcp route.
    """
    if os.environ.get("OPENAI_COMPAT", "") not in ("1", "true", "True", "yes"):
        return False
    existing = {getattr(r, "path", None) for r in app.router.routes}
    for route in openai_routes():
        if route.path not in existing:
            app.router.routes.append(route)
    return True
