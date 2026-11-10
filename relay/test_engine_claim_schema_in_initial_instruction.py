# -*- coding: utf-8 -*-
"""The engine's required registration.claim keys are taught UP FRONT, from one single source,
and a re-send names only what is still missing.

Why this exists. The frozen validity engine's claim loader needs a STRUCTURED claim; a free-text
{id, statement, data_location} claim makes it return INPUT_MISSING_KEY before any analysis runs.
Previously the worker only learned the required keys AFTER that first engine failure, because the
initial instruction listed registration as just {root, claim, registered_at}. These tests pin:

  - the full engine-claim key list lives in ONE place (_ENGINE_CLAIM_SCHEMA_HINT);
  - the INITIAL request schema (JUDGE_REQUEST_SCHEMA) embeds that same source, so a worker can
    write a valid registration.claim the first time;
  - the composed goal injected by relay_fleet embeds that same source too (source inspection, so
    we do not drag the browser stack into a unit test);
  - the FIRST correction turn carries the full shapes; a SECOND+ turn names ONLY the keys still
    missing and drops the long schema -- no repeated bytes, same single source of truth.

No secret data, measurement row or employee id appears here -- every fixture is synthetic.
"""
import os

from relay import judge_gate


# The keys the frozen engine requires, mirrored in judge_gate from validity/claim.py. We assert
# the teaching text names each of them -- not a hand-copied list, but the module's own tuple.
REQUIRED = judge_gate._ENGINE_CLAIM_REQUIRED_KEYS


def test_single_source_hint_names_every_required_engine_key():
    hint = judge_gate._ENGINE_CLAIM_SCHEMA_HINT
    assert isinstance(hint, str) and hint.strip()
    for key in REQUIRED:
        assert key in hint, key


def test_initial_request_schema_embeds_the_same_single_source():
    """JUDGE_REQUEST_SCHEMA (the initial, read-once request shape) contains the SAME hint text,
    so the keys taught up front cannot drift from the keys the first correction names."""
    schema = judge_gate.JUDGE_REQUEST_SCHEMA
    assert judge_gate._ENGINE_CLAIM_SCHEMA_HINT in schema
    for key in REQUIRED:
        assert key in schema, key


def test_composed_goal_in_relay_fleet_embeds_the_same_single_source():
    """The instruction relay_fleet injects for a data-analysis goal must teach the structured
    claim from the SAME source. Source inspection (importing relay_fleet drags in a browser stack
    we do not want in a unit test): the composed_goal block references _ENGINE_CLAIM_SCHEMA_HINT.
    """
    here = os.path.dirname(__file__)
    src = open(os.path.join(here, "relay_fleet.py"), encoding="utf-8").read()
    i = src.index("composed_goal = (\"For this data analysis")
    # the hint is imported from the single source and concatenated into the injected instruction
    assert "_ENGINE_CLAIM_SCHEMA_HINT" in src[:i]
    # and the concatenation sits on the judge-output branch, before the composed_goal is used
    assert "_CLAIM_HINT" in src[i:i + 2000]


def _free_text_claim():
    """A worker's FREE-TEXT registration claim: it has a marker (statement) and none of the
    engine's structured keys beyond id, so the schema screen fires."""
    return {"id": "SYN1", "statement": "group_a rate differs between lots", "data_location": "data.csv"}


def test_first_correction_carries_full_shapes_second_names_only_missing(tmp_path, monkeypatch):
    """First turn: the full shape hint + the whole request schema. Second turn: only the keys
    still missing, and no long schema -- shorter, pointed at the remaining gap."""
    import json

    monkeypatch.setattr(judge_gate, "run_engine_judge",
                        lambda *a, **kw: {"engine_available": False})
    request = {"claim_id": "SYN1", "data_location": "data.csv",
               "fetched_at": "2026-10-30T00:00:00+09:00",
               "registration": {"root": "reg", "claim": _free_text_claim(),
                                 "registered_at": "2026-10-29T00:00:00+09:00"}}
    (tmp_path / "judge_request.json").write_text(json.dumps(request), encoding="utf-8")

    # FIRST correction turn (attempt 0): full hint + full schema handed back.
    a0, d0, c0 = judge_gate.complete_judge_request(
        "odbc_query で集計して表にする", str(tmp_path),
        correction_budget=2, correction_attempt=0)
    assert c0 is not None
    first_msg = " ".join(c0["messages"])
    assert "INPUT_MISSING_KEY" in first_msg
    assert judge_gate._ENGINE_CLAIM_SCHEMA_HINT in first_msg   # full shapes
    assert c0["schema"] == judge_gate.JUDGE_REQUEST_SCHEMA     # whole schema

    # SECOND correction turn (attempt 1): only the still-missing keys, no long schema.
    a1, d1, c1 = judge_gate.complete_judge_request(
        "odbc_query で集計して表にする", str(tmp_path),
        correction_budget=2, correction_attempt=1)
    assert c1 is not None
    second_msg = " ".join(c1["messages"])
    # the full multi-shape block is NOT repeated
    assert judge_gate._ENGINE_CLAIM_SCHEMA_HINT not in second_msg
    assert c1["schema"] == ""
    # but it still NAMES the missing keys (same single source: the gap list)
    for key in ("dataset_id", "data_kind", "response", "exposure"):
        assert key in second_msg, key
    # the second message is materially shorter than the first (no repeated schema/shapes)
    assert len(second_msg) < len(first_msg)
