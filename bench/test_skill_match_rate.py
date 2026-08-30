"""The match rate is only meaningful next to the store it matched against."""
import io
import json

from bench.skill_match_rate import load, rate


def _log(tmp_path, rows):
    p = tmp_path / "skill_use.jsonl"
    io.open(p, "w", encoding="utf-8").write("".join(json.dumps(r) + "\n" for r in rows))
    return str(p)


def test_the_rate_is_over_consultations_not_over_log_lines(tmp_path):
    """Other kinds of record must not enter the denominator."""
    p = _log(tmp_path, [
        {"kind": "match", "matched": "a", "query_hash": "1"},
        {"kind": "match", "matched": "",  "query_hash": "2"},
        {"kind": "load",  "matched": "a", "query_hash": "3"},
    ])
    r = rate(load(p))
    assert r["consultations"] == 2 and r["matched"] == 1 and r["match_rate"] == 0.5


def test_repeated_probes_are_flagged_rather_than_averaged(tmp_path):
    """A rate over twenty runs of ONE question is not a rate over twenty questions.

    The flag is what stops the second from being reported as the first."""
    p = _log(tmp_path, [{"kind": "match", "matched": "", "query_hash": "same"}] * 5)
    r = rate(load(p))
    assert r["distinct_queries"] == 1
    assert r["consultations_are_distinct_questions"] is False


def test_an_empty_log_reports_no_rate_rather_than_zero(tmp_path):
    """Zero out of zero is not zero percent, and reporting it as such invents a finding."""
    r = rate(load(_log(tmp_path, [])))
    assert r["consultations"] == 0 and r["match_rate"] is None


def test_the_store_size_travels_with_the_rate(tmp_path):
    """A low rate against a store of six is a different fact from a low rate against sixty."""
    p = _log(tmp_path, [{"kind": "match", "matched": "", "query_hash": "1"}])
    r = rate(load(p), trusted_skills=6)
    assert r["trusted_skills_available"] == 6
    assert "more skills rather than fewer consultations" in r["reading"]
