# -*- coding: utf-8 -*-
"""Data provenance and contamination report (slice G2). Synthetic data only."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.org import provenance as P  # noqa: E402

SCRIPT = os.path.join(REPO, "scripts", "org_provenance.py")


def rec(item, pool="", **extra):
    r = {"item_id": item, "pool": pool}
    r.update(extra)
    return r


def test_empty_and_non_list_input():
    for bad in ([], None, "x", {"a": 1}):
        r = P.build_report(bad)
        assert r["total"] == 0 and r["items"] == 0 and r["malformed"] == 0
        assert r["contaminated_holdout_items"] == [] and r["provenance"] == {}


@pytest.mark.parametrize("record,expected", [
    (rec("a", "sealed"), P.HOLDOUT),
    (rec("a", "SEALED"), P.HOLDOUT),
    (rec("a", " holdout "), P.HOLDOUT),
    (rec("a", "dev"), P.DESIGN),
    (rec("a", "regression"), P.DESIGN),
    (rec("a", "", burned=True), P.DESIGN),
    (rec("a", ""), P.UNKNOWN),
    ({"item_id": "a"}, P.UNKNOWN),
    ("text", P.UNKNOWN),
])
def test_role(record, expected):
    assert P.role_of(record) == expected


def test_burned_flag_does_not_make_sealed_pool_design_by_itself():
    assert P.role_of(rec("a", "sealed", burned=True)) == P.HOLDOUT


@pytest.mark.parametrize("record,expected", [
    ({"item_id": "a"}, "a"),
    ({"instance_id": "b"}, "b"),
    ({"task_id": "c"}, "c"),
    ({"item_id": "  ", "task_id": "c"}, "c"),
    ({"item_id": 5}, None),
    ({}, None),
    ([], None),
])
def test_item_id_of(record, expected):
    assert P.item_id_of(record) == expected


def test_malformed_rows_skipped_and_counted():
    rows = [rec("a", "dev"), "junk", None, {"pool": "sealed"}, 7, rec("b", "sealed")]
    r = P.build_report(rows)
    assert r["total"] == 6 and r["malformed"] == 4 and r["items"] == 2
    assert [s["index"] for s in r["skipped"]] == [1, 2, 3, 4]


def test_design_only_and_holdout_only_items():
    r = P.build_report([rec("d", "dev"), rec("h", "sealed")])
    assert r["design_items"] == 1 and r["holdout_items"] == 1
    assert r["provenance"]["d"]["flags"] == [] and r["provenance"]["h"]["flags"] == []
    assert r["clean_holdout_items"] == ["h"] and r["contaminated_holdout_items"] == []


def test_reuse_across_design_and_holdout_flagged():
    r = P.build_report([rec("x", "dev"), rec("x", "sealed"), rec("y", "sealed")])
    assert P.REUSED in r["provenance"]["x"]["flags"]
    assert r["provenance"]["y"]["flags"] == []
    assert r["flag_counts"][P.REUSED] == 1
    assert r["contaminated_holdout_items"] == ["x"]
    assert r["clean_holdout_items"] == ["y"]


def test_reuse_order_does_not_matter():
    a = P.build_report([rec("x", "sealed"), rec("x", "dev")])
    b = P.build_report([rec("x", "dev"), rec("x", "sealed")])
    assert a["flag_counts"] == b["flag_counts"]


def test_burned_marker_plus_holdout_run_is_reuse():
    r = P.build_report([rec("x", "", burned=True), rec("x", "sealed")])
    assert P.REUSED in r["provenance"]["x"]["flags"] and P.BURNED in r["provenance"]["x"]["flags"]


def test_burned_flag_alone_does_not_contaminate_a_clean_holdout_item():
    r = P.build_report([rec("x", "sealed", burned=True)])
    assert r["provenance"]["x"]["flags"] == [P.BURNED]
    assert r["contaminated_holdout_items"] == [] and r["clean_holdout_items"] == ["x"]


def test_two_holdout_reads_are_not_reuse():
    r = P.build_report([rec("x", "sealed"), rec("x", "sealed")])
    assert r["provenance"]["x"]["holdout_uses"] == 2
    assert r["provenance"]["x"]["flags"] == []


def test_holdout_id_in_candidate_inputs_flagged():
    rows = [rec("case-17", "sealed"), rec("other", "dev", candidate_inputs=["fix the failure on case-17 please"])]
    r = P.build_report(rows)
    assert P.ID_LEAK in r["provenance"]["case-17"]["flags"]
    assert r["contaminated_holdout_items"] == ["case-17"]


def test_id_match_respects_token_boundaries():
    rows = [rec("case-1", "sealed"), rec("z", "dev", candidate_inputs="saw case-10 and xcase-1 and case-1x")]
    assert P.build_report(rows)["provenance"]["case-1"]["flags"] == []


def test_id_match_at_punctuation_and_line_edges():
    rows = [rec("case-1", "sealed"), rec("z", "dev", candidate_inputs=["case-1", "(case-1)."])]
    assert P.ID_LEAK in P.build_report(rows)["provenance"]["case-1"]["flags"]


def test_id_with_regex_characters_is_matched_literally():
    rows = [rec("a.b+c", "sealed"), rec("z", "dev", candidate_inputs=["aXb+c", "a.b+c"])]
    assert P.ID_LEAK in P.build_report(rows)["provenance"]["a.b+c"]["flags"]
    rows = [rec("a.b+c", "sealed"), rec("z", "dev", candidate_inputs=["aXb+c"])]
    assert P.build_report(rows)["provenance"]["a.b+c"]["flags"] == []


def test_design_item_id_in_inputs_is_not_a_holdout_leak():
    rows = [rec("d1", "dev"), rec("z", "dev", candidate_inputs=["d1 failed"])]
    assert P.build_report(rows)["provenance"]["d1"]["flags"] == []


def test_holdout_text_in_candidate_inputs_flagged():
    rows = [rec("h1", "sealed", item_text="  Reverse the list in place.  "),
            rec("z", "dev", candidate_inputs=["note: Reverse the list in place. then retry"])]
    r = P.build_report(rows)
    assert P.TEXT_LEAK in r["provenance"]["h1"]["flags"]
    assert P.ID_LEAK not in r["provenance"]["h1"]["flags"]


def test_text_match_is_case_sensitive_copy_only():
    rows = [rec("h1", "sealed", item_text="Reverse the list"), rec("z", "dev", candidate_inputs=["reverse the list"])]
    assert P.build_report(rows)["provenance"]["h1"]["flags"] == []


def test_leak_from_the_items_own_holdout_record_inputs_counts():
    rows = [rec("h1", "sealed", candidate_inputs=["context mentions h1"])]
    assert P.ID_LEAK in P.build_report(rows)["provenance"]["h1"]["flags"]


def test_bad_candidate_inputs_shapes_are_ignored():
    rows = [rec("h1", "sealed"),
            rec("z", "dev", candidate_inputs=[1, None, {"x": "h1"}, ["h1"]]),
            rec("w", "dev", candidate_inputs={"k": "h1"}),
            rec("v", "dev", candidate_inputs=7)]
    assert P.build_report(rows)["provenance"]["h1"]["flags"] == []


def test_blank_item_text_is_not_a_leak():
    rows = [rec("h1", "sealed", item_text="   "), rec("z", "dev", candidate_inputs=["anything"])]
    assert P.build_report(rows)["provenance"]["h1"]["flags"] == []


def test_unknown_only_items_counted_not_assumed():
    r = P.build_report([rec("u"), rec("u", ""), rec("h", "sealed")])
    assert r["unknown_only_items"] == 1 and r["provenance"]["u"]["unknown_uses"] == 2
    assert r["holdout_items"] == 1 and r["design_items"] == 0


def test_pools_listed_sorted_and_record_indexes_kept():
    r = P.build_report([rec("x", "Dev"), rec("x", "regression"), rec("x", "sealed")])
    assert r["provenance"]["x"]["pools"] == ["dev", "regression", "sealed"]
    assert r["provenance"]["x"]["record_indexes"] == [0, 1, 2]


def test_input_is_not_mutated_and_report_is_deterministic():
    rows = [rec("x", "dev"), rec("x", "sealed", candidate_inputs=["x"]), "bad"]
    before = copy.deepcopy(rows)
    a = P.to_json(P.build_report(rows))
    assert rows == before
    assert a == P.to_json(P.build_report(rows))
    json.loads(a)


def test_build_provenance_returns_items_and_skipped():
    items, skipped = P.build_provenance([rec("a", "dev"), 3])
    assert list(items) == ["a"] and skipped == [{"index": 1, "code": "malformed"}]


def _write(tmp_path, lines):
    p = tmp_path / "runs.jsonl"
    p.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return str(p)


def _run(*args):
    return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, timeout=60)


def test_cli_text_and_json_and_writes_nothing(tmp_path):
    path = _write(tmp_path, [json.dumps(rec("x", "dev")), json.dumps(rec("x", "sealed")),
                             "{not json", "", json.dumps(rec("y", "sealed"))])
    before = sorted(os.listdir(tmp_path))
    p = _run(path)
    assert p.returncode == 0
    out = p.stdout.decode("utf-8")
    assert "3 records, 1 malformed" in out or "4 records, 1 malformed" in out
    assert "x: reused_across_train_and_holdout" in out
    q = _run(path, "--format", "json")
    assert q.returncode == 0
    data = json.loads(q.stdout.decode("utf-8"))
    assert data["flag_counts"][P.REUSED] == 1 and data["malformed"] == 1
    assert sorted(os.listdir(tmp_path)) == before


def test_cli_refuses_missing_file(tmp_path):
    p = _run(str(tmp_path / "nope.jsonl"))
    assert p.returncode == 2 and b"refused" in p.stderr


def test_cli_empty_file(tmp_path):
    path = _write(tmp_path, [""])
    p = _run(path, "--format", "json")
    assert p.returncode == 0 and json.loads(p.stdout.decode("utf-8"))["total"] == 0
