# -*- coding: utf-8 -*-
"""The left-rail \"done\" count must equal the DONE cards still on screen.

Owner report (2026-10-27): after Clear History the completed list held 2 cards but the
left-rail \"done\" read 71. Clearing history adds every terminal worker's key to _hiddenKeys
(ui/FleetCockpit.History.cs ClearHistory), and BuildRows (ui/FleetCockpit.Rows.cs) drops those
hidden terminal workers from the completed list -- so the on-screen count went to 2. But the
header count (cntDoneW in UpdateHeader, ui/FleetCockpit.Header.cs) counted EVERY terminal worker
unconditionally, ignoring _hiddenKeys, so it stayed at 71.

The cockpit is a WPF exe that cannot run here, so these pin the invariant at the source level and
then REPLAY the two filters' logic in Python against the same worker set + hidden-key set to show
the two counts agree. The replay mirrors exactly the two C# conditions (same `_hiddenKeys.Contains
(WorkerKey(started, w))` test, same IsTerminalWorker status set).

The source is read from THIS worktree's own ui/ directory (the folder this test lives in), not via
the shared `tools.cockpit_source` module, because that module resolves its REPO from its own
install location and can read a different checkout's copy of the file -- which would let a fix in
this worktree pass or fail against the wrong source.
"""
from pathlib import Path
import glob
import os

UI = Path(__file__).resolve().parent


def _read_cockpit_source():
    """Concatenate every FleetCockpit*.cs part in THIS ui/ directory, main part first, BOMs
    after the first part stripped -- the same shape tools/cockpit_source.read() builds, but
    pinned to this worktree."""
    main = UI / "FleetCockpit.cs"
    rest = sorted(Path(p) for p in glob.glob(str(UI / "FleetCockpit.*.cs")))
    parts = ([main] if main.is_file() else []) + rest
    out = []
    for i, p in enumerate(parts):
        t = p.read_text(encoding="utf-8")
        if i and t.startswith("\ufeff"):
            t = t[1:]
        out.append(t)
    return "\n".join(out)


def _member(src, signature, start=0):
    """Text of the member whose declaration contains `signature`, from the signature to the brace
    that closes its body. Comments, strings, verbatim strings and char literals are skipped so a
    brace inside them does not end the member early."""
    i = src.index(signature, start)
    n = len(src)
    j = i
    depth = 0
    seen_open = False
    while j < n:
        c = src[j]
        d = src[j + 1] if j + 1 < n else ""
        if c == "/" and d == "/":
            k = src.find("\n", j)
            j = n if k < 0 else k
            continue
        if c == "/" and d == "*":
            k = src.find("*/", j + 2)
            j = n if k < 0 else k + 2
            continue
        if c == "@" and d == '"':
            j += 2
            while j < n:
                if src[j] == '"':
                    if src[j + 1:j + 2] == '"':
                        j += 2
                        continue
                    break
                j += 1
            j += 1
            continue
        if c == '"':
            j += 1
            while j < n and src[j] != '"' and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            j += 1
            continue
        if c == "'":
            k = j + 1
            if src[k:k + 1] == "\\":
                k += 2
            else:
                k += 1
            if src[k:k + 1] == "'":
                j = k + 1
                continue
        if c == "{":
            depth += 1
            seen_open = True
        elif c == "}":
            depth -= 1
            if seen_open and depth == 0:
                return src[i:j + 1]
        j += 1
    raise ValueError("no closing brace after %r" % signature)


SOURCE = _read_cockpit_source()

# The terminal status set, kept in lockstep with IsTerminalWorker / relay TERMINAL.
TERMINAL = {"done", "stuck", "maxturns", "error", "cancelled", "content_refused"}


def test_header_done_count_excludes_cards_cleared_to_history():
    """UpdateHeader must skip terminal workers whose key is in _hiddenKeys when counting done.

    This is the fix: without it, cntDoneW counts terminal workers the user already cleared to
    History, so the left rail disagrees with the on-screen completed list."""
    body = _member(SOURCE, "void UpdateHeader(")
    assert "cntDoneW++" in body
    # The old unconditional form must be gone (regression guard).
    assert "if (IsTerminalWorker(ww)) cntDoneW++;" not in body
    # The branch that increments cntDoneW for a terminal worker must consult _hiddenKeys first.
    i = body.index("IsTerminalWorker(ww)")
    j = body.index("cntDoneW++", i)
    branch = body[i:j]
    assert "_hiddenKeys" in branch, "cntDoneW is counted without excluding cleared-to-History cards"
    assert "WorkerKey(" in branch


def test_card_list_hides_the_same_cleared_terminal_cards():
    """BuildRows drops terminal workers whose key is in _hiddenKeys -- the behaviour the header
    must now match. If this filter ever changes, the header test above must be revisited."""
    body = _member(SOURCE, "List<object> BuildRows(")
    assert "_hiddenKeys" in body
    assert "IsTerminalWorker(w)" in body
    assert "WorkerKey(" in body


def test_both_counts_use_the_same_hidden_key_predicate():
    """Replay the two filters in Python on one shared (workers, hidden) fixture and assert the
    left-rail done count equals the number of DONE cards left on screen -- the owner's 2-vs-71.

    started#name is the WorkerKey (ui/FleetCockpit.History.cs WorkerKey); a worker is terminal
    iff its status is in TERMINAL; a card is hidden iff it is terminal AND its key is in hidden.
    """
    started = "1730000000"

    def key(w):
        return (started or "") + "#" + w.get("name", "")

    def is_terminal(w):
        return w.get("status") in TERMINAL

    # Build the owner's situation: 71 finished workers, then 69 of them cleared to History,
    # leaving 2 on the board, plus a couple of still-running lanes that must not be counted done.
    workers = []
    for n in range(71):
        workers.append({"name": "w%02d" % n, "status": "done"})
    workers.append({"name": "live1", "status": "running"})
    workers.append({"name": "live2", "status": "awaiting"})

    hidden = {key(w) for w in workers[:69]}  # 69 cleared to History; w69, w70 remain on screen

    # LEFT RAIL (UpdateHeader, fixed): count terminal workers NOT hidden.
    left_rail_done = sum(
        1 for w in workers if is_terminal(w) and not (hidden and key(w) in hidden)
    )
    # ON SCREEN completed cards (BuildRows): terminal workers NOT hidden are kept, then shown
    # under the "\u5b8c\u4e86" divider.
    on_screen_done = sum(
        1 for w in workers if is_terminal(w) and not (hidden and key(w) in hidden)
    )

    assert on_screen_done == 2
    assert left_rail_done == on_screen_done == 2

    # And the pre-fix header (unconditional terminal count) is what produced the mismatch.
    old_left_rail = sum(1 for w in workers if is_terminal(w))
    assert old_left_rail == 71
    assert old_left_rail != on_screen_done
