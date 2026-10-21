# -*- coding: utf-8 -*-
"""Rollback of the promoted definition TEXT (slice D6-b).

Pins: a round trip through fresh processes restores the earlier text and the genotype; a second
rollback swaps back; a missing stored text refuses and writes nothing; the store is read once at
import; writes are atomic; any part but the planner prompt is refused; and no frozen file, no
frozen manifest entry and no scaffold allowlist entry changed.
"""
from __future__ import annotations

import importlib
import io
import json
import os
import re
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.org import def_store as S  # noqa: E402
from relay.org import definitions as D  # noqa: E402
from relay.org import promotion as P  # noqa: E402

LABEL = "relay.planner.PLAN_PROMPT"
SCRIPT = os.path.join(REPO, "scripts", "org_promotion.py")


def _op():
    spec = importlib.util.spec_from_file_location("org_promotion_script", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _snap(r, w="w"):
    return {"roles_hash": r, "workflows_hash": w, "roles": {}, "workflows": {}}


def _bytes(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _two_versions(op, fleet):
    """A and B promoted with distinct stored text; returns their genotype ids."""
    op.promote_with_text(fleet, _snap("ra"), parts={LABEL: "text A"}, now=1.0)
    op.promote_with_text(fleet, _snap("rb"), parts={LABEL: "text B"}, now=2.0)
    return D.genotype_id(_snap("ra")), D.genotype_id(_snap("rb"))


def test_rollback_restores_text_and_a_second_rollback_swaps_back(tmp_path):
    op = _op()
    ga, gb = _two_versions(op, tmp_path)
    defs = op.defs_dir(tmp_path)
    S.write_overrides({LABEL: "text B"}, defs)
    res = op.rollback_text(tmp_path, "worse", now=3.0)
    assert res["status"] == "recorded" and res["to"] == ga and res["part"] == LABEL
    assert S.read_overrides(defs) == {LABEL: "text A"}
    assert P.status(P.ledger_path(tmp_path))["current"] == ga
    res = op.rollback_text(tmp_path, "undo", now=4.0)
    assert res["status"] == "recorded" and res["to"] == gb
    assert S.read_overrides(defs) == {LABEL: "text B"}


def test_promote_stores_this_and_the_previous_text(tmp_path):
    op = _op()
    ga, gb = _two_versions(op, tmp_path)
    obj = S.load_version(gb, op.defs_dir(tmp_path))
    assert obj["parts"] == {LABEL: "text B"}
    assert obj["previous"] == {"genotype_id": ga, "parts": {LABEL: "text A"}}
    assert S.load_version(ga, op.defs_dir(tmp_path))["previous"]["genotype_id"] == ""


def test_missing_stored_text_refuses_and_writes_nothing(tmp_path):
    op = _op()
    ga, gb = _two_versions(op, tmp_path)
    defs = op.defs_dir(tmp_path)
    S.write_overrides({LABEL: "text B"}, defs)
    os.unlink(os.path.join(defs, ga + ".json"))
    ledger = P.ledger_path(tmp_path)
    before = (_bytes(ledger), _bytes(os.path.join(defs, S.OVERRIDES)))
    res = op.rollback_text(tmp_path, "worse")
    assert res["status"] == "refused" and "no stored text" in res["reason"]
    assert (_bytes(ledger), _bytes(os.path.join(defs, S.OVERRIDES))) == before


def test_other_refusals_write_nothing(tmp_path):
    op = _op()
    ga, gb = _two_versions(op, tmp_path)
    ledger = P.ledger_path(tmp_path)
    before = _bytes(ledger)
    assert op.rollback_text(tmp_path, "")["status"] == "refused"
    assert op.rollback_text(tmp_path, "x", to=gb)["status"] == "refused"      # already current
    assert op.rollback_text(tmp_path, "x", to="nope")["status"] == "refused"  # not in ledger
    assert _bytes(ledger) == before
    assert not os.path.exists(os.path.join(op.defs_dir(tmp_path), S.OVERRIDES))


def test_other_parts_are_refused_with_a_clear_error(tmp_path):
    op = _op()
    _two_versions(op, tmp_path)
    for bad in ("relay.copilot_autopilot_relay.PROTOCOL", "relay.refuter.LENS_PROMPTS"):
        res = op.rollback_text(tmp_path, "x", part=bad)
        assert res["status"] == "refused" and bad in res["reason"] and "cannot be" in res["reason"]
        with pytest.raises(S.StoreError):
            S.write_overrides({bad: "x"}, op.defs_dir(tmp_path))
        with pytest.raises(S.StoreError):
            S.save_version("abc123", {bad: "x"}, directory=op.defs_dir(tmp_path))
    # a hand-written store naming another part is ignored by the reader, not applied
    S.write_overrides({LABEL: "ok"}, op.defs_dir(tmp_path))
    path = os.path.join(op.defs_dir(tmp_path), S.OVERRIDES)
    with io.open(path, "w", encoding="utf-8") as fh:
        json.dump({LABEL: "ok", "relay.copilot_autopilot_relay.PROTOCOL": "evil"}, fh)
    assert S.read_overrides(op.defs_dir(tmp_path)) == {LABEL: "ok"}


def _py(code, env_dir, *args):
    env = dict(os.environ, ORG_DEFS_DIR=str(env_dir))
    return subprocess.run([sys.executable, "-I", "-c", code, *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", cwd=REPO, env=env)


def test_the_store_is_read_once_at_import(tmp_path):
    S.write_overrides({LABEL: "first"}, tmp_path)
    code = ("import sys, json\n"
            "sys.path.insert(0, %r)\n"
            "from relay import planner\n"
            "from relay.org import def_store\n"
            "a = planner.PLAN_PROMPT\n"
            "def_store.write_overrides({%r: 'second'})\n"
            "print(json.dumps([a, planner.PLAN_PROMPT]))\n" % (REPO, LABEL))
    run = _py(code, tmp_path)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == ["first", "first"]                 # the write changed nothing
    again = _py("import sys\nsys.path.insert(0, %r)\nfrom relay import planner\n"
                "print(planner.PLAN_PROMPT)" % REPO, tmp_path)
    assert again.stdout.strip() == "second"                              # the next process sees it


def test_no_store_means_the_source_text_and_a_damaged_store_is_ignored(tmp_path):
    src = _py("import sys\nsys.path.insert(0, %r)\nfrom relay import planner\n"
              "print(planner.PLAN_PROMPT)" % REPO, tmp_path)
    with io.open(os.path.join(tmp_path, S.OVERRIDES), "w", encoding="utf-8") as fh:
        fh.write("{ not json")
    damaged = _py("import sys\nsys.path.insert(0, %r)\nfrom relay import planner\n"
                  "print(planner.PLAN_PROMPT)" % REPO, tmp_path)
    assert src.returncode == 0 and src.stdout == damaged.stdout and "PLAN_READY" in src.stdout


def test_writes_are_atomic(tmp_path, monkeypatch):
    S.write_overrides({LABEL: "old"}, tmp_path)
    target = os.path.join(tmp_path, S.OVERRIDES)
    seen = []
    real = os.replace

    def spy(src, dst):
        seen.append((os.path.basename(src), os.path.basename(dst), _bytes(dst)))
        return real(src, dst)

    monkeypatch.setattr(S.os, "replace", spy)
    S.write_overrides({LABEL: "new"}, tmp_path)
    # the file was complete in a temporary name and moved over; the old content was whole until then
    assert seen and seen[0][0].startswith(".tmp-") and seen[0][1] == S.OVERRIDES
    assert json.loads(seen[0][2]) == {LABEL: "old"}
    assert S.read_overrides(tmp_path) == {LABEL: "new"}

    def boom(src, dst):
        raise OSError("disk")

    monkeypatch.setattr(S.os, "replace", boom)
    with pytest.raises(OSError):
        S.write_overrides({LABEL: "newer"}, tmp_path)
    assert S.read_overrides(tmp_path) == {LABEL: "new"}                  # never half-written
    assert [f for f in os.listdir(tmp_path) if f.startswith(".tmp-")] == []
    assert _bytes(target)


def test_round_trip_through_fresh_processes(tmp_path):
    pytest.importorskip("fastmcp")
    defs = os.path.join(str(tmp_path), "org_defs")

    def cli(*a):
        env = dict(os.environ, ORG_DEFS_DIR=defs)
        r = subprocess.run([sys.executable, "-I", SCRIPT, "--fleet-dir", str(tmp_path), *a],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=REPO, env=env)
        return r.returncode, json.loads(r.stdout.strip().splitlines()[-1])

    rc, a = cli("promote")
    assert rc == 0
    S.write_overrides({LABEL: "an edited planner prompt"}, defs)          # the operator's edit
    rc, b = cli("promote")
    assert rc == 0 and a["genotype_id"] != b["genotype_id"]
    rc, out = cli("rollback", "--reason", "worse")
    assert rc == 0 and out["to"] == a["genotype_id"]
    rc, st = cli("status")                                                # a fresh import
    assert st["current"] == a["genotype_id"] and st["drift"]["state"] == "in_sync"
    assert st["drift"]["source"] == a["genotype_id"]
    rc, out = cli("rollback", "--reason", "undo")
    rc, st = cli("status")
    assert st["current"] == b["genotype_id"] and st["drift"]["state"] == "in_sync"
    rc, out = cli("rollback", "--reason", "x", "--part", "relay.copilot_autopilot_relay.PROTOCOL")
    assert rc == 1 and "cannot be" in out["reason"]


def test_the_cli_warns_when_the_version_record_is_off(tmp_path, capsys, monkeypatch):
    pytest.importorskip("fastmcp")
    op = _op()
    from relay.org import versions
    monkeypatch.setattr(versions, "mode", lambda settings_path=None: "off")
    rc = op.main(["--fleet-dir", str(tmp_path), "rollback", "--reason", "x"])
    cap = capsys.readouterr()
    assert rc == 1 and "org_version_record is off" in cap.err        # warned, not blocked by it
    assert "nothing has been promoted" in cap.out


def test_frozen_files_and_allowlists_are_untouched():
    from relay.selfimprove import apply as A
    from relay.selfimprove import authority_ledger as L
    from relay.selfimprove import frozen as F
    touched = ("relay/planner.py", "relay/org/def_store.py", "scripts/org_promotion.py",
               "relay/org/promotion.py")
    assert not [p for p in touched if p in F.FROZEN_MANIFEST]
    assert not [p for p in F.FROZEN_MANIFEST if "org" in p.lower() or "planner" in p.lower()]
    assert A.SCAFFOLD_ALLOWLIST == ["relay/selfimprove/active_genome.json",
                                    "relay/quality_cards.py", "relay/coding_discipline.py",
                                    "bench/swe_batch_setup.py"]
    assert "org_rollback" not in L.EVENTS
    run = subprocess.run([sys.executable, "-m", "relay.selfimprove.frozen", "--verify"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace",
                         cwd=REPO)
    assert run.returncode == 0, run.stdout + run.stderr


def test_no_run_path_reads_the_ledger_or_the_script():
    # the planner reads only the override store; nothing in relay/ or tools/ imports the ledger
    for root in ("relay", "tools"):
        for dirpath, _d, files in os.walk(os.path.join(REPO, root)):
            for f in files:
                if f.endswith(".py") and not f.startswith("test_"):
                    with io.open(os.path.join(dirpath, f), encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                    assert not re.search(r"^\s*(from|import)\s+[\w.]*org_promotion", text, re.M), f
