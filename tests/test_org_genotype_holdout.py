# -*- coding: utf-8 -*-
"""Sealed holdout registry and contamination verdict (slice E7)."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.org import genotype_holdout as H  # noqa: E402

SCRIPT = os.path.join(REPO, "scripts", "org_genotype_holdout.py")
HOLD = ["item-h1", "item-h2", "item-h3"]
DEV = ["item-d1", "item-d2"]


def X(item, genotype, purpose, at="t0"):
    return {"item": item, "genotype": genotype, "purpose": purpose, "at": at}


def test_seal_is_deterministic_and_order_independent():
    a = H.seal(HOLD)
    assert a == H.seal(list(reversed(HOLD))) == H.seal(HOLD + ["item-h1"])
    assert a["count"] == 3 and len(a["digest"]) == 64
    assert "item-h1" not in json.dumps(a)
    assert H.seal(HOLD + ["item-h4"])["digest"] != a["digest"]


def test_clean_when_sealed_and_each_item_evaluated_once():
    ex = [X("item-h1", "g1", "evaluation"), X("item-h2", "g2", "evaluation"),
          X("item-d1", "g1", "tuning")]
    v = H.verdict(H.seal(HOLD), DEV, HOLD, ex)
    assert v["state"] == "clean" and v["offending_items"] == []


def test_digest_mismatch_is_contaminated():
    v = H.verdict(H.seal(HOLD), DEV, HOLD + ["item-h9"], [])
    assert v["state"] == "contaminated" and v["findings"]["digest_mismatch"] is True
    assert v["sealed_digest"] != v["current_digest"]


@pytest.mark.parametrize("purpose", ["dev", "tuning", "candidate_generation", "some-new-purpose"])
def test_holdout_item_exposed_in_non_evaluation_context(purpose):
    v = H.verdict(H.seal(HOLD), DEV, HOLD, [X("item-h2", "g1", purpose)])
    assert v["state"] == "contaminated"
    assert v["offending_items"] == ["item-h2"]
    assert v["findings"]["leaked"][0]["purposes"] == [purpose]


def test_reuse_budget():
    ex = [X("item-h1", "g1", "evaluation"), X("item-h1", "g2", "evaluation")]
    seal = H.seal(HOLD)
    v = H.verdict(seal, DEV, HOLD, ex)
    assert v["state"] == "contaminated" and v["offending_items"] == ["item-h1"]
    assert v["findings"]["over_budget"][0]["genotypes"] == ["g1", "g2"]
    assert H.verdict(seal, DEV, HOLD, ex, budget=2)["state"] == "clean"
    # the same genotype evaluating the same item twice is one candidate, not a reuse
    same = [X("item-h1", "g1", "evaluation"), X("item-h1", "g1", "evaluation", at="t1")]
    assert H.verdict(seal, DEV, HOLD, same)["state"] == "clean"


def test_overlap_between_dev_and_holdout():
    v = H.verdict(H.seal(HOLD), DEV + ["item-h3"], HOLD, [])
    assert v["state"] == "contaminated" and v["findings"]["overlap"] == ["item-h3"]


def test_empty_and_missing_inputs_are_unsealed_not_clean():
    assert H.verdict(None, [], [], [])["state"] == "unsealed"
    assert H.verdict({}, DEV, HOLD, [])["state"] == "unsealed"
    assert H.verdict(H.seal([]), [], [], [])["state"] == "unsealed"
    assert H.seal([])["count"] == 0


def test_bad_input_is_refused():
    with pytest.raises(H.HoldoutError):
        H.seal("not-a-list")
    with pytest.raises(H.HoldoutError):
        H.seal([1])
    with pytest.raises(H.HoldoutError):
        H.verdict(H.seal(HOLD), DEV, HOLD, [{"item": "item-h1"}])
    with pytest.raises(H.HoldoutError):
        H.verdict(H.seal(HOLD), DEV, HOLD, [], budget=0)


def test_record_exposure_does_not_mutate():
    base = [X("item-h1", "g1", "evaluation")]
    out = H.record_exposure(base, "item-h2", "g1", "evaluation", "t9")
    assert len(base) == 1 and len(out) == 2 and out[1]["at"] == "t9"


def _run(*args):
    return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True,
                          encoding="utf-8")


def _write(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")
    return str(path)


def test_cli_exit_codes(tmp_path):
    hold = _write(tmp_path / "hold.json", HOLD)
    dev = _write(tmp_path / "dev.json", DEV)
    made = _run("--holdout", hold, "--make-seal")
    assert made.returncode == 0
    seal = _write(tmp_path / "seal.json", json.loads(made.stdout))

    clean = _write(tmp_path / "clean.json", [X("item-h1", "g1", "evaluation")])
    r = _run("--holdout", hold, "--seal", seal, "--dev", dev, "--exposures", clean)
    assert r.returncode == 0 and "state: clean" in r.stdout

    bad = _write(tmp_path / "bad.json", [X("item-h1", "g1", "tuning")])
    r = _run("--holdout", hold, "--seal", seal, "--exposures", bad, "--format", "json")
    assert r.returncode == 1
    assert json.loads(r.stdout)["offending_items"] == ["item-h1"]

    assert _run("--holdout", hold).returncode == 3  # no seal given
    assert _run("--holdout", str(tmp_path / "missing.json")).returncode == 2
    junk = tmp_path / "junk.json"
    junk.write_text("{not json", encoding="utf-8")
    assert _run("--holdout", str(junk)).returncode == 2


def test_cli_add_exposure_what_if(tmp_path):
    hold = _write(tmp_path / "hold.json", HOLD)
    made = _run("--holdout", hold, "--make-seal")
    seal = _write(tmp_path / "seal.json", json.loads(made.stdout))
    ok = _run("--holdout", hold, "--seal", seal, "--add-exposure", "item-h1:g1:evaluation:t1")
    assert ok.returncode == 0
    bad = _run("--holdout", hold, "--seal", seal, "--add-exposure", "item-h1:g1:tuning:t1")
    assert bad.returncode == 1
    assert _run("--holdout", hold, "--seal", seal, "--add-exposure", "oops").returncode == 2
