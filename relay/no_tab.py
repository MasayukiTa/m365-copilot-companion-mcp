"""The owner's rule, as code: Copilot never runs in a browser tab.

HARD RULE (owner, 2026-10-14): Copilot must NEVER run in a browser tab, in any case. No code
path that executes a goal may open a tab for it -- not as a fallback, not after a wait, not
for a side agent (research / refuter), and not to capture a token by sending a turn.

WHY A MODULE AND NOT A COMMENT. The rule used to be a design preference ("the socket is a
speed-up, never a capability; losing it opens a tab"), and every path that honoured it opened
a tab on the one sweep thread, froze status.json for up to 210 s and made the watchdog kill the
Edge. A preference does not stop the next fallback being added. This does: every place that
used to open a Copilot tab now calls `forbid_copilot_tab(where)`, which raises
`CopilotTabForbidden`, and `relay/test_no_goal_path_opens_a_tab.py` fails the build if a
`new_page(` call or an `_open_fresh(` call appears in a goal-path module outside the allow-list
below.

WHAT A GOAL DOES INSTEAD OF A TAB: it waits, with a reason in status.json, while the thing it
lacks is repaired -- "waiting for token" (an off-thread capture that sends no turn retries
with backoff) or "socket route unavailable" (the route is probed and reopened by
relay/route_reopen.py). Waiting has no ceiling, because the alternative is forbidden.

WHAT IS NOT COVERED. A page that carries NO Copilot turn is not Copilot running in a tab: the
token capture's light page (relay/profile_token.py) loads the signed-in chat page, reads the
Authorization header the page itself sent, and closes. It sends no message. That is the only
page this product opens on the Copilot surface, and it is not a goal path.
"""
from __future__ import annotations


class CopilotTabForbidden(RuntimeError):
    """A code path tried to run Copilot in a browser tab. This is never allowed."""


def forbid_copilot_tab(where: str) -> None:
    """Raise `CopilotTabForbidden`. Called where a Copilot tab used to be opened."""
    raise CopilotTabForbidden(
        "refused: Copilot never runs in a browser tab (%s). The goal waits for the socket "
        "route instead; see relay/no_tab.py" % (where,))
