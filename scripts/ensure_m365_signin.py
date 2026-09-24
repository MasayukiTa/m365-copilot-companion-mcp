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

#: "An auth host", for LISTING leftovers, and the fallback wall test when relay/edge_auth cannot
#: be imported. The wall test itself is edge_auth.looks_like_signin_wall (see _is_wall): ONE
#: definition of "still on a login wall" -- two would drift, and then setup, the health check
#: and the thing that shows the window would disagree about whether this step is done.
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


def _is_residue(url: str) -> bool:
    """Is this auth-host tab a leftover rather than a page waiting for a person?

    THE ANSWER LIVES IN relay/edge_auth, NOT HERE. That module owns what an auth URL means,
    and relay/relay_fleet.py's residue reaper was written against the same two shapes. A copy
    here would be the second place to keep one fact -- the defect this repository spent
    2026-09-22 removing from four build scripts.

    IF IT CANNOT BE IMPORTED, NOTHING IS RESIDUE. That is the previous behaviour, which
    over-reports a sign-in rather than skipping one, and it fails toward the answer a human
    can check.
    """
    try:
        from relay import edge_auth
    except Exception:
        return False
    return edge_auth.looks_like_auth_bounce_residue(url)


def _is_wall(url: str) -> bool:
    """Is this tab an identity provider's sign-in page waiting for a person?

    THE ANSWER LIVES IN relay/edge_auth (looks_like_signin_wall), for the reason _is_residue
    gives. It matters more here than anywhere: the doctor reads this function, and the thing
    that brings the bridge's window forward (scripts/start_bridge.ps1) reads it too, through
    --check-only and --bridge-watch. When those two disagreed -- this file knew /adfs/, the
    surfacing side did not -- the doctor said "sign in" about a page nothing would show.

    If edge_auth cannot be imported, LOGIN_RE decides, which over-reports rather than skips.
    """
    try:
        from relay import edge_auth
    except Exception:
        return bool(LOGIN_RE.search(url or ""))
    return edge_auth.looks_like_signin_wall(url)


def _bare(url: str) -> str:
    """scheme://host/path -- the identifying part, without the query string.

    A sign-in URL's query carries `login_hint=` (an e-mail address) and state tokens, and this
    goes to a console and into screenshots.
    """
    try:
        p = urllib.parse.urlsplit(url)
    except Exception:
        return "(unparseable url)"
    if not p.scheme and not p.netloc:
        return "(unparseable url)"
    return "%s://%s%s" % (p.scheme, p.netloc, p.path)


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
        # NOT "companion Edge". This function is asked about :9223 (the bridge) and :9224 (the
        # evaluation browser) too, and naming the wrong browser in the reason sends the reader
        # to look at a process that is fine.
        return None, "no Edge is answering on :%d" % port
    urls = [x.get("url") or "" for x in t]
    walls = [u for u in urls if _is_wall(u) and not _is_residue(u)]
    if walls:
        # NAME THE TAB. "a sign-in page is open" is not enough to act on, and on 2026-09-23 it
        # was the whole of what the operator's screen said while the same FAIL kept coming back
        # on a machine this box cannot reproduce. Which URL it is settles what the person is
        # looking at: login.microsoftonline is a real wall, an /adfs/ bounce is a different
        # story, and a stale tab left over from an earlier attempt is a third.
        #
        # SCHEME, HOST AND PATH ONLY. The query string of a sign-in URL carries login_hint=
        # (the account's e-mail address) and assorted state tokens, and this line is printed on
        # a console and pasted into screenshots.
        return False, "a sign-in page is open: %s" % ", ".join(sorted({_bare(u) for u in walls}))
    m365 = [u for u in urls if re.search(r"m365|copilot", u, re.I)]
    if m365:
        # AN M365 TAB IS NOT PROOF ON ITS OWN. If every m365 tab is still on an
        # authentication-in-progress URL (account picker, consent bounce, CsrToSSR), we have
        # not reached the app -- reporting "signed in" here is the false positive #2 is about.
        # Only a settled m365/copilot URL counts.
        if all(MID_AUTH_RE.search(u) for u in m365):
            return None, "an M365 tab is open but still mid-authentication, so nothing to confirm yet"
        return True, "an M365 page is open, past any sign-in wall and not mid-authentication"
    # AUTH-HOST TABS THAT ARE ONLY RESIDUE. Reached when nothing above decided: no wall, no
    # M365 tab. Residue is not evidence of a wall and it is not evidence of a sign-in either,
    # so this is the third answer, said out loud -- otherwise the reason would be the tab-less
    # one and a reader would go looking for a browser with no tabs while two are open.
    residue = sorted({_bare(u) for u in urls if LOGIN_RE.search(u) or _is_residue(u)})
    if residue:
        return None, ("only auth-bounce leftovers are open, which say nothing either way: %s"
                      % ", ".join(residue))
    return None, "no M365 page open, so nothing to judge from (the fleet opens no tabs)"


def _report_every_profile(primary_port: int):
    """One `PROFILE:` line per managed Edge, because each one is a separate sign-in.

    THE OWNER ASKED THE RIGHT QUESTION, 2026-09-23: ":9222 と :9223 で建てるので、その片方に
    しか入れていない、ということはないか". Structurally, yes it can be. Edge locks a
    user-data-dir to one process, so the companion (:9222), the bridge (:9223) and the
    evaluation browser (:9224) MUST have distinct profiles to run at once -- and distinct
    profiles mean distinct cookie jars. Signing in on one signs in exactly one.

    Everything here checked :9222 and nothing else, so a bridge sitting on a login wall was
    invisible: the doctor's `Bridge Edge running (:9223)` row says the process answers CDP, not
    that it can reach Copilot.

    THE PORT LIST COMES FROM relay.edge_recover.MANAGED_EDGE_PROFILES, never from a copy here.
    That constant's own docstring records the same omission happening FOUR times -- "each time
    a profile was added and the places that enumerate profiles were not swept" -- and a literal
    list in this file would have been the fifth. If it cannot be imported, say so and check
    nothing, rather than checking a subset and reporting it as the whole.
    """
    try:
        from relay import edge_recover
        profiles = dict(edge_recover.MANAGED_EDGE_PROFILES)
    except Exception as exc:
        print("  PROFILE: (could not read the managed profile list: %s)" % exc)
        return
    for port in sorted(profiles):
        ready, why = state(port)
        verdict = "signed_in" if ready else ("sign_in_needed" if ready is False else "cannot_tell")
        print("  PROFILE: %d %s %s%s (%s)"
              % (port, profiles[port], verdict,
                 " [primary]" if port == primary_port else "", why))


# --------------------------------------------------------------------------------------------
# The bridge's browser: bring the sign-in in front of the person, once.
# --------------------------------------------------------------------------------------------
#
# WHAT WAS REPORTED, 2026-09-24. A freshly set-up PC: the doctor said the bridge Edge (:9223) was
# on a sign-in page, and the person could not sign in without typing a command, because that
# browser runs headless (--headless=new, parked at -32000,-32000) and nothing ever showed it.
# The owner: "バックグラウンドのタブがフォアグラウンドには出てこなかった。これバグでしょ。" It was.
#
# scripts/start_bridge.ps1's keepalive supervisor owns the bridge Edge's lifecycle -- it is the
# only thing that launches it headless or headed -- so it is the one that acts. This is the
# decision it asks for on every poll, kept here so that it is the SAME "is there a wall" the
# doctor reports (state() above) and so that it runs under pytest.

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Decisions --bridge-watch can print. Only "surface" makes the supervisor do anything.
BRIDGE_DECISIONS = ("surface", "already_surfaced", "deferred_turn_live",
                    "signed_in", "no_wall", "cannot_tell")


def _latch_path(port: int) -> str:
    """The "already brought forward for this sign-in" mark, one per browser port.
    MCP_SIGNIN_LATCH_DIR moves it (tests run the real CLI without writing the live .fleet)."""
    d = os.environ.get("MCP_SIGNIN_LATCH_DIR") or os.path.join(_REPO, ".fleet")
    return os.path.join(d, "signin_surfaced_%d.json" % int(port))


def _read_latch(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return float(json.load(f).get("t") or 0.0)
    except Exception:
        return None


def rearm(port: int, latch_path: str = None) -> bool:
    """Forget that the window was already shown, so the next wall shows it again."""
    p = latch_path or _latch_path(port)
    try:
        os.remove(p)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def _write_latch(path: str, now: float, why: str) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"t": now, "why": why}, f)
    except Exception:
        pass


def _last_start_all_began(repo: str = _REPO) -> float:
    """When the most recent start_all run BEGAN (epoch seconds), or 0.0 if unknown.

    WHY THIS RE-ARMS THE WINDOW. "Once per sign-in need" cannot mean "once per process": the
    person who was away when the window came up needs a way to see it again that is not a
    command. Starting the app again (the Desktop shortcut, or the logon start) is that way, and
    start_all records every run in .setup/logs/start_all_runs.jsonl. A run that began AFTER the
    window was last shown is a new request to be shown it. The run that launched this bridge
    began before the window was shown, so it does not re-fire it.

    ALSO CHECKS .setup/logs/start_all_runs.d/ (2026-09-25): a leaving copy (someone double-
    clicked the icon while this app was already up) no longer writes start_all_runs.jsonl
    directly -- see scripts/start_all.ps1's Write-StartAllRunRecordSpooled -- it writes its own
    file there, and only the NEXT holder's bring-up folds it into the jsonl, which can be
    minutes away. That leave already happened and is a real "started again" event; waiting for
    the eventual merge to notice it would mean the exact double-click this function exists to
    catch (a person clicking the icon because they don't see the window) stays invisible until
    long after the person gave up.
    """
    import datetime as _dt

    def _ts_of(text: str):
        text = text.strip()
        if not text.startswith("{"):
            return None
        try:
            ts = json.loads(text).get("ts") or ""
            return _dt.datetime.fromisoformat(ts).timestamp()
        except Exception:
            return None

    best = 0.0
    path = os.path.join(repo, ".setup", "logs", "start_all_runs.jsonl")
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 16384))
            tail = f.read().decode("utf-8", "replace").splitlines()
        for line in reversed(tail):
            t = _ts_of(line)
            if t is not None:
                best = t          # newest-last in an append-only file: first hit is newest
                break
    except Exception:
        pass

    spool_dir = os.path.join(repo, ".setup", "logs", "start_all_runs.d")
    try:
        names = os.listdir(spool_dir)
    except Exception:
        names = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(spool_dir, name), "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            continue
        t = _ts_of(text)
        if t is not None and t > best:
            best = t
    return best


def _bridge_busy(status_url: str) -> bool:
    """Is a chat turn live on the bridge? The SAME rule the restart gates use
    (scripts/stale_server_check._bridge_state: unreadable-but-present is busy), imported, not
    copied. Surfacing the window takes the focus; doing that mid-turn takes it from a person who
    is reading an answer."""
    if not status_url:
        return False
    try:
        import stale_server_check as _ssc
    except Exception:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import stale_server_check as _ssc
        except Exception:
            return True          # cannot ask -> treat as busy: deferring is the safe error
    try:
        return bool(_ssc._bridge_state(status_url)[1])
    except Exception:
        return True


def _bridge_saw_wall(status_url: str):
    """The bridge's own report (GET /status "signin_wall") that its page landed on a sign-in wall.

    NEEDED BECAUSE THE TAB DOES NOT STAY. The bridge closes its startup page once startup ends,
    so a wall it met at startup can be gone from the tab list by the time anybody looks --
    and a watcher that only reads tabs would see nothing. Returns (bare_url or "") or None.
    """
    if not status_url:
        return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(status_url, timeout=5) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(body, dict) or body.get("signin_wall") is not True:
        return None
    return str(body.get("signin_wall_url") or "")


def bridge_signin_decision(port: int, status_url: str = "", latch_path: str = None,
                           now: float = None, last_start_began: float = None):
    """(decision, why) for the bridge's browser on `port`. See BRIDGE_DECISIONS.

    * no wall            -> nothing to do. A confirmed sign-in also clears the latch.
    * wall, already shown for this need and nobody has started the app since -> nothing.
      ONCE PER NEED: a window that comes back every poll is one people learn to close.
    * wall, a turn live  -> deferred; asked again on the next poll.
    * wall               -> "surface", and the latch is set before returning, so a supervisor
      that dies half-way cannot turn this into a loop.
    """
    now = time.time() if now is None else now
    latch_path = latch_path or _latch_path(port)
    ready, why = state(port)
    if ready is True:
        rearm(port, latch_path)
        return "signed_in", why
    reported = _bridge_saw_wall(status_url) if ready is None else None
    if ready is None and reported is None:
        return ("no_wall" if tabs(port) is not None else "cannot_tell"), why
    if ready is not False:
        why = "the bridge reported its page landed on a sign-in page%s" % (
            (": " + reported) if reported else "")
    shown_at = _read_latch(latch_path)
    began = _last_start_all_began() if last_start_began is None else last_start_began
    if shown_at is not None and not (began > shown_at):
        return "already_surfaced", why
    if _bridge_busy(status_url):
        return "deferred_turn_live", why
    _write_latch(latch_path, now, why)
    return "surface", why


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--timeout", type=float, default=600.0,
                    help="how long to wait for the person to finish (default 10 minutes)")
    ap.add_argument("--check-only", action="store_true",
                    help="report and exit; never surface the window. For the health check, so "
                         "there is ONE definition of 'signed in' rather than two that drift.")
    ap.add_argument("--all", action="store_true",
                    help="also report EVERY managed Edge profile, not just --port. Each profile "
                         "is a separate browser with its own user-data-dir, so each has its own "
                         "sign-in state.")
    ap.add_argument("--bridge-watch", action="store_true",
                    help="for scripts/start_bridge.ps1: decide whether the bridge's browser on "
                         "--port must be brought forward for a sign-in now. Prints "
                         "'DECISION: <word>' and never touches a window itself.")
    ap.add_argument("--status-url", default="",
                    help="the bridge's GET /status, read to defer while a turn is live")
    ap.add_argument("--rearm", action="store_true",
                    help="forget that the window for --port was already shown (a new start)")
    a = ap.parse_args(argv)

    if a.rearm:
        rearm(a.port)
        print("REARMED: %d" % a.port)
        return 0
    if a.bridge_watch:
        decision, why = bridge_signin_decision(a.port, a.status_url)
        print("  %s" % why)
        print("DECISION: %s" % decision)
        return 0

    if a.all:
        _report_every_profile(a.port)

    # READ THE TABS THAT EXIST; DO NOT MAKE ONE. A previous version opened a page through
    # /json/new to settle the tab-less case and left about:blank tabs behind in a browser the
    # fleet keeps at zero tabs -- the close was best-effort in a finally and does not survive an
    # interruption or a 404. "Nothing to judge from" is a real answer and is reported as one.
    ready, why = state(a.port)
    if ready:
        print("  [ OK ] M365 already signed in on the companion Edge (%s)" % why)
        if a.check_only:
            print("VERDICT: signed_in")
        return 0
    if a.check_only:
        # The health check asks a question; it does not take the window. 2 = could not tell,
        # which the caller must not report as "not signed in" -- that sends somebody to do
        # something they cannot do.
        #
        # AND A VERDICT LINE, BECAUSE AN EXIT CODE CANNOT SAY "I CRASHED". 1 means "I looked
        # and there is a sign-in wall"; a traceback also exits 1, and scripts/doctor.ps1 maps
        # everything that is neither 0 nor 2 onto "M365 signed in: FAIL -- run quickstart.bat
        # again". So any unexpected exception here becomes a confident instruction to redo a
        # sign-in that may be perfectly fine. The reader takes this line when it is present and
        # falls back to "could not tell" when it is not, which is what a crash should read as.
        print("  sign-in needed (%s)" % why if ready is False else "  cannot tell (%s)" % why)
        print("VERDICT: %s" % ("sign_in_needed" if ready is False else "cannot_tell"))
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
        # THE REASON ALREADY SAYS THIS. `why` for the tab-less case is "no M365 page open, so
        # nothing to judge from (the fleet opens no tabs)", and wrapping it in a sentence that
        # says the same thing printed it twice, nested, on the operator's screen:
        #   "no M365 page is open yet, so there is nothing to judge from (no M365 page open,
        #    so nothing to judge from (the fleet opens no tabs))"
        # The parenthetical is for reasons that ADD something; this one is the whole sentence.
        print("  %s." % why)
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

    # EVERY surface() NEEDS A PAIRED REHIDE, and this one had none. relay/edge_auth.py states the
    # rule and names where it was learned: bridge/copilot_bridge.py's surface() was
    # fire-and-forget and left the window up. So was this one -- on success and on timeout alike
    # it returned straight out, leaving a headed companion Edge on screen and in the taskbar
    # until something else happened to hide it.
    #
    # That is what the operator sees as "an about:blank tab appears now and then": surface()
    # kills a headless instance and relaunches it HEADED, which starts on about:blank before it
    # navigates, and nothing put it back afterwards. It is rare because it fires only when the
    # check reports a real sign-in wall (start_all.ps1 escalates on exit 1 and nothing else), so
    # it will not reproduce on demand -- three hours of window-state sampling caught nothing.
    #
    # AFTER the wait, never during: hiding the window while somebody is typing an MFA code is
    # the other way to get this wrong.
    try:
        return _wait_for_signin(a, edge_recover, deadline_s=a.timeout)
    finally:
        try:
            edge_recover.rehide(port=a.port)
        except Exception:
            pass


def _wait_for_signin(a, edge_recover, deadline_s):
    """Poll until signed in, or until the deadline. Split out so the caller can guarantee the
    window is put back on EVERY exit path -- including the early `return 0` on success, which is
    the path that actually ran."""
    deadline = time.time() + deadline_s
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
        # THE REASON GOES ON ITS OWN LINE, ONCE; THE COUNTDOWN REDRAWS IN PLACE.
        #
        # `\r` returns to the start of the last PHYSICAL line, so a status line longer than the
        # console is not redrawn -- it wraps, and every tick leaves the previous copy behind.
        # With the reason now naming the tabs it found, the line went past 80 columns and the
        # operator's screen filled with identical half-truncated repeats of
        # "waiting for sign-in... (a sign-in page is open: https://login.live.com/Me.srf, ...".
        # Nothing was wrong with the content; it could not be read.
        if why != last:
            sys.stdout.write("\r" + " " * 78 + "\r")
            print("  %s" % why)
            last = why
        sys.stdout.write("\r  waiting for sign-in... (%dm%02ds left)   " % (left // 60, left % 60))
        sys.stdout.flush()
        time.sleep(2.0)

    print("\n  sign-in did not complete within %d minutes." % int(a.timeout / 60))
    print("  Nothing is broken -- run quickstart.bat again when you are ready to finish it,")
    print("  or bring the window up yourself with:")
    print("      powershell -File scripts\\start_companion_edge.ps1 -Foreground")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
