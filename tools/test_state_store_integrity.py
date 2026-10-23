"""Integrity of the whole-file stores (the 2026-10-18 loss: .procedural_memory.json 725 -> 90).

Covers what tools/state_store.py promises beyond test_state_store.py: an unreadable file is
kept as `<name>.corrupt-<timestamp>`, a refused shrink is logged loudly and leaves the
previous version in place, and every sibling store that holds accumulated data (the
`.fleet/memory` theme notes, the refuter memory, the approved-job classes) goes through the
same locked, atomic, never-empty-on-failure path. The last test is the inventory of every
writer of a whole JSON document in the repository: a new writer fails it until somebody
classifies it as protected, audited or open.

Hermetic: everything lives under tmp_path. Run: pytest -q tools/test_state_store_integrity.py
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path

import pytest

from tools import childproc
from tools import state_store as ss

REPO = Path(__file__).resolve().parent.parent
TORN = b"{ \"procedures\": { \"a\": \xff\xfe torn"


def _valid(d):
    return isinstance(d, dict) and isinstance(d.get("e"), dict)


def _upd(path, mutate, **kw):
    return ss.update_json(path, mutate, validate=_valid, count=lambda d: len(d["e"]),
                          default=lambda: {"e": {}}, **kw)


def _corrupt_copies(path):
    return sorted(p for p in path.parent.iterdir() if p.name.startswith(path.name + ".corrupt-"))


# --- state_store --------------------------------------------------------------------------

def test_unreadable_main_is_kept_as_a_corrupt_copy_before_a_backup_replaces_it(tmp_path):
    p = tmp_path / "s.json"
    _upd(p, lambda s: s["e"].update({"a": 1}))
    _upd(p, lambda s: s["e"].update({"b": 2}))
    p.write_bytes(TORN)
    assert ss.read_json(p, validate=_valid, default=lambda: {"e": {}}) == {"e": {"a": 1, "b": 2}}
    copies = _corrupt_copies(p)
    assert len(copies) == 1 and copies[0].read_bytes() == TORN
    # the next save repairs the main file; the damaged bytes survive in the copy
    _upd(p, lambda s: s["e"].update({"c": 3}))
    assert json.loads(p.read_text(encoding="utf-8"))["e"] == {"a": 1, "b": 2, "c": 3}
    assert _corrupt_copies(p)[0].read_bytes() == TORN
    assert len(_corrupt_copies(p)) == 1          # same bytes are not copied again


def test_unreadable_without_backup_keeps_a_corrupt_copy_and_raises(tmp_path):
    p = tmp_path / "s.json"
    p.write_bytes(TORN)
    with pytest.raises(ss.StateUnreadable):
        _upd(p, lambda s: s["e"].update({"a": 1}))
    assert p.read_bytes() == TORN
    assert _corrupt_copies(p)[0].read_bytes() == TORN


def test_read_text_missing_is_none_and_unreadable_is_never_empty(tmp_path):
    p = tmp_path / "t.md"
    assert ss.read_text(p) is None
    p.write_bytes(b"\xff\xfe\x00 not utf-8 \xff")
    with pytest.raises(ss.StateUnreadable):
        ss.read_text(p)
    assert _corrupt_copies(p)


def test_read_text_falls_back_to_the_newest_backup(tmp_path):
    p = tmp_path / "t.md"
    ss.atomic_write_text(p, "first\n", generations=3)
    ss.atomic_write_text(p, "second\n", generations=3)
    p.write_bytes(b"\xff\xff\xff")
    assert ss.read_text(p, generations=3) == "second\n"
    assert _corrupt_copies(p)


def test_the_725_to_90_case_is_refused_loudly_and_the_previous_version_stays(tmp_path, caplog):
    p = tmp_path / "procedural.json"
    _upd(p, lambda s: s["e"].update({f"k{i}": i for i in range(725)}))
    before = p.read_bytes()
    with caplog.at_level(logging.ERROR, logger="state_store"):
        with pytest.raises(ss.StateShrinkRefused):
            _upd(p, lambda s: [s["e"].pop(f"k{i}") for i in range(635)])
    assert p.read_bytes() == before
    assert any("REFUSED" in r.getMessage() and "725 -> 90" in r.getMessage() for r in caplog.records)
    # an explicit delete is the one way to shrink, and it still keeps the older version
    _upd(p, lambda s: [s["e"].pop(f"k{i}") for i in range(635)], allow_shrink=True)
    assert len(json.loads(p.read_text(encoding="utf-8"))["e"]) == 90
    assert len(json.loads((tmp_path / "procedural.json.bak.2").read_text(encoding="utf-8"))["e"]) == 725


# --- .fleet/memory theme notes (relay/project_memory.py) ----------------------------------

def _theme_file(state_dir, theme):
    from relay import project_memory as pm
    return Path(pm._theme_path(pm.slugify(theme), str(state_dir)))


def test_an_unreadable_theme_file_is_not_replaced_by_a_one_entry_file(tmp_path):
    from relay import project_memory as pm
    sd = str(tmp_path)
    assert pm.record_task("T", "goal one", "DONE", note="n1", state_dir=sd, ts=1)
    path = _theme_file(tmp_path, "T")
    for b in path.parent.glob(path.name + ".bak.*"):
        b.unlink()                                  # no generation to fall back to
    path.write_bytes(b"\xff\xfe damaged \xff")
    assert pm.record_task("T", "goal two", "DONE", note="n2", state_dir=sd, ts=2) is False
    assert path.read_bytes() == b"\xff\xfe damaged \xff"
    assert _corrupt_copies(path)


def test_an_unreadable_theme_file_is_restored_from_its_generation_on_the_next_record(tmp_path):
    from relay import project_memory as pm
    sd = str(tmp_path)
    for i in range(3):
        assert pm.record_task("T", f"goal {i}", "DONE", note=f"n{i}", state_dir=sd, ts=i + 1)
    path = _theme_file(tmp_path, "T")
    path.write_bytes(b"\xff\xfe damaged \xff")
    assert pm.record_task("T", "goal 3", "DONE", note="n3", state_dir=sd, ts=9)
    text = path.read_text(encoding="utf-8")
    assert all(f"n{i}" in text for i in range(4))
    assert _corrupt_copies(path)


def test_concurrent_records_on_one_theme_lose_nothing(tmp_path):
    from relay import project_memory as pm
    sd = str(tmp_path)
    errs = []

    def work(i):
        try:
            assert pm.record_task("T", f"goal {i}", "DONE", note=f"note-{i}", state_dir=sd, ts=i + 1)
        except BaseException as e:           # pragma: no cover
            errs.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errs
    text = _theme_file(tmp_path, "T").read_text(encoding="utf-8")
    assert all(f"note-{i}" in text for i in range(8))
    assert not [n for n in os.listdir(tmp_path / "memory") if n.endswith((".tmp", ".lock"))]


# --- refuter memory ------------------------------------------------------------------------

_FEAT = {"domain": "coding", "size": "s", "files": "one", "has_repro": "y", "claims_minimal": "n"}


def test_refuter_memory_never_writes_over_an_unreadable_store(tmp_path):
    from relay.refuter_memory import RefuterMemory
    p = tmp_path / "refuter_memory.json"
    p.write_bytes(b"{ torn")
    m = RefuterMemory(str(p))
    m.record(_FEAT, "edge", True)
    m.select_lenses(_FEAT, ["correctness", "edge", "security"], 2)
    assert p.read_bytes() == b"{ torn"
    assert _corrupt_copies(p)


def test_refuter_memory_keeps_another_process_observations(tmp_path):
    from relay.refuter_memory import RefuterMemory
    p = str(tmp_path / "refuter_memory.json")
    a, b = RefuterMemory(p), RefuterMemory(p)       # both loaded the same (empty) state
    a.record(_FEAT, "edge", True)
    b.record(_FEAT, "edge", False)
    c = RefuterMemory(p)
    (cell,) = c.data["cells"].values()
    assert cell == {"refute": 1, "total": 2}


# --- approved job classes (relay/task_router.py) -------------------------------------------

def test_approved_jobs_keep_every_approval_and_never_overwrite_an_unreadable_store(tmp_path, monkeypatch):
    import relay.task_router as tr
    path = tmp_path / "approved_jobs.json"
    monkeypatch.setattr(tr, "APPROVED_JOBS_FILE", str(path))
    tr._approve_class("shell::git status", example="git status")
    tr._approve_class("shell::ls", example="ls")
    assert tr._is_class_approved("shell::git status") and tr._is_class_approved("shell::ls")
    for b in tmp_path.glob("approved_jobs.json.bak.*"):
        b.unlink()
    path.write_bytes(b"{ torn")
    tr._approve_class("shell::cat", example="cat")
    assert path.read_bytes() == b"{ torn"
    assert not tr._is_class_approved("shell::cat")  # not persisted, so the human is asked again
    assert _corrupt_copies(path)


# --- inventory of every whole-document JSON writer -----------------------------------------
#
# Pattern: `json.dump(` or `write_text(json.dumps(` -- a writer of one complete JSON
# document. (JSONL appends write one line and cannot truncate history, so they are not
# the pattern.) Every file with a hit must be classified below. A new writer fails this test
# until someone decides whether it holds irreplaceable data.

# Files that hold accumulated data and now use tools/state_store (locked, atomic, generation
# backups, never empty on a failed read). They must reference state_store.
PROTECTED = {
    "relay/task_router.py": "approved_jobs.json via state_store.update_json; the other json.dump "
                            "sites in this file are per-job records (see AUDITED)",
}
# Writers of regenerable or per-run documents; losing one costs a re-run, not knowledge.
AUDITED = {
    "relay/capture_status.py": "latest capture status snapshot, overwritten every time",
    "relay/consumer_watch.py": "alarm marker file",
    "relay/fleet_reaper.py": "status/history snapshots finalised atomically after an interrupted run",
    "relay/fleet_runner.py": "per-run status/goal snapshots",
    "relay/local_loop_controller.py": "controller marker, atomic tmp+replace, one file per job",
    "relay/org/def_store.py": "override map and per-version text, each file replaced whole by an operator command through temp file + os.replace; nothing accumulates",
    "relay/profile_token.py": "captured token template, re-captured on demand",
    "relay/upload_template.py": "cached shape of the page's UploadFile request, re-captured on demand (fallback path only)",
    "relay/resident_guard.py": "single-use lock file holding the owner pid, created with O_EXCL and removed on release",
    "relay/selfimprove/apply.py": "genome file written from a ledger row",
    "relay/selfimprove/branches.py": "at most 5 branch refs, recreated from the archive by genome_id",
    "relay/selfimprove/dashboard.py": "derived dashboard state",
    "relay/selfimprove/frozen.py": "frozen-manifest baseline (guarded elsewhere)",
    "relay/selfimprove/loop.py": "run report",
    "relay/selfimprove/quality_loop.py": "run report",
    "relay/selfimprove/record_summary.py": "cache derived from the ledger",
    "relay/selfimprove/runtime_config.py": "manifest derived from the genome",
    "relay/selfimprove/scheduler.py": "iteration lock / candidate manifest, per run",
    "bridge/copilot_bridge.py": "see OPEN (fleet conversations list)",
    "tools/auth_stats.py": "derived counters snapshot, atomic tmp+replace",
    "tools/contract_gate.py": "gate files, atomic, regenerated per decision",
    "tools/data_ops.py": "write_json tool: the caller supplies the whole document",
    "tools/evalbench/fairness.py": "report output",
    "tools/fleet_intake.py": "one job file per submission",
    "tools/lock_state.py": "lock/refusal state, rewritten whole by design",
    "tools/rebuild_history.py": "rebuilt from source records",
    "tools/screen_ops.py": "one frame file per capture",
    "tools/security.py": "frozen file",
    "tools/tool_probe.py": "probe reports",
    "scripts/bootstrap.py": "install state, re-derived by the next run",
    "scripts/update_from_release.py": "uses state_store already; release metadata",
    "scripts/win/lean_capture_trial.py": "trial output",
}
AUDITED_PREFIXES = {
    "bench/": "benchmark inputs/outputs regenerated per run",
    "scripts/": "one-shot reports and experiment outputs",
    "relay/testdata/": "test data capture tool",
}
# Known to follow the unsafe pattern and NOT fixed here; each is reported with file:line in
# the PR and tracked in TASKS.md. They hold small, re-derivable or low-value data.
OPEN = {
    "scripts/win/capture_budget.py": "acknowledgement records: read failure -> {} then whole-file write",
    "tools/task_ops.py": "session todo list: read failure -> empty list then whole-file write",
    "tools/gate_ops.py": "one gate file per token: unlocked read-modify-write",
    "relay/fleet_retention.py": "conversation registry: atomic + a single .bak, no lock, no generations",
}
_PATTERN = re.compile(r"json\.dump\(|write_text\(\s*json\.dumps")


def _tracked_py():
    out = childproc.run(["git", "ls-files", "*.py"], cwd=REPO,
                        check=True).stdout.split()
    return [f for f in out
            if not re.search(r"(^|/)test_|^tests/|conftest|^thirdparty/", f)]


def _writers():
    found = {}
    for f in _tracked_py():
        for n, line in enumerate((REPO / f).read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if _PATTERN.search(line):
                found.setdefault(f, []).append(n)
    return found


def test_every_whole_document_json_writer_is_classified():
    found = _writers()
    classified = set(PROTECTED) | set(AUDITED) | set(OPEN)
    unclassified = [f for f in found
                    if f not in classified and not any(f.startswith(p) for p in AUDITED_PREFIXES)]
    assert not unclassified, (
        "new whole-document JSON writer(s) %s: if they hold accumulated data use "
        "tools.state_store.update_json; otherwise add them to AUDITED with a reason"
        % {f: found[f] for f in unclassified})
    for f in PROTECTED:
        assert "state_store" in (REPO / f).read_text(encoding="utf-8"), f


def test_the_memory_stores_have_no_direct_whole_file_json_write():
    """The stores that carry the agent's accumulated knowledge never write a document directly."""
    for f in ("tools/procedural_memory.py", "tools/memory_ops.py", "tools/agent_memory_ops.py",
              "tools/data_aliases.py", "relay/refuter_memory.py", "relay/project_memory.py"):
        text = (REPO / f).read_text(encoding="utf-8")
        assert "state_store" in text, f
        assert not _PATTERN.search(text), f
