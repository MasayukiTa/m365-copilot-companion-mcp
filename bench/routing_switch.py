"""One place to ask "is this run routed?", because the answer was got wrong twice.

Both `pro_stage_goals.py` and `pro_capture.py` had the same three lines:

    try:
        from relay import broker_client as bc
    except ImportError:
        routed = False

Run as `python bench/<script>.py`, sys.path[0] is bench/ and `import relay` always raises, so
both scripts read "routing is off" every single time -- while the switch was on and the
operator had been told it was on. Staging produced four local clones and four lines saying
"ok"; capture read the empty local directories and reported four skips reading "not a worktree
root", after a worker had edited seven files inside its container. Neither failure looks like a
switch that was ignored. One reads as a staging problem and the other as a modelling result.

So the question is asked here, once, and being unable to answer it is an error rather than a
"no".
"""
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKER = os.path.join(REPO, ".fleet", "BROKER_ON")


def routing_requested():
    """Was routing asked for -- by either switch, before any import is attempted.

    Both switches, because reading only the environment variable left the marker-file route
    with exactly the silent fallback this module exists to close.
    """
    if (os.environ.get("SWE_BROKER") or "").strip().lower() in ("1", "on", "true", "yes"):
        return True
    try:
        return os.path.isfile(MARKER)
    except OSError:
        return False


def broker(context=""):
    """relay.broker_client if routing is carrying this run, else None.

    Raises RuntimeError when routing was asked for and the module cannot be reached: falling
    back to the local machine there is the behaviour being replaced, and doing it silently is
    how a routed run comes to look like an ordinary one that went badly.
    """
    import sys
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    asked = routing_requested()
    try:
        from relay import broker_client as bc
    except ImportError as exc:
        if asked:
            raise RuntimeError(
                "routing was asked for but relay.broker_client could not be imported (%s)%s; "
                "refusing to fall back to this machine, which is the behaviour routing "
                "replaces" % (exc, (" [%s]" % context) if context else ""))
        return None
    return bc if bc.enabled() else None
