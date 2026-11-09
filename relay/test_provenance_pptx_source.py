# -*- coding: utf-8 -*-
"""A number written into THIS run's .pptx deliverable is a produced-in-this-run source.

The provenance gate (relay/analysis_provenance.py) accepts a number if it appears in a file
this run created/changed/read. For md/json/csv/txt the body is a plain string arg. For a .pptx
the body is NOT a string: it is the structured slide spec passed to the MCP pptx writers
(create_pptx 'slides', pptx_from_markdown 'markdown', pptx_add_slide 'slide', pptx_add_table
'rows'), and the file on disk is binary, so reading the outcome would never reveal the numbers.

The real false rejection (2026-11-03, second-wave manufacturing analysis): a worker built its
slide with create_pptx/pptx_add_table carrying the computed statistics (0.271, 4.77), reported
them in its answer, and was sent back VERIFY_FAILED because the collector only looked at plain
string write args. These tests pin two things so that cannot silently come back:

  * RelayWorker._unwrap_arg json-encodes a structured pptx arg (dict/list) so every number it
    carries survives as text -- while still unwrapping the ledger's {'text': ...} wrapper and
    leaving a bare string untouched;
  * the pptx writers are registered in _FILE_WRITE_ARG with the arg that holds the body, and an
    answer whose numbers are only on the slide PASSES, while a number that is on neither the
    slide nor any computation is still flagged (transcription stays caught).

Hermetic: pure functions over (answer text, this run's file bodies); no ledger/clock/IO.
"""
from __future__ import annotations

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import analysis_provenance as ap  # noqa: E402
from relay.relay_fleet import RelayWorker  # noqa: E402


# -- _unwrap_arg over every shape it must handle ---------------------------------------------

def test_unwrap_arg_unwraps_the_ledger_text_wrapper():
    # The ordinary bounded wrapper the ledger stores for a string arg.
    assert RelayWorker._unwrap_arg({"text": "defectA rho = 0.271", "len": 18}) == \
        "defectA rho = 0.271"


def test_unwrap_arg_leaves_a_bare_string_untouched():
    assert RelayWorker._unwrap_arg("n = 7, mean = 4.77") == "n = 7, mean = 4.77"


def test_unwrap_arg_json_encodes_pptx_slides_so_numbers_survive():
    slides = [{"title": "\u5bf8\u6cd5\u78ba\u8a8d", "bullets": ["rho=0.271", "mean=4.77"]}]
    out = RelayWorker._unwrap_arg(slides)
    # It is valid JSON and both computed numbers are present as text.
    back = json.loads(out)
    assert back == slides
    assert "0.271" in out and "4.77" in out


def test_unwrap_arg_json_encodes_a_table_rows_arg():
    rows = [["metric", "value"], ["rho", "0.271"], ["mean", "4.77"]]
    out = RelayWorker._unwrap_arg(rows)
    assert "0.271" in out and "4.77" in out


def test_unwrap_arg_handles_a_create_pptx_arg_dict_with_slides_key():
    # create_pptx's whole args can arrive as {'slides': [...], 'title': ...}; its slide numbers
    # must survive rather than being read as an empty ledger-wrapper.
    arg = {"slides": [{"bullets": ["x=0.271"]}], "title": "t"}
    out = RelayWorker._unwrap_arg(arg)
    assert "0.271" in out


def test_unwrap_arg_returns_empty_for_none_or_number():
    assert RelayWorker._unwrap_arg(None) == ""
    assert RelayWorker._unwrap_arg(5) == ""


# -- the pptx writers are registered as body-bearing file writes ----------------------------

def test_pptx_writers_are_registered_in_file_write_arg():
    for tool, arg in (("create_pptx", "slides"),
                      ("pptx_from_markdown", "markdown"),
                      ("pptx_add_slide", "slide"),
                      ("pptx_add_table", "rows")):
        assert RelayWorker._FILE_WRITE_ARG.get(tool) == arg, tool


# -- end to end: a slide number is a valid source, a slide-absent number is still caught ------

def _slide_body():
    # What _judge_run_file_outputs would append for a create_pptx call whose slides carry the
    # run's computed statistics.
    slides = [{"title": "\u5bf8\u6cd5\u78ba\u8a8d",
               "bullets": ["rho=0.271", "mean=4.77", "viol=0"]}]
    return RelayWorker._unwrap_arg(slides)


def test_a_number_only_on_this_runs_slide_is_supported():
    answer = ("\u5bf8\u6cd5\u78ba\u8a8d \u89e3\u6790\u7d50\u679c "
              "\u76f8\u95a2\u4fc2\u6570 0.271 \u5e73\u5747\u8d85\u904e 4.77 "
              "\u9055\u53cd\u4ef6\u6570 0 \u4ef6 \u4e0a\u4f4d 4 \u4ef6")
    r = ap.check_answer_provenance(
        answer, compute_outputs=[], is_analysis=True,
        request_text="\u5bf8\u6cd5\u78ba\u8a8d", run_file_outputs=[_slide_body()])
    assert r["ok"], r
    assert r["unsupported"] == [], r


def test_a_table_rows_pptx_source_supports_the_answer():
    rows = [["metric", "value"], ["rho", "0.271"], ["mean", "4.77"]]
    body = RelayWorker._unwrap_arg(rows)
    answer = "\u76f8\u95a2 0.271 \u5e73\u5747 4.77"
    r = ap.check_answer_provenance(answer, compute_outputs=[], is_analysis=True,
                                   run_file_outputs=[body])
    assert r["ok"], r


def test_a_number_on_neither_slide_nor_computation_is_still_rejected():
    # Transcription must still be caught: 0.959 is on no slide and in no computation output.
    answer = "\u76f8\u95a2 0.271 \u5e73\u5747 4.77 \u3060\u304c 0.959 \u3082\u8ff0\u3079\u308b"
    r = ap.check_answer_provenance(
        answer, compute_outputs=[], is_analysis=True,
        request_text="\u5bf8\u6cd5\u78ba\u8a8d", run_file_outputs=[_slide_body()])
    assert not r["ok"], r
    assert any(abs(v - 0.959) < 1e-9 for v in r["unsupported"]), r["unsupported"]
