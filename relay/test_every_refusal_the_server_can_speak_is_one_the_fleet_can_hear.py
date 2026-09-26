# -*- coding: utf-8 -*-
"""tools/security.py can speak three lock refusals. relay_fleet.py's comment said two.

WHAT HAPPENED. require_unlocked() started with two literal refusal strings: one for a caller
with no HTTP request context at all, one for a client IP that was never unlocked. relay_fleet.py
grew LOCKED_MARKERS and _looks_locked() to recognise both, and a comment above them said so in
as many words: "the server returns ONE of its two literal error strings".

On 2026-08-18, commit "Make the second key something a caller holds, not something it states"
added a THIRD refusal -- an identity whose IP is already unlocked but which presented no
per-call unlock_token. Nobody came back to relay_fleet.py. The comment kept saying "two" for
almost a month, and LOCKED_MARKERS only recognised the new refusal because its bracket prefix
("[locked: ") happened to collide with the no-context refusal's own prefix -- two unrelated
messages sharing a format, not a deliberate match. Nothing had verified that on purpose, and
nothing would have caught it if either message's wording had drifted apart to no longer share
that prefix.

A fleet worker (r6aa8e10b_a0_w0, 2026-09-15) hit exactly this shape: its IP was unlocked, its
per-call unlock_token was not, and the token-missing refusal that resulted did not read as short
raw tool output -- the worker wrapped it in paragraphs of Japanese analysis, well past
LOCKED_DOMINANCE_MAX_CHARS, which is _looks_locked()'s marker branch working exactly as
designed (see test_recognises_the_token_missing_refusal_verbatim below for the case that branch
DOES cover). The auto-unlock injection is only ever one layer of the fix that matters here.

WHAT THESE TESTS FIX IN PLACE.

1. NO_CONTEXT_REFUSAL and TOKEN_MISSING_REFUSAL are now named, in relay_fleet.py, rather than
   left to the "[locked:" prefix they happen to share. test_every_locked_literal_in_security_py
   below reads tools/security.py's ACTUAL SOURCE, finds every string literal that contains the
   bracketed "[locked" marker the server can emit, and asserts each one is covered by
   relay_fleet.LOCKED_MARKERS. A fourth refusal added to the server without a matching entry
   here now fails this test, by name, instead of silently going unrecognised in production.

2. test_recognises_the_token_missing_refusal_verbatim proves the positive: a short worker reply
   that is essentially the raw token-missing refusal is classified as locked, so the auto-unlock
   recovery in relay_fleet.RelayWorker._decide still fires for it.

3. test_still_rejects_long_prose_that_merely_discusses_unlock re-proves the negative from the
   2026-07-13 false-positive fix (see the comment above LOCKED_MARKERS in relay_fleet.py): a
   long analytical response that quotes/discusses unlock(password='<password>') at length must
   NOT be classified as locked. Adding a marker for the token-missing refusal must not reopen
   that hole -- the new marker still requires the same short-response dominance check as the
   others.
"""
from __future__ import annotations

import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import relay_fleet as RF  # noqa: E402

SECURITY_PY = os.path.join(REPO, "tools", "security.py")

#: Finds each double-quoted string literal (plain or f-string) in tools/security.py whose text
#: opens with the bracketed "[locked" marker. Concatenated adjacent literals (security.py joins
#: several per message) are captured piecewise -- only the FIRST segment of a message carries
#: "[locked", which is all that matters: the marker relay_fleet keys on must appear before any
#: f-string interpolation (a runtime IP, say) does, or it could never be a fixed string to key
#: on in the first place.
_LOCKED_LITERAL_RE = re.compile(r'f?"(\[locked[^"\n]*)"')


def _locked_literals_in_security_py() -> list[str]:
    with open(SECURITY_PY, encoding="utf-8") as f:
        src = f.read()
    return [m.group(1) for m in _LOCKED_LITERAL_RE.finditer(src)]


def _static_prefix(literal: str) -> str:
    """The part of a source literal that cannot vary at runtime: up to the first f-string
    interpolation (`{...}`), or the whole literal if it has none. A marker can only ever match
    reliably against this part -- text after a `{ip!r}` substitution is not fixed."""
    brace = literal.find("{")
    return literal if brace < 0 else literal[:brace]


def test_the_source_sweep_finds_all_known_refusal_variants():
    """Pins the sweep itself. Session-auth and token-fallback now have separate token-missing
    messages, so there are four source literals but still three refusal classes/prefixes."""
    literals = _locked_literals_in_security_py()
    assert len(literals) == 4, (
        "expected exactly 4 '[locked' literals in tools/security.py, found %d: %r -- "
        "either a refusal was removed (update this count) or the sweep regex needs fixing"
        % (len(literals), literals))
    assert any(lit.startswith("[locked: no HTTP request context") for lit in literals)
    assert any(lit.startswith("[locked: no valid unlock token") for lit in literals)
    assert any(lit.startswith("[locked client IP") for lit in literals)


def test_every_locked_literal_in_security_py_is_covered_by_a_relay_marker():
    """THE GUARD. Every '[locked' literal tools/security.py can actually emit must be matched
    by at least one entry in relay_fleet.LOCKED_MARKERS -- the only line of defence between a
    new server refusal and a fleet worker that gets refused, cannot recover, and reports a
    task blocked on authentication that the relay was supposed to walk it through.
    """
    literals = _locked_literals_in_security_py()
    assert literals, "no '[locked' literals found in tools/security.py -- sweep is broken"

    uncovered = []
    for literal in literals:
        prefix = _static_prefix(literal).lower()
        if not any(marker in prefix for marker in RF.LOCKED_MARKERS):
            uncovered.append(literal)

    if uncovered:
        raise AssertionError(
            "tools/security.py can emit a '[locked' refusal that relay_fleet.LOCKED_MARKERS "
            "does not cover, so the relay will not recognise it and cannot auto-unlock past "
            "it:\n  %s\n"
            "Add a marker for it (a named constant like NO_CONTEXT_REFUSAL / "
            "TOKEN_MISSING_REFUSAL, listed in relay/relay_fleet.py's LOCKED_MARKERS) -- match "
            "on the literal bracketed prefix the server actually emits, never on a loose "
            "phrase like 'unlock(password=', or a worker's own prose discussing the unlock() "
            "API will false-trip it (see the 2026-07-13 fix above LOCKED_MARKERS)."
            % "\n  ".join(repr(u) for u in uncovered)
        )


def test_named_constants_match_their_source_literals_verbatim():
    """NO_CONTEXT_REFUSAL and TOKEN_MISSING_REFUSAL are copies of tools/security.py's text
    (that module is frozen and cannot be imported from here). A copy is only as good as the
    literal staying identical, so pin both against the actual source rather than trusting them
    to have been transcribed correctly."""
    with open(SECURITY_PY, encoding="utf-8") as f:
        src = f.read()
    assert RF.NO_CONTEXT_REFUSAL in src
    assert RF.TOKEN_MISSING_REFUSAL in src


def test_recognises_the_token_missing_refusal_verbatim():
    """THE RECOVERY PATH. A worker reply that IS the token-missing refusal (short, the way an
    agent instructed to report facts tersely actually behaves) must be recognised as locked so
    RelayWorker._decide injects the same auto-unlock recovery as the other two refusals."""
    resp = (
        "[locked: no valid unlock token for '20.210.146.129'] The identity in the forwarding "
        "header is not sufficient on its own. Call unlock(password='<password>') and pass the "
        "returned `unlock_token` with the call. If you do not have the password, do not stop: "
        "hand the whole instruction to fleet_submit(goal=...), which needs no unlock."
    )
    assert len(resp) < RF.LOCKED_DOMINANCE_MAX_CHARS, (
        "fixture drifted from the real refusal's length; the test would stop meaning anything")
    assert RF._looks_locked(resp) is True

    # A worker paraphrasing the same refusal down to just the bracket (observed live in
    # r6aa8e10b_a0_w0's transcript: "[locked: no valid unlock token]", ip elided) must also be
    # recognised -- the marker is the bracket prefix, not the full sentence.
    assert RF._looks_locked("[locked: no valid unlock token]") is True


def test_still_rejects_long_prose_that_merely_discusses_unlock():
    """THE FALSE-POSITIVE GUARD MUST SURVIVE THE NEW MARKER. Proven live, 2026-07-13: a fleet
    security-review worker examining tools/security.py wrote a long response that quoted
    unlock(password='<password>') as example text, which false-matched the markers of the day
    and made the relay auto-unlock four times. Adding TOKEN_MISSING_REFUSAL must not reopen
    that hole -- dominance (a short response) is still required alongside the marker.
    """
    review = (
        "tools/security.py のセキュリティレビューを完了しました。require_unlocked() は "
        "呼び出し元の識別子がすでに解錠済みでも、per-call の unlock_token が伴わない場合は "
        "[locked: no valid unlock token for '203.0.113.7'] という文字列を返す設計です。"
        "これはレビュー対象のコードが返す実際の拒否メッセージの一例として引用したものであり、"
        "本会話でその呼び出しが実際に拒否されたわけではありません。実装は "
        "unlock(password='<password>') が呼ばれるとトークンを発行し、そのSHA-256のみを保存する "
        "ため、平文のトークンはどこにも残りません。IPごとの解錠状態は .unlock_state.json に "
        "保存され、MCP_REQUIRE_UNLOCK_TOKEN が有効な場合はトークンの一致も必須になります。"
        "全体として、この設計に重大なセキュリティ上の欠陥は見つかりませんでした。"
        "レビューは以上です。DONE"
    )
    assert len(review) >= RF.LOCKED_DOMINANCE_MAX_CHARS, (
        "fixture must be at or past the dominance ceiling for this test to mean anything")
    assert RF._looks_locked(review) is False

    # And the loose phrase alone, without the bracket, must never trip it regardless of length.
    passing_mention = (
        "unlock(password='<password>') is called once the identity is verified, and "
        "no valid unlock token can mean neither a fallback token nor the current MCP session satisfied the second factor."
    )
    assert RF._looks_locked(passing_mention) is False


if __name__ == "__main__":
    test_the_source_sweep_finds_all_known_refusal_variants()
    test_every_locked_literal_in_security_py_is_covered_by_a_relay_marker()
    test_named_constants_match_their_source_literals_verbatim()
    test_recognises_the_token_missing_refusal_verbatim()
    test_still_rejects_long_prose_that_merely_discusses_unlock()
    print("OK")
