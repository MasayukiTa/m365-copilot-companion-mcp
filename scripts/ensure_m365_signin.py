"""Drive the M365 sign-in as part of setup, instead of telling the operator to run a command.

WHY THIS EXISTS. quickstart did nothing about sign-in: it finished, the doctor went red, and the
fix line told the person to run `start_companion_edge.ps1 -Foreground` themselves. That is not a
step that needs a human to TYPE anything -- surfacing the window, opening the right page, waiting
for the wall to clear and re-arming the keeper are all mechanical, and edge_recover already has
the pieces (surface(), touch_pause(); the latter's docstring is written for exactly this loop).
Leaving it as an instruction made setup look finished when it was not.

WHAT STILL NEEDS A PERSON, AND WHY IT IS NOT A GAP HERE. Measured on the machine that reported
this:

    AzureAdJoined : NO      AzureAdPrt : NO      DomainJoined : YES   WorkplaceJoined : YES

Silent Entra SSO works by exchanging a Primary Refresh Token held by the OS. With no PRT there is
nothing to exchange, so the browser must authenticate interactively -- password plus whatever
conditional access asks for. Automating that would mean storing and typing the account password,
which is both a credential-handling design nobody should ship and the thing that breaks the first
time MFA or a policy prompt changes. So the human factor stays; everything around it does not.

The result persists across restarts, so this runs once per machine.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Same pattern the doctor uses. ONE definition of "still on a login wall" -- two would drift,
#: and then setup and the health check would disagree about whether this step is done.
LOGIN_RE = re.compile(
    r"login\.microsoftonline|login\.live\.com|/adfs/|adfs\.|/oauth2/authorize|/signin|login_hint=",
    re.I)

#: An m365/copilot URL that is MID-AUTHENTICATION rather than the loaded app. Presence of an
#: m365 tab was being read as "signed in" on its own, but the account picker, an interrupted
#: consent, and the CsrToSSR bounce all live on an m365/copilot URL too -- so a tab that had
#: not finished authenticating counted as done. These fragments mark "not the app yet": the
#: bounce carries redirfrom=/auth=, the sign-in surfaces carry /login or ?login, and the
#: account chooser is /common/ or select_account. Matching any of them means we do NOT get to
#: claim signed-in from the mere existence of the tab; we return "cannot tell" and let the
#: caller wait or surface the window, which is safe either way.
MID_AUTH_RE = re.compile(
    r"redirfrom=|[?&]auth=|/login|[?&]login|select_account|/common/oauth2|prompt=",
    re.I)

SIGNED_IN_URL = "https://m365.cloud.microsoft/chat"


def tabs(port: int):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/json" % port, timeout=4) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def state(port: int):
    """(ready, reason).

    ready=None means "cannot tell" -- the browser is unreachable. That is a THIRD answer, not a
    kind of "not signed in": telling somebody to sign in when the browser is not running sends
    them to do something they cannot do.

    A LOGIN WALL is the only evidence of "not signed in". The absence of an m365 tab is not:
    the fleet is websocket-driven and opens no tabs, so a signed-in machine shows none. Deciding
    from that is what made this surface the window on a machine that was already signed in.
    """
    t = tabs(port)
    if t is None:
        return None, "companion Edge is not answering on :%d" % port
    urls = [x.get("url") or "" for x in t]
    if any(LOGIN_RE.search(u) for u in urls):
        return False, "a sign-in page is open"
    m365 = [u for u in urls if re.search(r"m365|copilot", u, re.I)]
    if m365:
        # AN M365 TAB IS NOT PROOF ON ITS OWN. If every m365 tab is still on an
        # authentication-in-progress URL (account picker, consent bounce, CsrToSSR), we have
        # not reached the app -- reporting "signed in" here is the false positive #2 is about.
        # Only a settled m365/copilot URL counts.
        if all(MID_AUTH_RE.search(u) for u in m365):
            return None, "an M365 tab is open but still mid-authentication, so nothing to confirm yet"
        return True, "an M365 page is open, past any sign-in wall and not mid-authentication"
    return None, "no M365 page open, so nothing to judge from (the fleet opens no tabs)"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--timeout", type=float, default=600.0,
                    help="how long to wait for the person to finish (default 10 minutes)")
    ap.add_argument("--check-only", action="store_true",
                    help="report and exit; never surface the window. For the health check, so "
                         "there is ONE definition of 'signed in' rather than two that drift.")
    a = ap.parse_args(argv)

    # READ THE TABS THAT EXIST; DO NOT MAKE ONE. A previous version opened a page through
    # /json/new to settle the tab-less case and left about:blank tabs behind in a browser the
    # fleet keeps at zero tabs -- the close was best-effort in a finally and does not survive an
    # interruption or a 404. "Nothing to judge from" is a real answer and is reported as one.
    ready, why = state(a.port)
    if ready:
        print("  [ OK ] M365 already signed in on the companion Edge (%s)" % why)
        return 0
    if a.check_only:
        # The health check asks a question; it does not take the window. 2 = could not tell,
        # which the caller must not report as "not signed in" -- that sends somebody to do
        # something they cannot do.
        print("  sign-in needed (%s)" % why if ready is False else "  cannot tell (%s)" % why)
        return 1 if ready is False else 2
    if ready is None and tabs(a.port) is None:
        # NOT a sign-in failure. Saying "sign in" when the browser is not running sends the
        # person to do something they cannot do.
        print("  the companion Edge is not running (%s)." % why)
        print("  start the stack first (start_all.bat), then run this again.")
        return 2
    if ready is None:
        # EDGE IS ANSWERING AND HAS NO M365 TAB -- which is what a fresh machine looks like,
        # because start_companion_edge.ps1 opens it at about:blank. This used to take the branch
        # above and report the browser as not running, so setup ended without ever offering a
        # sign-in, the wrapper turned the 2 into a 0, and doctor logged it as INFO. Opening the
        # page is the whole point of this path; if they are already signed in it costs a tab.
        print("  no M365 page is open yet, so there is nothing to judge from (%s)." % why)
    else:
        print("  M365 sign-in is needed (%s)." % why)
    print("  Opening the page and bringing the companion Edge window to the front...")
    try:
        from relay import edge_recover
    except Exception as exc:
        print("  could not load the window helper: %s" % exc)
        print("  run this yourself: powershell -File scripts\\start_companion_edge.ps1 -Foreground")
        return 2

    try:
        edge_recover.surface(port=a.port, open_url=SIGNED_IN_URL)
    except Exception as exc:
        print("  could not surface the window: %s" % exc)

    print("")
    print("  ==> Sign in with your work account in the window that just appeared.")
    print("      Complete any MFA prompt. This is the only part that needs you, and it is")
    print("      remembered afterwards -- you will not be asked again on this machine.")
    print("")

    deadline = time.time() + a.timeout
    last = ""
    while time.time() < deadline:
        # Keeps the background keeper backing off while the login page is up; its age check
        # expires after 180s, so a slow MFA login would otherwise be re-minimized mid-typing.
        try:
            edge_recover.touch_pause()
        except Exception:
            pass
        ready, why = state(a.port)
        if ready:
            print("\n  [ OK ] signed in. Continuing.")
            return 0
        left = int(deadline - time.time())
        msg = "  waiting for sign-in... (%s, %dm%02ds left)" % (why, left // 60, left % 60)
        if msg != last:
            sys.stdout.write("\r" + msg + " " * 8)
            sys.stdout.flush()
            last = msg
        time.sleep(2.0)

    print("\n  sign-in did not complete within %d minutes." % int(a.timeout / 60))
    print("  Nothing is broken -- run quickstart.bat again when you are ready to finish it,")
    print("  or bring the window up yourself with:")
    print("      powershell -File scripts\\start_companion_edge.ps1 -Foreground")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
