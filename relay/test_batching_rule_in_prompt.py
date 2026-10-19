# -*- coding: utf-8 -*-
"""The prompt tells the model, firmly, to put independent calls in ONE reply.

Each model response costs about 10 s whatever it holds (speed/測定_新経路_ターン内訳_20261019),
so responses are the lever. The old sentence said independent calls "may" be combined; the
model mostly did not. These tests pin the stronger wording, that it is in the prompt exactly
once, and that the promise the wording makes (results come back numbered, in order) is true.
"""
from relay import socket_tools as ST

_CAT = [{"name": "call_tool", "description": "gateway"}]


def _prompt():
    return ST.build_prompt("REQ", _CAT, protocol="P\n")


def test_the_rule_is_an_instruction_not_a_permission():
    p = _prompt()
    assert "必ず1つの応答" in p
    assert "put all their fenced blocks in ONE reply" in p
    assert "まとめてよい" not in p


def test_the_rule_names_the_exception_so_dependent_calls_are_not_batched():
    assert "前の呼び出しの結果が要るもの" in _prompt()


def test_the_rule_appears_once_and_before_the_request():
    p = _prompt()
    assert p.count("put all their fenced blocks in ONE reply") == 1
    assert p.index("ONE reply") < p.index("<tools>") < p.index("REQ")


def test_no_tools_means_no_rule():
    assert ST.build_prompt("REQ", [], protocol="P\n") == "P\nREQ"


def test_the_promised_numbering_is_real():
    nxt, calls = ST.step('```call_tool\n{"name": "a"}\n```\n```call_tool\n{"name": "b"}\n```',
                         lambda n, a: (True, "r-" + a["name"]))
    assert len(calls) == 2
    assert nxt.index("#1 ") < nxt.index("r-a") < nxt.index("#2 ") < nxt.index("r-b")
