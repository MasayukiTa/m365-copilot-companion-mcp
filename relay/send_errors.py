"""Shared send-outcome exceptions with retry semantics.

Kept browser-independent so deterministic coordinators can classify a send result without
importing Playwright or the M365 DOM driver.
"""


class FreshSubmitAmbiguous(RuntimeError):
    """A fresh submit may already have landed, but its USER-turn receipt is ambiguous.

    Callers MUST NOT automatically resend the same payload. Retrying can duplicate a user turn.
    Stop/pause the work and require a new observable decision boundary instead.
    """
