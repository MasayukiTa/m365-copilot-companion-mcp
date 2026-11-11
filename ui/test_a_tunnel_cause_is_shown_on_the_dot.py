# -*- coding: utf-8 -*-
"""The tunnel dot must NAME which cause took the public path down, not just go red.

MEASURED 2026-11-11: the public URL was unhosted for 63 minutes and nobody was told. One of the
four causes found was a devtunnel login that had expired; another was that detection had no
always-on view of the public path at all. The fix on the probe side writes an explicit cause
into .fleet/tunnel_host.json; this test fixes the RECEIVING side -- the cockpit's tunnel dot --
so that when the probe names a cause, the operator sees it on the strip, in red, with the file's
own plain message and action, within one cockpit tick.

The dot already reddened for the cross-PC verdicts (foreign/shared) read from the same file.
This test pins that the SAME surface now also reddens for the four public-path-down causes
(not_logged_in / no_host / local_down / public_down) and -- just as importantly -- that it does
NOT redden for the states the supervisor re-hosts by itself (none/stale) or the healthy one
(ours), which must keep falling through to the live probe exactly as before.

The assertions are made against the cockpit's C# source (read through tools.cockpit_source so
every split part is covered). They are structural -- "the source routes this state to red with
the file's text" -- because the WPF UI is not built in the hermetic Linux CI.
"""
from tools import cockpit_source

SRC = cockpit_source.read(encoding="utf-8-sig")

# The four public-path-down causes the always-on probe writes so the dot can say WHICH one.
CAUSE_STATES = ("not_logged_in", "no_host", "local_down", "public_down")
# States the supervisor fixes on its own (or the healthy one): these must NOT redden here.
SELF_HEALED_OR_OK = ("none", "stale", "ours")


def _down_set_block():
    """The initialiser body of the closed allow-list of 'reported down' states."""
    i = SRC.index("_tunnelDownStates")
    j = SRC.index("}", SRC.index("{", i))
    return SRC[i:j]


def test_the_four_causes_are_in_the_reported_down_set():
    """Each cause the probe can name must map to red on the dot. A cause written to the file but
    missing from this set would leave the dot falling through to the generic probe, which is the
    silent 63-minute gap this work exists to close."""
    block = _down_set_block()
    for cause in CAUSE_STATES:
        assert '"%s"' % cause in block, (
            "%s is not in _tunnelDownStates; the dot would not redden when the probe names it" % cause)


def test_the_cross_pc_verdicts_stay_in_the_set():
    """foreign/shared reddened before this change and must keep doing so -- the set is the one
    place the condition is now decided."""
    block = _down_set_block()
    assert '"foreign"' in block
    assert '"shared"' in block


def test_self_healed_and_healthy_states_are_not_reddened():
    """none/stale are re-hosted by the supervisor and ours is healthy; reddening any of them
    would point the operator at an outage the machine is already handling. They must NOT be in
    the down set -- they fall through to the live probe, as before."""
    block = _down_set_block()
    for state in SELF_HEALED_OR_OK:
        assert '"%s"' % state not in block, (
            "%s is in _tunnelDownStates; a self-healed/healthy state must not redden the dot" % state)


def test_the_dot_branch_uses_the_set_and_goes_red_with_the_files_text():
    """The branch that colours the tunnel dot (dot index 1) from the file must: gate on the set,
    show the file's own message, append its action, fall back to a non-blank string when the
    file carried no text, and set the state to Red."""
    i = SRC.index("TunnelStateIsReportedDown(tunHost.State)")
    # The whole else-if block body up to the next top-level branch.
    block = SRC[i:SRC.index("string tunnel = EnvValue(", i)]
    assert "string detail = tunHost.Message;" in block
    assert "tunHost.Action" in block and "detail = detail" in block, "action is not appended"
    assert 'T("hs_tun_detail_bad")' in block, "no non-blank fallback when the file has no text"
    assert "SetDot(1, HealthState.Red, detail, now)" in block, "the dot is not reddened from the file"


def test_the_match_is_case_insensitive():
    """A producer that writes 'No_Host' or 'PUBLIC_DOWN' must still be recognised; a case-
    sensitive compare would silently drop the alert."""
    i = SRC.index("_tunnelDownStates = new HashSet<string>")
    decl = SRC[i:SRC.index("\n", i)]
    assert "StringComparer.OrdinalIgnoreCase" in decl


def test_the_helper_guards_an_empty_state():
    """An empty/missing state must not be treated as 'down' (ReadTunnelHostState returns null on a
    missing file, but a present file with an empty state string must fall through, not redden)."""
    body = cockpit_source.member(SRC, "static bool TunnelStateIsReportedDown(")
    assert "IsNullOrEmpty(state)" in body
    assert "_tunnelDownStates.Contains(state)" in body
