"""The one instrument that watches the tool path could never report success.

`MCP_BRIDGE_RELEASE_PAGE=1` is the default and was chosen deliberately: a resident Copilot tab
costs about half a gigabyte. So PAGE is None whenever the bridge is idle. And the tool probe
only runs when the bridge is idle (TOOL_PROBE_MIN_IDLE_SEC). It looked for a page at exactly
the moment the design guarantees there is none, found None, and recorded
"PAGE not initialized; retrying".

The consequence was not a red dot. On 2026-08-31 the tool path was genuinely down for hours:
every fleet worker reported having no tools, two benchmark runs produced patches written from
memory, and this signal said "starting" throughout -- which is what it also says when
everything is fine. An instrument that cannot distinguish those is worse than none, because
people stop looking at the thing it was meant to replace.

Asserted against source rather than by running the bridge: this probe drives a real browser
through a page-owner thread, and standing one up in a unit test would test the harness. The
properties here are structural -- that a borrow happens, and that it is returned on every path.
"""
import ast
import io
import os

BRIDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "copilot_bridge.py")


def _probe_source():
    src = io.open(BRIDGE, encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_run_tool_probe":
            return ast.get_source_segment(src, node) or ""
    raise AssertionError("_run_tool_probe is gone")


def test_the_probe_borrows_a_page_rather_than_giving_up():
    src = _probe_source()
    assert "borrow_page" in src, (
        "the probe gives up when PAGE is None, which under the default release-page setting "
        "is every time it runs")


def test_the_borrowed_page_is_returned():
    src = _probe_source()
    assert "return_page" in src, "a borrowed page that is never returned leaks a tab per cycle"


def test_the_return_is_in_a_finally_so_no_exit_path_leaks():
    """Several paths leave this function -- os._exit for one class of failure, three different
    retry intervals, and a bare exception handler. A return on one of them is a leak on the
    others."""
    src = _probe_source()
    tree = ast.parse(src)
    fn = tree.body[0]
    finallies = [n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.finalbody]
    assert finallies, "the page is returned outside a finally, so some exit path leaks it"
    text = "\n".join(ast.dump(b) for f in finallies for b in f.finalbody)
    assert "return_page" in text, "the finally does not return the page"


def test_the_borrow_variable_is_bound_before_the_try():
    """The finally reads it on every path, including one that throws before the borrow."""
    src = _probe_source()
    body = src.split("try:", 1)[0]
    assert "_probe_borrowed = None" in body, (
        "the finally would raise NameError on an exception thrown before the borrow")


def test_the_borrow_runs_on_the_page_thread_and_cannot_hang():
    """borrow_page reaches Playwright, which has thread affinity, so the probe's timer thread
    may not call it directly.

    The comment this replaced explained why the original did not simply submit: an unbounded
    submit blocks the timer forever if the owner thread never services its queue. That is a
    real hazard and the reason the guard existed -- but submit_bounded fails closed instead of
    hanging, which answers it without giving up the probe.
    """
    src = _probe_source()
    assert "submit_bounded" in src, (
        "the borrow either runs on the wrong thread or can hang the probe timer")
    assert "borrow_page()" not in src, (
        "borrow_page is still called directly from the timer thread")


def test_the_borrow_and_the_return_both_sit_inside_the_page_lock():
    """A real turn must not be able to inherit the probe's page and then lose it.

    The borrow used to happen before PAGE_LOCK.acquire and the return after PAGE_LOCK.release,
    leaving a gap at each end. A user turn takes that lock, so it could begin in either gap,
    inherit the page and driver the probe had just created, and then have the probe's finally
    close the page out from under the conversation.
    """
    src = _probe_source()
    acq = src.index("PAGE_LOCK.acquire")
    rel = src.rindex("PAGE_LOCK.release")
    borrow = src.index("borrow_page")
    ret = src.index("return_page")
    assert acq < borrow, "the page is borrowed before the lock is held"
    assert ret < rel, "the page is returned after the lock is released"
