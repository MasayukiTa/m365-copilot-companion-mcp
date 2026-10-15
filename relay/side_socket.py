# -*- coding: utf-8 -*-
"""How a SIDE AGENT (refuter / research / analyst) gets its socket, and what it does when it cannot.

ROOT CAUSE THIS REPLACES (live 2026-10-15). Every fleet goal ended "refuter#N: UNCLEAR:
[rootcause] harness: opening the side page failed: CopilotTabForbidden". The control flow of the
side sessions was still the pre-2026-10-14 one -- "try a socket ONCE; on any False, open a page" --
and the no-tab commit only swapped the page open for a raise (relay/no_tab.py). So the single
socket attempt, which demanded MORE than a worker does, decided the whole review:

  * a worker asks `route.driver_for(...)` and needs a LIVE token + a template (route.ready());
  * the side session additionally required `not route.needs_refresh(url)`, i.e. a token with more
    than the refresh margin (600 s) of life left, and otherwise ran `route.refresh(context, url)`
    -- a SYNCHRONOUS capture on the sweep thread, which the capture floor / backoff refuses and
    which fails whenever the page shows no fresh token. A token with 5 minutes left is perfectly
    usable and is what the fleet normally holds, so the refuter lost to a margin no worker has;
  * any False (or any swallowed exception) was final: `_socket_tried` made it one attempt, and the
    fall-through raised CopilotTabForbidden -> UNCLEAR, which the fleet accepts as "no objection".
    The independent reviewer never reviewed.

THE RULE HERE. A side agent obtains its driver exactly the way a worker does: a live token and a
cached template for ITS agent, an MCP-ready server (relay/mcp_ready.py), a fresh socket
conversation of its own. When it cannot get one it HOLDS, with a reason, and tries again on the
next poll -- it never opens a page and never turns "I could not ask" into a verdict. A token that
needs renewing is renewed OFF the sweep thread (the async capture manager), never synchronously.
UNCLEAR stays reserved for "the reviewer was asked and could not judge".

No credential is read, kept or logged here: a token is only ever passed through the route.
"""
from __future__ import annotations

#: Reason shown while the side agent's own agent surface has never been captured on this machine.
#: A capture never sends a Copilot turn (relay/no_tab.py), and the request template is derived
#: only by a real earlier conversation, so this cannot repair itself by waiting on a capture --
#: it is said out loud instead of being turned into a verdict.
NO_TEMPLATE_REASON = "%s agent has no cached template (a capture never sends a turn to derive one)"


def _default_kick(route, url):
    """Ask the async capture manager (a separate process, never the sweep thread) for a token."""
    from relay.relay_fleet import _consider_socket_refresh
    return _consider_socket_refresh(route, url)


def _disk_template(url):
    from relay.profile_token import load_template
    return load_template(url)


def acquire_side_driver(label, agent_url, *, hold_name, turn_timeout_s, frame_timeout_s,
                        model="", same_agent_as_default=False, route=None, gate=None,
                        kick=None, disk_template=None):
    """Return `(driver, "")` when a socket conversation can be started now, else `(None, reason)`.

    `label`            name of the side agent ("refuter" / "research"); also in the reason text.
    `agent_url`        the agent surface the side agent talks to (its template/token key).
    `hold_name`        the name this wait is registered under in status.json `token_wait`.
    `same_agent_as_default`  the side agent IS the fleet's own agent (the refuter): when its url has
                       no usable entry but the default agent does, use the default entry.
    Never raises, never blocks (the MCP gate caches its probe; a token renewal is a launch of a
    helper process, not a wait on one)."""
    from relay import mcp_ready, token_provider
    if route is None:
        from relay.relay_fleet import _socket_route
        route = _socket_route()
    kick = kick or _default_kick
    disk_template = disk_template or _disk_template

    def held(base):
        try:
            return token_provider.hold().hold_text(hold_name, base)
        except Exception:
            return base

    try:
        if not route.enabled or not route.open():
            return None, held(token_provider.wait_reason(route, agent_url))
        # CREATION GATE: a conversation created while the MCP server is unreachable stays
        # without the connector tools for its whole life (relay/mcp_ready.py). The reviewer
        # reads files through those tools, so it waits for the same gate a worker does.
        why = (gate or mcp_ready.gate()).reason()
        if why:
            return None, held(why)

        key = agent_url or None
        if not route.ready(key):
            if same_agent_as_default and route.ready(None):
                key = None
            else:
                have_tpl = route.template_for(key) is not None
                if not have_tpl:
                    try:
                        have_tpl = disk_template(agent_url) is not None
                    except Exception:
                        have_tpl = False
                if not have_tpl:
                    # No capture can help (it would only raise NoTemplateWithoutTurn): say so and
                    # do not launch a helper process per poll to learn it again.
                    return None, held(NO_TEMPLATE_REASON % label)
                try:
                    if route.needs_refresh(key):
                        kick(route, agent_url)
                except Exception:
                    pass
                return None, held(token_provider.wait_reason(route, agent_url))
        # A usable token that is inside the refresh margin is still usable: renew it in the
        # background, exactly as the fleet sweep does for the workers, and go ahead.
        try:
            if route.needs_refresh(key):
                kick(route, agent_url if key is not None else route.default_agent_url)
        except Exception:
            pass
        drv = route.driver_for(label, agent_url=key, model=model,
                               turn_timeout_s=turn_timeout_s, frame_timeout_s=frame_timeout_s)
        if drv is None:
            return None, held(token_provider.wait_reason(route, agent_url))
    except Exception as exc:
        return None, held("%s socket not available yet: %s" % (label, type(exc).__name__))
    try:
        token_provider.hold().release(hold_name)
    except Exception:
        pass
    return drv, ""
