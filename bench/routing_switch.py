"""One place to ask "is this run routed?", because the answer was got wrong twice.

Both `pro_stage_goals.py` and `pro_capture.py` had the same three lines:

    try:
        from relay import broker_client as bc        # where it lived at the time
    except ImportError:
        routed = False

Run as `python bench/<script>.py`, sys.path[0] is bench/ and that import always raises, so
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


#: Whether the broker answered a ping, cached for the life of the process. `broker()` is
#: called once per instance -- pro_capture.py and pro_stage_goals.py both call it in a loop
#: over the run's instances -- and re-pinging on every call would turn a forty-instance run
#: into forty extra SSH round trips for a fact that does not change mid-run. None means "not
#: checked yet this process"; True/False is the cached answer.
_broker_live = None
_broker_ping_error = None


def broker(context=""):
    """relay.broker_client if routing is carrying this run AND the broker answers, else None.

    Raises RuntimeError when routing was asked for and the module cannot be reached, or is
    reachable but never answers a ping: falling back to the local machine there is the
    behaviour being replaced, and doing it silently is how a routed run comes to look like an
    ordinary one that went badly -- a run with routing switched on against a down host used to
    proceed and fail one `create` at a time across forty instances instead of failing once,
    here, with a reason.
    """
    global _broker_live, _broker_ping_error
    import sys
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    asked = routing_requested()
    try:
        from bench.remote import broker_client as bc
    except ImportError as exc:
        if asked:
            raise RuntimeError(
                "routing was asked for but bench.remote.broker_client could not be imported "
                "(%s)%s; "
                "refusing to fall back to this machine, which is the behaviour routing "
                "replaces" % (exc, (" [%s]" % context) if context else ""))
        return None
    if not bc.enabled():
        return None
    if _broker_live is None:
        try:
            bc.ping()
            _broker_live = True
        except Exception as exc:
            _broker_live = False
            _broker_ping_error = exc
    if _broker_live:
        return bc
    if asked:
        raise RuntimeError(
            "routing was asked for and bench.remote.broker_client imported, but the broker "
            "did not answer a ping (%s)%s; refusing to fall back to this machine, which is "
            "the behaviour routing replaces" % (
                _broker_ping_error, (" [%s]" % context) if context else ""))
    return None
