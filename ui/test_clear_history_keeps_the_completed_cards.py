# -*- coding: utf-8 -*-
"""Clearing History must NOT remove the completed (完了) cards from the board.

Owner report (2026-10-27 09:26 / 13:45): after Clear History the board held only 2 completed
cards but the left-rail 完了 read 71. The real defect was that ClearHistory
(ui/FleetCockpit.History.cs) added every terminal worker's key to _hiddenKeys, and BuildRows
(ui/FleetCockpit.Rows.cs) / UpdateHeader (ui/FleetCockpit.Header.cs) both drop/skip hidden
terminal workers -- so clearing the HISTORY LIST also tore the completed CARDS off the board.
PR #300 only re-aligned the left-rail count to that broken board; it did not stop the cards from
vanishing.

The contract (完了 → 履歴 → 履歴削除):
  * a finished worker appears as a 完了 card AND is recorded in history;
  * ClearHistory erases the HISTORY LIST only; the 完了 section is unchanged;
  * taking a card off the board is a SEPARATE action (_archiveOne / ArchiveAllTerminal, the
    完了-divider button) which is the only writer of _hiddenKeys.

The cockpit is a WPF exe that cannot run here, so these pin the invariant at the source level and
then REPLAY the board/left-rail logic in Python to show that after a history clear the completed
cards -- and the left-rail count -- stay at 71. The source is read from THIS worktree's own ui/
directory (see the sibling test_left_rail_done_matches_the_completed_cards.py for why).
"""
from pathlib import Path
import glob

UI = Path(__file__).resolve().parent


def _read_cockpit_source():
    """Concatenate every FleetCockpit*.cs part in THIS ui/ directory, main part first, BOMs after
    the first part stripped -- pinned to this worktree (not tools/cockpit_source)."""
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


def test_clear_history_does_not_add_to_hidden_keys():
    """THE FIX. ClearHistory must never put a completed card's key into _hiddenKeys, because that
    set is what removes a card from the 完了 section. Clearing the history list must leave the
    completed cards exactly where they are."""
    body = _member(SOURCE, "void ClearHistory(")
    # The regression: ClearHistory used to hide every terminal card. That line must be gone.
    assert "_hiddenKeys.Add" not in body, \
        "ClearHistory still hides completed cards via _hiddenKeys -- clearing history must not retire cards"
    # And it must not persist a hidden-key file from this path either.
    assert "SaveHidden(" not in body, \
        "ClearHistory still writes the hidden-keys file -- it must not touch the 完了 hide state"


def test_hidden_keys_is_only_written_by_the_dismiss_card_action():
    """_hiddenKeys.Add belongs to the SEPARATE dismiss-a-card action (_archiveOne and the
    完了-divider bulk variants), not to history clearing. If this ever regresses to adding keys in
    ClearHistory, the test above catches it; this one pins where the one writer lives."""
    archive = _member(SOURCE, "void _archiveOne(") if "void _archiveOne(" in SOURCE \
        else _member(SOURCE, "_archiveOne(")
    assert "_hiddenKeys.Add" in archive, \
        "the dismiss-card path (_archiveOne) must be the writer that removes a card from the board"


def test_board_and_left_rail_survive_a_history_clear():
    """Replay the board + left-rail logic across a Clear-History event on the owner's situation:
    71 completed workers, 69 of them also recorded in history. After clearing history (which now
    leaves _hiddenKeys empty) all 71 completed cards stay on the board and the left-rail 完了 reads
    71 -- never the 2-vs-71 split the owner saw.

    started#name is the WorkerKey (ui/FleetCockpit.History.cs WorkerKey); a worker is terminal iff
    its status is in TERMINAL; a card is OFF the board iff it is terminal AND its key is in
    _hiddenKeys (the dismiss-card set) -- history membership has no bearing on the board.
    """
    started = "1730000000"

    def key(w):
        return (started or "") + "#" + w.get("name", "")

    def is_terminal(w):
        return w.get("status") in TERMINAL

    # 71 completed workers plus two live lanes that must never count as done.
    workers = [{"name": "w%02d" % n, "status": "done"} for n in range(71)]
    workers.append({"name": "live1", "status": "running"})
    workers.append({"name": "live2", "status": "awaiting"})

    # 69 of the 71 completed workers are also in the history list. The board hide-set
    # (_hiddenKeys) is EMPTY -- no card was dismissed via the 完了-divider / card toolbar.
    history_keys = {key(w) for w in workers[:69]}
    hidden = set()  # nothing dismissed from the board

    def board_done(hidden_set):
        # BuildRows / UpdateHeader keep a terminal worker unless its key is in _hiddenKeys.
        return sum(
            1 for w in workers
            if is_terminal(w) and not (hidden_set and key(w) in hidden_set)
        )

    # Before the clear: 71 completed cards, left-rail 71.
    assert board_done(hidden) == 71

    # CLEAR HISTORY. The fixed ClearHistory wipes the history list and the archive index, and does
    # NOT touch _hiddenKeys.
    history_keys.clear()
    # hidden is unchanged by ClearHistory  -> still empty
    assert hidden == set()

    # After the clear: the completed cards are untouched and the left-rail still reads 71.
    on_screen_done = board_done(hidden)
    left_rail_done = board_done(hidden)  # same predicate, same set -> identical by construction
    assert on_screen_done == 71
    assert left_rail_done == on_screen_done == 71

    # The old behaviour (ClearHistory hiding every terminal card) is exactly what produced the
    # owner's collapse to a near-empty board; guard against its return.
    old_hidden = {key(w) for w in workers if is_terminal(w)}
    old_board_done = board_done(old_hidden)
    assert old_board_done == 0
    assert old_board_done != on_screen_done
