"""An EVALERR without a cause cannot be acted on, and thirty of them were recorded that way.

RESOLVED and `not` are answers. EVALERR is a question -- "the evaluation could not be run" --
and the ledger held 30 of them with an empty note. Nobody could tell afterwards whether the
host was down, the container died, or the disk filled, so the whole run's grading was
unexplainable rather than merely bad.

The far side wrote its reason into the verdict file. poll_verdict() pulled one line out of that
file with a regex and discarded the rest on the way past.
"""
import io
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bench import swe_grade_batch as G


@pytest.fixture(autouse=True)
def _tmp(monkeypatch):
    monkeypatch.setattr(G, "TMP", tempfile.mkdtemp())


def _verdict(monkeypatch, content):
    """Make the fetch produce `content` without touching a remote host."""
    def fake(remote, local):
        io.open(local, "w", encoding="utf-8").write(content)
        return True
    monkeypatch.setattr(G.R, "_scp_from", fake)


def test_a_clean_verdict_carries_no_note(monkeypatch):
    # A note on a successful grade is noise. The absence of one on EVALERR is the problem.
    _verdict(monkeypatch, "RUNNER_DONE\nVERDICT=RESOLVED\n")
    v, why = G.poll_verdict("r1")
    assert v == "RESOLVED"
    assert why == ""


def test_an_unresolved_verdict_carries_no_note(monkeypatch):
    _verdict(monkeypatch, "RUNNER_DONE\nVERDICT=not\n")
    v, why = G.poll_verdict("r2")
    assert v == "not"
    assert why == ""


def test_an_evalerr_carries_the_far_sides_own_words(monkeypatch):
    _verdict(monkeypatch,
             "RUNNER_DONE\ndocker: Error response from daemon: no space left on device\n"
             "VERDICT=EVALERR\n")
    v, why = G.poll_verdict("r3")
    assert v == "EVALERR"
    assert "no space left on device" in why, why


def test_a_verdict_file_with_no_VERDICT_line_does_not_crash(monkeypatch):
    """WAS AN AttributeError, inside the poll loop.

    re.search returned None and `m.group(1)` raised, so ONE malformed verdict file took down
    the grading of every instance still in flight -- not just its own.
    """
    _verdict(monkeypatch, "RUNNER_DONE\ncontainer exited 137 (OOM killed)\n")
    v, why = G.poll_verdict("r4")
    assert v == "EVALERR"
    assert "137" in why and "no VERDICT" in why, why


def test_an_empty_verdict_file_says_so(monkeypatch):
    # "(the verdict file was empty)" is a fact. An empty note is not.
    _verdict(monkeypatch, "RUNNER_DONE\n")
    v, why = G.poll_verdict("r5")
    assert v == "EVALERR"
    assert "empty" in why.lower(), why


def test_an_unfinished_run_is_not_a_verdict(monkeypatch):
    # No RUNNER_DONE means still in flight. Reporting that as EVALERR would retire an instance
    # that is simply still working.
    _verdict(monkeypatch, "starting up\n")
    v, why = G.poll_verdict("r6")
    assert v == "" and why == ""


def test_an_unreadable_file_is_reported_rather_than_swallowed(monkeypatch):
    def fake(remote, local):
        return True            # claims success, writes nothing
    monkeypatch.setattr(G.R, "_scp_from", fake)
    v, why = G.poll_verdict("r7")
    # Either "not finished" or an explained EVALERR is acceptable; a silent empty note is not.
    assert (v, why) == ("", "") or (v == "EVALERR" and why)


def test_the_note_is_bounded():
    # A verdict file can be a whole build log. The ledger is read by people and by the
    # calibration tooling; a megabyte in one field helps neither.
    long_tail = "RUNNER_DONE\n" + ("x" * 5000) + "\nVERDICT=EVALERR\n"
    assert len(G._tail_reason(long_tail)) <= 300
