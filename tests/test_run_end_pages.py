"""A finished run must not leave a Copilot page open.

Every worker closes its own page and the token capture closes its tab in a finally -- and a
run still ended with a bare /chat page sitting in the browser. It was there for the nine and
a half hours after the run finished, in 2219 of 4149 monitoring samples, and the profile
holding it stood at 757 MB against 278 MB for the one holding only a blank page.

The invariant is asserted at the end of the run instead of hunting every path that can leave
one. These fix the two ways asserting it could do harm: closing the browser by taking its
last page, and closing pages that are not ours to close.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "relay", "relay_fleet.py")


def _cleanup_src():
    src = open(SRC, encoding="utf-8").read()
    i = src.index("LEAVE NO COPILOT PAGE BEHIND")
    return src[i:i + 1600]


def test_only_copilot_pages_are_closed():
    """A blank keep-alive costs a few MB and holds the browser open; closing it is the bug."""
    body = _cleanup_src()
    assert 'm365.cloud.microsoft' in body
    assert "about:blank" in body, "a blank page must be created, never treated as stale"


def test_a_keepalive_is_opened_before_the_last_page_goes():
    """Edge exits with its final page. Taking the browser down as a tidiness measure would
    end the next run's SSO with it."""
    body = _cleanup_src()
    i = body.index("new_page")
    j = body.index("p.close()")
    assert i < j, "the replacement page must be opened before the stale ones are closed"


def test_a_failed_keepalive_cancels_the_close():
    """If the blank page cannot be opened, closing anyway would kill the browser."""
    body = _cleanup_src()
    assert re.search(r"except Exception:\s*\n\s*stale = \[\]", body), \
        "failing to create a keep-alive must abandon the cleanup, not proceed"


def test_the_cleanup_can_never_break_the_run():
    """This runs after the answers are in hand. A tidy-up that raises would discard them."""
    body = _cleanup_src()
    assert body.rstrip().endswith("pass") or "except Exception:" in body


def test_it_says_what_it_closed():
    """Reclaiming something a person may be watching in Task Manager has to be explainable."""
    assert "closed %d idle Copilot page(s)" in _cleanup_src()


# ---- and start clean too -------------------------------------------------------------------

def _runner_src():
    src = open(os.path.join(ROOT, "relay", "fleet_runner.py"), encoding="utf-8").read()
    i = src.index("def _close_idle_copilot_pages")
    return src[i:i + 1400]


class _Page:
    def __init__(self, url):
        self.url = url
        self.closed = False

    def close(self):
        self.closed = True

    def goto(self, url, timeout=None):
        self.url = url


class _Ctx:
    def __init__(self, urls):
        self.pages = [_Page(u) for u in urls]

    def new_page(self):
        p = _Page("about:blank")
        self.pages.append(p)
        return p


def _closer():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fleet_runner_close", os.path.join(ROOT, "relay", "fleet_runner.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._close_idle_copilot_pages


def test_a_page_left_by_an_earlier_run_is_closed_before_this_one_starts(monkeypatch):
    """The end-of-run cleanup stops a run LEAVING one. It does nothing about one already
    there -- and that gap cost a measurement: a page sat open for nine and a half hours,
    runs were launched on top of it, and every memory figure in that window carried it.
    Stratified afterwards: 341 MB median without a Copilot page, 697 MB with one.

    The unclaimed-set is stubbed because the real one asks the browser for target ids, and
    what this test is about is what happens to a page once it IS judged unowned."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fleet_runner_close", os.path.join(ROOT, "relay", "fleet_runner.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ctx = _Ctx(["https://m365.cloud.microsoft/chat", "about:blank"])
    monkeypatch.setattr(mod, "_unclaimed_pages", lambda pages: list(pages))
    assert mod._close_idle_copilot_pages(ctx) == 1
    assert ctx.pages[0].closed is True
    assert ctx.pages[1].closed is False, "the blank keep-alive must survive"


def test_a_clean_browser_is_left_alone():
    ctx = _Ctx(["about:blank"])
    assert _closer()(ctx) == 0
    assert ctx.pages[0].closed is False


def test_the_browser_is_never_left_with_no_pages(monkeypatch):
    """Edge exits with its final page, and that would end the next run's SSO."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fleet_runner_close2", os.path.join(ROOT, "relay", "fleet_runner.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ctx = _Ctx(["https://m365.cloud.microsoft/chat"])
    monkeypatch.setattr(mod, "_unclaimed_pages", lambda pages: list(pages))
    assert mod._close_idle_copilot_pages(ctx) == 1
    assert any(p.url == "about:blank" and not p.closed for p in ctx.pages)


def test_a_launch_reports_what_it_reclaimed():
    src = open(os.path.join(ROOT, "relay", "fleet_runner.py"), encoding="utf-8").read()
    assert "closed %d Copilot page(s) left by an earlier run" in src


# ---- ownership, not presence -----------------------------------------------------------------

def test_the_cleanup_asks_who_owns_a_page_rather_than_that_it_exists():
    """It used to close every Copilot page on the context while claiming they were unowned."""
    src = open(os.path.join(ROOT, "relay", "fleet_runner.py"), encoding="utf-8").read()
    i = src.index("def _close_idle_copilot_pages")
    body = src[i:i + 1800]
    assert "_unclaimed_pages(" in body, "the cleanup must reconcile, not sweep"


def test_a_page_that_cannot_be_identified_is_never_closed():
    """No target id means no way to know whose it is, and closing somebody else's working
    page is worse than leaving a stray one."""
    src = open(os.path.join(ROOT, "relay", "fleet_runner.py"), encoding="utf-8").read()
    i = src.index("def _unclaimed_pages")
    body = src[i:i + 1200]
    assert "continue" in body and "cannot identify" in body


def test_being_unsure_closes_nothing():
    src = open(os.path.join(ROOT, "relay", "fleet_runner.py"), encoding="utf-8").read()
    i = src.index("def _unclaimed_pages")
    body = src[i:i + 1400]
    assert "return []" in body.split("except Exception:")[-1]


def test_a_claimed_page_survives_the_cleanup(monkeypatch):
    """The new contract, stated positively: a page a live run owns is not touched."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fleet_runner_close3", os.path.join(ROOT, "relay", "fleet_runner.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ctx = _Ctx(["https://m365.cloud.microsoft/chat", "about:blank"])
    monkeypatch.setattr(mod, "_unclaimed_pages", lambda pages: [])
    assert mod._close_idle_copilot_pages(ctx) == 0
    assert all(not p.closed for p in ctx.pages)
