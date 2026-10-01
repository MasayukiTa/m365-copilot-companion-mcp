"""`interrupted` means the same thing in every place that classifies a status.

A run whose coordinator died is written by relay/fleet_reaper.py with status `interrupted` and
outcome `INTERRUPTED`. Read wrongly, it is a disaster in either direction: as TERMINAL it is
archived and counted finished (work lost); as retryable both the cockpit's per-worker requeue
and the coordinator's resume run the same goal twice; as unknown the cockpit paints it as
Stopped (the incident's misreading) and one such card blocks auto-archive of the whole run.

The Python side is checked by importing the sets; the C# side (WPF, not compilable in a unit
test) by reading the source for the exact lines, in the style of test_retry_sets_agree.py.
The mirrored retry list itself is pinned equal to relay/outcomes.py in that file; here we pin
that INTERRUPTED is on neither side.
"""
import io
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with io.open(os.path.join(REPO, rel), encoding="utf-8-sig") as fh:
        return fh.read()


def _strip_comments(src):
    return re.sub(r"//[^\n]*", "", src)


def test_python_vocabulary():
    from relay import fleet_reaper, outcomes, relay_fleet, fleet_runner
    assert outcomes.STATUS_OF["INTERRUPTED"] == "interrupted"
    assert "interrupted" not in relay_fleet.TERMINAL
    assert "interrupted" not in fleet_reaper.TERMINAL_STATUSES
    assert "INTERRUPTED" not in outcomes.RETRYABLE
    assert "INTERRUPTED" not in outcomes.FINISHED
    assert "INTERRUPTED" in outcomes.SCORING
    assert fleet_runner.STATUS_PILL["interrupted"] == ("中断", "warn")
    assert relay_fleet._PHASE_LABELS["interrupted"] == "Interrupted"
    assert (fleet_reaper.INTERRUPTED_STATUS, fleet_reaper.INTERRUPTED_OUTCOME) == (
        "interrupted", "INTERRUPTED")
    assert (fleet_reaper.INTERRUPTED_PILL, fleet_reaper.INTERRUPTED_COLOR) == ("中断", "warn")


def test_the_task_router_treats_it_as_not_yet_resolved():
    from relay import task_router
    assert "interrupted" not in task_router._WORKER_STATUS_TO_JOB_STATUS


def test_the_snapshot_dir_is_outside_every_retention_rule():
    from relay import fleet_reaper, fleet_retention
    d = fleet_reaper.INTERRUPTED_DIR
    assert d not in fleet_retention.STORE_DIRS
    assert not d.startswith("_")          # scratch() matches root files starting with `_`
    assert not d.startswith("coordinator_")


def test_cockpit_keeps_interrupted_out_of_terminal_and_retryable():
    src = _strip_comments(_read("ui/FleetCockpit.cs"))
    i = src.index("static bool IsTerminalWorker")
    body = src[i:src.index("}", i)]
    assert "interrupted" not in body, "interrupted must NOT be terminal (archived/finished)"
    j = src.index("static readonly string[] _retryableOutcomes")
    assert "INTERRUPTED" not in src[j:src.index(";", j)]
    assert "static bool IsInterruptedWorker" in src


def test_cockpit_labels_and_colour_for_interrupted():
    src = _read("ui/FleetCockpit.cs")
    assert re.search(r'if \(s == "interrupted"\) return ja \? "中断" : "Interrupted";', src)
    assert re.search(r'case "INTERRUPTED": outcomeEv = ja \? "中断" : "Interrupted"; '
                     r'outcomeKey = "interrupted"', src)
    assert 'case "INTERRUPTED": return' in src
    assert 'chip.ToolTip = reason' in src
    theme = _read("ui/Theme.cs")
    assert re.search(r'\{ "interrupted", "warning" \}', theme), "must not be muted like cancelled"
    assert re.search(r'case "interrupted": return jp \? "中断"\s+: "Interrupted";', theme)
    # the distinct-from-Stopped rule, both ways
    assert '"停止"' in theme and re.search(r'case "cancelled":\s+return jp \? "停止"', theme)


def test_cockpit_sites_that_count_or_block_know_about_interrupted():
    src = _strip_comments(_read("ui/FleetCockpit.cs"))
    # one interrupted card must not stop the finished ones being auto-archived
    i = src.index("void MaybeAutoArchive")
    assert "IsInterruptedWorker(w)) continue;" in src[i:i + 2500]
    # it is neither running nor active
    assert "else if (IsInterruptedWorker(ww)) cntIntr++;" in src
    assert "!IsTerminalWorker(dw) && !IsInterruptedWorker(dw)" in src
    assert "!IsTerminalWorker(w) && !IsInterruptedWorker(w) && st != \"pending\") cntActive++" in src
    # AutoRetryScan only looks at terminal workers, and retry is by outcome: both exclude it
    k = src.index("void AutoRetryScan")
    assert "if (!IsTerminalWorker(w)) continue;" in src[k:k + 600]


def test_cockpit_has_an_interrupted_filter_chip_and_its_own_counter():
    src = _strip_comments(_read("ui/FleetCockpit.cs"))
    assert 'if (IsInterruptedWorker(w)) intN++;' in src
    assert 'if (_cardFilter == 4 && !IsInterruptedWorker(w)) continue;' in src
    assert 'SegFilterButton(T("flt_intr") + " " + cntIntr, 4' in src
    assert 'if (k == "flt_intr")' in src
    # the interrupted tally is not the failure tally
    i = src.index('else if (oc == "STUCK" || oc == "ERROR"')
    assert "INTERRUPTED" not in src[i:i + 200]


def test_cockpit_shows_the_split_group_line_and_parent_display_label():
    src = _strip_comments(_read("ui/FleetCockpit.cs"))
    # the parent chip uses the derived label; real status/outcome stay untouched
    assert 'S(w, "display_label")' in src and 'S(w, "display_state")' in src
    assert 'parentLabel.Length > 0 ? parentLabel : Theme.StatusLabel(status, _lang)' in src
    # the one-line group summary: reads the `groups` the coordinator writes, plain wording
    assert '"groups"' in src and 'Obj(g, "ledger")' in src
    # the words live in GroupTreeView (WPF-free, compiled by its own harness test)
    view = _strip_comments(_read("ui/EffortPolicy.cs"))
    assert 'Obj(g, "children")' in view and 'GroupTreeView.Line(' in src
    assert '"分割グループ 子"' in view and '" / 統合: "' in view
    # never the long goal: the group line reads only the capped ledger keys, not a goal field
    j = src.index("UIElement BuildGroupLine")
    body = src[j:src.index("Border Card(Dictionary", j)]
    assert 'S(w, "goal")' not in body and 'S(g, "goal")' not in body
    assert 'S(led, "task")' in body and '"constraints"' in body and '"tokens"' in body
    # the derived strings the UI relies on exist on the Python side of the contract
    from relay import family_view
    for k in ("children_total", "merge_label", "display_label", "tokens"):
        assert k in family_view.__doc__
