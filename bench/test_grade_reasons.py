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


# --------------------------------------------------- the grader must be watchable while it runs


def test_log_actually_prints(capsys):
    """IT DID NOT, AND ITS OWN except HID THAT.

    The flush fix was applied by rewriting statement-level print( to log( -- including the two
    print() calls INSIDE log() itself. That made log() call itself, and the RecursionError was
    caught by its own `except Exception: pass`, so every message vanished and three separate
    checks showed an empty log and exit code 0. A handler that swallows its own failure turns a
    broken function into a silent one.
    """
    G.log("hello from the grader")
    assert "hello from the grader" in capsys.readouterr().out


def test_log_does_not_call_itself():
    # PARSED, NOT GREPPED. Two earlier versions of this test failed on strings rather than code:
    # first the docstring that explains the recursion, then the literal "log() failed" inside the
    # last-resort handler. Searching source text for a call is the same mistake in miniature --
    # ast knows what a call is.
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(G.log)))
    fn = tree.body[0]
    recursive = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "log"]
    assert not recursive, (
        "log() calls itself; the RecursionError is then caught by its own handler and every "
        "message vanishes")


def test_log_flushes():
    # Without flush, stdout is block-buffered when redirected -- which is how this job is always
    # launched -- so a run of tens of minutes produced an EMPTY file until it exited, and
    # "working" and "hung" were indistinguishable for the whole run.
    import inspect
    assert 'flush=True' in inspect.getsource(G.log)


def test_no_handler_in_log_swallows_silently():
    # rindex('except Exception') finds whichever handler is last in the TEXT, not the outermost
    # one -- another property-by-substring guess. What matters is that NO handler here is a bare
    # pass, because that is exactly what turned a RecursionError into silence for three checks.
    import inspect
    lines = [ln.strip() for ln in inspect.getsource(G.log).splitlines()]
    for n, ln in enumerate(lines):
        if ln.startswith('except'):
            rest = [x for x in lines[n + 1:n + 4] if x and not x.startswith('#')]
            assert rest and rest[0] != 'pass', (
                'a handler in log() swallows silently (line %d)' % (n + 1))
