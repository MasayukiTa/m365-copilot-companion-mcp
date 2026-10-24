"""開き fence の無い `call_tool` ラベル + JSON を呼び出しとして読む。

実測 (2026-10-23, transcript r6adb57ae_a0_w0 の 2〜7 ターン): 返信が
`call_tool\\n{"name": ..., "arguments": {...}}\\n```` の形で届き (開きの ``` が無い)、
読み取りが 0 件になって 6 回催促のあと STUCK になった。ただし、散文に JSON が混じっただけの
ものを呼び出しにしてはならない (モデルの出力は信頼できない入力)。
"""
from relay import socket_tools as ST


def _calls(reply):
    return [(n, a) for _p, n, a in ST.read_reply(reply)["calls"]]


REAL = 'call_tool\n{"name": "read_file", "arguments": {"path": "C:/x/a.txt"}}\n```'


def test_the_real_shape_without_an_opening_fence_is_read():
    assert _calls(REAL) == [("call_tool", {"name": "read_file", "arguments": {"path": "C:/x/a.txt"}})]


def test_prose_before_and_a_trailing_continue_marker_are_fine():
    reply = "接続一覧の結果が返っていないので、再度確認します。\n\n" + REAL + "\n\nCONTINUE"
    assert len(_calls(reply)) == 1


def test_without_the_trailing_fence_it_is_still_read():
    assert len(_calls('call_tool\n{"name": "odbc_connections", "arguments": {}}')) == 1


def test_crlf_line_endings_are_read():
    assert len(_calls('call_tool\r\n{"name": "a", "arguments": {}}\r\n```\r\n')) == 1


def test_nested_braces_and_braces_in_strings_are_balanced():
    reply = 'call_tool\n{"name": "run_python", "arguments": {"code": "d = {\\"a\\": {1: 2}}  # } {"}}\n```'
    calls = _calls(reply)
    assert len(calls) == 1 and calls[0][1]["arguments"]["code"].startswith("d = ")


def test_a_fenced_call_and_an_unfenced_one_keep_the_order_written():
    reply = ('```call_tool\n{"name": "a", "arguments": {}}\n```\n'
             'call_tool\n{"name": "b", "arguments": {}}\n```')
    got = ST.read_reply(reply)["calls"]
    assert [(p, a["name"]) for p, _n, a in got] == [(1, "a"), (2, "b")]


def test_two_unfenced_calls_are_both_read():
    reply = ('call_tool\n{"name": "a", "arguments": {}}\n```\n\n'
             'call_tool\n{"name": "b", "arguments": {}}\n```')
    assert [a["name"] for _n, a in _calls(reply)] == ["a", "b"]


def test_strip_removes_the_unfenced_call_and_its_lone_fence():
    out = ST.strip_calls("前置き\n\n" + REAL + "\n\n続き")
    assert "call_tool" not in out and "```" not in out
    assert "前置き" in out and "続き" in out


# ---- 呼び出しにしてはならないもの -----------------------------------------------------------

def test_json_in_prose_without_the_label_is_not_a_call():
    assert _calls('結果は次のとおり。\n{"name": "read_file", "arguments": {}}\n以上') == []


def test_a_label_mentioned_mid_sentence_is_not_a_call():
    assert _calls('use call_tool\n{"name": "x", "arguments": {}}') == []
    assert _calls('call_tool を使います {"name": "x", "arguments": {}}') == []


def test_a_json_object_without_a_string_name_is_not_a_call():
    assert _calls('call_tool\n{"arguments": {}}') == []
    assert _calls('call_tool\n{"name": 3, "arguments": {}}') == []
    assert _calls('call_tool\n[{"name": "x"}]') == []


def test_a_broken_or_unbalanced_object_is_not_a_call():
    assert _calls('call_tool\n{"name": "x", "arguments": {') == []
    assert _calls('call_tool\n{"name": "x" "arguments": {}}') == []


def test_prose_after_the_object_on_the_same_line_is_not_a_call():
    assert _calls('call_tool\n{"name": "x", "arguments": {}} ということです') == []


def test_an_example_inside_a_fenced_code_block_is_not_a_call():
    reply = '例:\n```text\ncall_tool\n{"name": "x", "arguments": {}}\n```\n'
    assert _calls(reply) == []
    assert ST.strip_calls(reply) == reply.strip()


def test_other_labels_are_not_the_gateway():
    assert _calls('read_file\n{"name": "x", "arguments": {}}') == []
    assert _calls('call_tools\n{"name": "x", "arguments": {}}') == []


def test_an_allow_list_without_call_tool_reads_nothing():
    assert ST.read_reply(REAL, allowed=("other",))["calls"] == []
    assert ST.strip_calls(REAL, allowed=("other",)) == REAL.strip()


def test_the_ordinary_fenced_form_is_unchanged():
    reply = '```call_tool\n{"name": "a", "arguments": {}}\n```'
    assert _calls(reply) == [("call_tool", {"name": "a", "arguments": {}})]
    assert ST.strip_calls(reply) == ""


def test_step_runs_the_unfenced_call():
    seen = []

    def run(name, args):
        seen.append(args["name"])
        return True, "ok"

    nxt, made = ST.step(REAL, run)
    assert seen == ["read_file"] and nxt and len(made) == 1
