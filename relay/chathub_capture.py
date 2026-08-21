"""Take the token and the request shape from the signed-in browser, by watching it work.

WHY THIS IS A SEPARATE MODULE. `chathub.py` speaks the protocol and touches no browser; this
one touches a browser and speaks no protocol. The split is what keeps the protocol testable
without a network, and it is also where the honesty of the route lives: everything the socket
sends was observed here first, so no field in a request is a value somebody made up.

WHAT IS OBSERVED, AND WHY IT HAS TO BE
  * the access token -- it is not in localStorage or sessionStorage (measured, 2026-08-20); the
    only place it appears is the query string of the socket the client opens.
  * the request template -- `variants` alone carried 68 feature flags, several of which select
    the responding model. Freezing that list into source would silently serve a different
    product the first time Microsoft ships a flag.

THIS DOES NOT MINT ANYTHING. It reads what the browser already obtained for the signed-in user
and the same backend. If nothing is observed, it raises, and the caller uses a tab -- the
fallback is a working path, not a degraded one.
"""
from __future__ import annotations

import json

from relay.chathub import RS, ChatHubError, RequestTemplate, expires_in


def _frames(payloads):
    out = []
    for payload in payloads:
        for part in (payload or "").split(RS):
            if not part.strip():
                continue
            try:
                out.append(json.loads(part))
            except Exception:
                continue
    return out


def capture(page, *, prompt: str = "ping", timeout_s: float = 180.0, attempts: int = 3):
    """One real turn on `page`, watched. Returns (token, RequestTemplate).

    The turn is a real one and costs what a turn costs; it is run about once per token
    lifetime (measured 15-79 minutes), not once per request.

    RETRIED, BECAUSE THIS IS A SINGLE POINT OF FAILURE. Every socket in the fleet depends on
    one capture succeeding, and a capture is an ordinary tab turn -- so it inherits the send
    race the tab path already retries around ("composer cleared without a conversation or
    generation acknowledgement"). That happened on the first attempt of a real run and left
    the whole fleet on tabs for want of one send. One flaky turn should cost a retry, not the
    route.
    """
    last = None
    for attempt in range(max(1, int(attempts))):
        try:
            return _capture_once(page, prompt=prompt, timeout_s=timeout_s)
        except Exception as exc:
            last = exc
    raise ChatHubError("capture failed after %d attempts: %s: %s"
                       % (max(1, int(attempts)), type(last).__name__, str(last)[:160]))


def _capture_once(page, *, prompt: str, timeout_s: float):
    """One attempt. Every failure mode here is a raise, so the retry above can see it."""
    from relay.copilot_autopilot_relay import CopilotWebDriver

    urls, sent = [], []
    cdp = page.context.new_cdp_session(page)
    cdp.send("Network.enable")
    cdp.on("Network.webSocketCreated", lambda e: urls.append(e.get("url") or ""))
    cdp.on("Network.webSocketFrameSent",
           lambda e: sent.append((e.get("response") or {}).get("payloadData", "")))

    drv = CopilotWebDriver(page)
    drv._count_before = drv._answers().count()
    drv.send(prompt)
    drv.wait_for_idle(timeout_s=timeout_s)

    url = next((u for u in urls if "Chathub" in u), "")
    if not url:
        # The client opens a socket per turn in every capture taken so far. If it ever reuses
        # one, nothing is observed and there is nothing to guess from -- so this says so.
        raise ChatHubError("no Chathub socket was opened during the capture turn")
    token = ""
    if "access_token=" in url:
        token = url.split("access_token=", 1)[1].split("&", 1)[0]
    if not token or expires_in(token) <= 0:
        raise ChatHubError("the captured socket carried no usable token")

    frame = next(((f.get("arguments") or [{}])[0] for f in _frames(sent)
                  if f.get("type") == 4 and f.get("target") == "chat"), None)
    if not frame:
        raise ChatHubError("the capture turn sent no chat frame")
    template = RequestTemplate.from_capture(url, frame)
    if not template.gpt_id:
        # A template with no agent reaches the DEFAULT Copilot -- no tools, no tenant
        # grounding. That is a different product answering, and it must not pass silently as
        # if the socket route had worked.
        raise ChatHubError("the captured request names no agent; the tab is not on an agent "
                           "surface, and a socket built from it would reach the default "
                           "Copilot instead")
    return token, template
