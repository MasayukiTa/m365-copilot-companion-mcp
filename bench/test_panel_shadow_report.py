"""Reading the veto shadow back. The properties are about not overstating what was measured."""
import json

from bench.panel_shadow_report import read_ledger, summarize
from relay.refuter import panel_shadow


def _panel(correctness, edge, security):
    rec = panel_shadow([("correctness", correctness, "c" if correctness == "REFUTED" else ""),
                        ("edge", edge, "e" if edge == "REFUTED" else ""),
                        ("security", security, "s" if security == "REFUTED" else "")])
    rec["kind"] = "panel"
    return rec


def test_an_empty_ledger_is_nothing_measured_not_a_zero_flip_rate():
    """The confusion this guards: 'no records' and 'the veto never fired' both look like 0."""
    s = summarize([])
    assert s["panels"] == 0 and s["flip_rate"] is None


def test_a_lone_security_refusal_counts_as_a_flip():
    s = summarize([_panel("UPHELD", "UPHELD", "REFUTED")])
    assert s["flips"] == 1 and s["flip_rate"] == 1.0


def test_a_lone_correctness_refusal_is_not_a_flip():
    s = summarize([_panel("REFUTED", "UPHELD", "UPHELD")])
    assert s["flips"] == 0


def test_unclear_leaves_the_refute_rate_denominator():
    """A reviewer that could not decide has not said the work is fine. Counting it as a
    non-refusal credits the lens with a clean look it never took."""
    s = summarize([_panel("UPHELD", "UPHELD", "UNCLEAR"),
                   _panel("UPHELD", "UPHELD", "REFUTED")])
    sec = s["per_lens"]["security"]
    assert sec["unclear"] == 1 and sec["refuted"] == 1 and sec["upheld"] == 0
    assert sec["refute_rate"] == 1.0


def test_a_lens_that_only_ever_abstained_has_no_rate_rather_than_zero():
    s = summarize([_panel("UPHELD", "UPHELD", "UNCLEAR")])
    assert s["per_lens"]["security"]["refute_rate"] is None


def test_the_flip_reasons_are_carried_through_for_a_human_to_judge():
    """The file refuses to call a flip real or noise; it must at least hand over the evidence."""
    s = summarize([_panel("UPHELD", "UPHELD", "REFUTED")])
    assert "s" in s["flip_rows"][0]["veto_shadow"]["reason"]


def test_a_half_written_last_line_does_not_take_the_file_down(tmp_path):
    """The ledger is appended to by a live run, so its tail can be truncated mid-write."""
    p = tmp_path / "panels.jsonl"
    good = dict(_panel("UPHELD", "UPHELD", "REFUTED"))
    p.write_text(json.dumps(good) + "\n" + '{"kind": "panel", "would_f',
                 encoding="utf-8")
    assert len(read_ledger(str(p))) == 1


def test_lines_that_are_not_panels_are_ignored(tmp_path):
    p = tmp_path / "panels.jsonl"
    p.write_text(json.dumps({"kind": "campaign", "goal": "x"}) + "\n", encoding="utf-8")
    assert read_ledger(str(p)) == []


def test_a_missing_ledger_reads_as_empty_rather_than_raising(tmp_path):
    assert read_ledger(str(tmp_path / "nope.jsonl")) == []
