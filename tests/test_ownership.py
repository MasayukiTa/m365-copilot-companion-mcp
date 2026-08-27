"""The ledger must never be believed on its own.

Recording ownership is not obviously safer than deriving it. A process killed before it can
write "finished" leaves a claim standing for ever, and a launch gate that trusts the file then
refuses every future run -- a leak turned into a lockout. That is the failure this design is
built around, and it is what most of these tests are about.
"""
import os
import time

import pytest

from relay import ownership as own


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    p = tmp_path / "ownership.jsonl"
    monkeypatch.setattr(own, "LEDGER", str(p))
    return str(p)


ALIVE = lambda pid: True
DEAD = lambda pid: False


def test_a_claim_is_recorded_and_read_back(ledger):
    own.claim("page", "p1", run_id="r1", pid=123)
    claims = own.read_claims(ledger)
    assert ("page", "p1") in claims
    assert claims[("page", "p1")]["run_id"] == "r1"


def test_releasing_removes_the_claim(ledger):
    own.claim("page", "p1", run_id="r1", pid=123)
    own.release("page", "p1", run_id="r1")
    assert own.read_claims(ledger) == {}


def test_releasing_twice_is_not_an_error(ledger):
    own.claim("page", "p1", run_id="r1", pid=123)
    own.release("page", "p1", run_id="r1")
    own.release("page", "p1", run_id="r1")
    assert own.read_claims(ledger) == {}


# ---- the lockout the design exists to prevent ------------------------------------------------

def test_a_claim_whose_owner_died_is_not_live(ledger):
    """The killed-before-release case. Believing this claim is how a leak becomes a lockout."""
    own.claim("page", "p1", run_id="r1", pid=999999)
    assert own.read_claims(ledger)                      # the file still says it is owned
    assert own.live_claims(DEAD, path=ledger) == {}     # reality says otherwise


def test_an_expired_lease_is_not_live_even_if_the_pid_is_alive(ledger):
    """A pid can be recycled, and a process can hang without releasing. The lease is the
    second, independent way out."""
    own.claim("page", "p1", run_id="r1", pid=os.getpid())
    later = time.time() + own.LEASE_S + 1
    assert own.live_claims(ALIVE, path=ledger, now=later) == {}


def test_renewing_keeps_a_long_run_alive(ledger):
    own.claim("page", "p1", run_id="r1", pid=os.getpid())
    mid = time.time() + own.LEASE_S - 1
    own.renew("page", "p1", run_id="r1")
    assert own.live_claims(ALIVE, path=ledger, now=mid)


def test_a_live_claim_needs_BOTH_a_lease_and_a_living_process(ledger):
    own.claim("page", "p1", run_id="r1", pid=os.getpid())
    assert own.live_claims(ALIVE, path=ledger)          # both hold
    assert own.live_claims(DEAD, path=ledger) == {}     # pid fails
    assert own.live_claims(ALIVE, path=ledger,
                           now=time.time() + own.LEASE_S + 1) == {}   # lease fails


# ---- reconciliation: the ledger and the machine, together ------------------------------------

def test_a_page_nobody_claims_is_orphaned(ledger):
    """The nine-and-a-half-hour page. It exists and no live claim covers it."""
    result = own.reconcile({("page", "p1"): "chat page"}, ALIVE, path=ledger)
    assert list(result["orphaned"]) == [("page", "p1")]
    assert result["claimed"] == {}


def test_a_page_a_live_run_claims_is_left_alone(ledger):
    own.claim("page", "p1", run_id="r1", pid=os.getpid())
    result = own.reconcile({("page", "p1"): "chat page"}, ALIVE, path=ledger)
    assert list(result["claimed"]) == [("page", "p1")]
    assert result["orphaned"] == {}


def test_a_page_claimed_by_a_DEAD_run_is_orphaned(ledger):
    """This is the whole point: the claim exists, and it does not protect anything."""
    own.claim("page", "p1", run_id="r1", pid=999999)
    result = own.reconcile({("page", "p1"): "chat page"}, DEAD, path=ledger)
    assert list(result["orphaned"]) == [("page", "p1")]


def test_a_claim_for_something_that_no_longer_exists_is_stale_not_orphaned(ledger):
    """The ledger being behind is not a leak. Nothing to close."""
    own.claim("page", "gone", run_id="r1", pid=os.getpid())
    result = own.reconcile({}, ALIVE, path=ledger)
    assert list(result["stale"]) == [("page", "gone")]
    assert result["orphaned"] == {}


def test_reconcile_decides_and_does_not_destroy():
    """A function that both decides and destroys cannot be tested safely, so this one returns."""
    import inspect
    src = inspect.getsource(own.reconcile)
    for destructive in ("close(", "kill", "Stop-Process", "remove", "unlink"):
        assert destructive not in src


def test_a_missing_ledger_is_an_empty_one(tmp_path, monkeypatch):
    """First run on a fresh machine must not be a special case."""
    monkeypatch.setattr(own, "LEDGER", str(tmp_path / "nope.jsonl"))
    assert own.read_claims() == {}
    # a dict, as the contract says -- the first version of this line passed a set
    assert list(own.reconcile({("page", "p"): "x"}, ALIVE)["orphaned"]) == [("page", "p")]


def test_a_corrupt_line_does_not_lose_the_rest(ledger):
    own.claim("page", "p1", run_id="r1", pid=os.getpid())
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    own.claim("page", "p2", run_id="r1", pid=os.getpid())
    assert len(own.read_claims(ledger)) == 2


# ---- the concurrent run this protects --------------------------------------------------------

def test_a_second_run_must_not_close_the_first_runs_working_page(ledger):
    """The bug this exists for. `_close_idle_copilot_pages` closed EVERY Copilot page on the
    context while its docstring said "nobody owns yet" and nothing checked. Concurrent runs
    are ordinary here, so the second run to start would close the first one's working page,
    and the first would see a TargetClosedError it could not explain."""
    own.claim("page", "target-A", run_id="run-1", pid=os.getpid())      # run 1 is working
    observed = {("page", "target-A"): "chat", ("page", "target-B"): "chat"}
    result = own.reconcile(observed, ALIVE, path=ledger)
    assert list(result["orphaned"]) == [("page", "target-B")]
    assert ("page", "target-A") in result["claimed"]


def test_a_page_whose_run_was_killed_is_reclaimed(ledger):
    """The other direction: ownership must not become a way to leak for ever."""
    own.claim("page", "target-A", run_id="run-1", pid=999999)
    result = own.reconcile({("page", "target-A"): "chat"}, DEAD, path=ledger)
    assert list(result["orphaned"]) == [("page", "target-A")]


def test_a_long_run_that_renews_keeps_its_pages(ledger):
    """A run outliving the lease must say so, or a starting run reconciles its live working
    pages as residue."""
    own.claim("page", "target-A", run_id="run-1", pid=os.getpid())
    late = time.time() + own.LEASE_S * 3
    assert own.reconcile({("page", "target-A"): "chat"}, ALIVE,
                         path=ledger, now=late)["orphaned"]
    own.renew("page", "target-A", run_id="run-1")
    assert not own.reconcile({("page", "target-A"): "chat"}, ALIVE,
                             path=ledger, now=time.time() + 1)["orphaned"]
