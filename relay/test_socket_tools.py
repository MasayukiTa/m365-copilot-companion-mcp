"""モデルの出力は**信頼できない入力**である、を実行の手前で固定する。

この機構では、モデルが書いた fenced block を見てこちら側がツールを実行する。つまり
返信テキストがそのまま実行要求になる。だからパーサの寛容さは、そのまま実行の危うさになる。

同時に、この機構が何を消すかも記録しておく: Copilot は我々の MCP に接続しないので、
渡すべき資格情報が無く（＝宣言に認証欄が無い問題が消え）、承認すべき対象も無い
（＝コンセントカードが発生しない）。エージェントに届かない件も、ツールがエージェント
由来でなくなるので効かなくなる。
"""
import json

from relay import socket_tools as ST


def _parse_calls(reply, **kw):
    """The calls a reply asks for, as [(name, args)]: read_reply is the production reader."""
    return [(name, args) for _pos, name, args in ST.read_reply(reply, **kw)["calls"]]


# ---- 実行してよいものだけを実行すること ---------------------------------------------------------

def test_only_allowed_names_are_returned():
    """モデルの出力は untrusted input。許可一覧に無い名前を拾えば、
    たまたまその名前だったブロックを、こちらが実行することになる。"""
    reply = "```rm_rf\n{\"path\": \"/\"}\n```"
    assert _parse_calls(reply) == []


def test_the_gateway_tool_is_what_is_advertised():
    """既存の PROTOCOL が『全ツールは call_tool の先』と教えている。
    транспорт が変わっても規律を変えない。"""
    assert ST.DEFAULT_ALLOWED == ("call_tool",)


def test_a_body_that_is_not_a_json_object_is_skipped():
    """モデルは普通のコードも書く。```python を呼び出しとして読むと、
    たまたまパースできたものを実行する。"""
    for body in ("print('hi')", "[1,2,3]", '"just a string"', "42", "null", "{oops"):
        assert _parse_calls("```call_tool\n%s\n```" % body) == [], body


def test_a_well_formed_call_is_read():
    reply = '```call_tool\n{"name": "read_file", "arguments": {"path": "a.txt"}}\n```'
    calls = _parse_calls(reply)
    assert calls == [("call_tool", {"name": "read_file",
                                    "arguments": {"path": "a.txt"}})]


def test_several_independent_calls_in_one_reply_are_all_read():
    reply = ('前置き\n'
             '```call_tool\n{"name": "a"}\n```\n'
             'あいだの文\n'
             '```call_tool\n{"name": "b"}\n```\n')
    assert [a["name"] for _n, a in _parse_calls(reply)] == ["a", "b"]


def test_every_call_block_in_a_reply_is_read_however_many_there_are():
    # There is no per-reply cap (owner decision 2026-10-20): 50 blocks are 50 calls, in order.
    reply = "".join('```call_tool\n{"name": "t%d"}\n```\n' % i for i in range(50))
    got = [a["name"] for _n, a in _parse_calls(reply)]
    assert got == ["t%d" % i for i in range(50)]
    assert not hasattr(ST, "MAX_CALLS_PER_REPLY")


def test_a_reply_with_no_calls_ends_the_turn():
    nxt, calls = ST.step("できました。", lambda n, a: (True, "x"))
    assert nxt is None and calls == []


# ---- 実行結果の扱い -----------------------------------------------------------------------------

def test_the_result_goes_back_as_a_turn_the_model_can_use():
    def run(name, args):
        return True, "3 files"
    nxt, calls = ST.step('```call_tool\n{"name": "list"}\n```', run)
    assert len(calls) == 1
    assert "3 files" in nxt and "実行結果" in nxt


def test_a_failing_tool_is_reported_rather_than_ending_the_goal():
    """引数を1つ間違えただけで目標ごと落とすより、何が起きたか伝えた方がよい。
    モデルはたいてい直せる。"""
    def run(name, args):
        raise ValueError("no such path")
    nxt, _calls = ST.step('```call_tool\n{"name": "read"}\n```', run)
    assert "失敗" in nxt and "ValueError" in nxt and "no such path" in nxt


def test_a_huge_result_goes_back_whole():
    """結果の長さの切り詰めはない(2026-10-20 オーナー判断。以前は8000文字で切っていた)。
    末尾まで届くことを、末尾の目印で確かめる。"""
    def run(name, args):
        return True, "x" * 50_000 + "END-MARK"
    nxt, _ = ST.step('```call_tool\n{"name": "big"}\n```', run)
    assert "切り詰め" not in nxt
    assert "x" * 50_000 + "END-MARK" in nxt


# ---- プロンプト --------------------------------------------------------------------------------

def test_the_request_comes_last_so_the_model_answers_it():
    """長い指示の直後に来たものに答える。タスクを先頭に置くと指示に答え始める。"""
    out = ST.build_prompt("合計を出して", [{"name": "call_tool", "description": "d"}],
                          protocol="[PROTO]")
    assert out.startswith("[PROTO]")
    assert out.index("<tools>") < out.index("合計を出して")
    assert out.rstrip().endswith("合計を出して")


def test_an_empty_catalogue_sends_the_task_untouched():
    """宣言するものが無いのに <tools> だけ送ると、存在しない道具を探させる。"""
    assert ST.build_prompt("やって", [], protocol="[P]") == "[P]やって"


def test_the_tools_block_carries_the_parameter_schema():
    block = ST.tools_block([{"name": "call_tool", "description": "gateway",
                             "parameters": {"type": "object"}}])
    assert "call_tool" in block and '"type"' in block


def test_a_nameless_entry_is_dropped_rather_than_advertised_as_blank():
    assert ST.tools_block([{"description": "no name"}]) == ""


# ---- モデルが言ったことと、要求したことを分けられること -------------------------------------------

def test_the_prose_can_be_separated_from_the_calls():
    reply = '確認します。\n```call_tool\n{"name": "a"}\n```\n以上です。'
    assert ST.strip_calls(reply) == "確認します。\n\n以上です。"


def test_stripping_leaves_ordinary_code_blocks_alone():
    """モデルが書いたコードは成果物であって呼び出しではない。消してはいけない。"""
    reply = "```python\nprint(1)\n```"
    assert "print(1)" in ST.strip_calls(reply)


# ---- この機構が何を消すか、コードに記録として残っていること ---------------------------------------

def test_the_module_does_not_execute_anything_itself():
    """実行は呼び出し側から注入される。ここが自分で実行できると、
    パーサの緩さが直接そのまま実行になる。"""
    src = open(ST.__file__, encoding="utf-8").read()
    body = src.split('"""', 2)[-1]
    for forbidden in ("subprocess", "os.system", "eval(", "exec(", "requests.", "httpx"):
        assert forbidden not in body, forbidden


def test_why_this_route_needs_no_credential_is_written_down():
    doc = ST.__doc__ or ""
    assert "AUTH" in doc and "CONSENT" in doc
    assert "Work IQ" in doc, "使えなくなるものも同じ場所に書いておくこと"


def test_a_fence_must_start_its_own_line():
    """文中に埋まった ``` を呼び出しとして読むと、モデルが説明のために書いた
    引用まで実行対象になる。行頭要求は意図的な厳しさ。"""
    inline = '確認します。```call_tool\n{"name": "ls"}\n```'
    assert _parse_calls(inline) == []
    proper = '確認します。\n```call_tool\n{"name": "ls"}\n```'
    assert _parse_calls(proper) == [("call_tool", {"name": "ls"})]


def test_an_indented_fence_still_counts():
    """箇条書きの中でモデルがインデントすることはある。行頭要求は
    『行の先頭が空白のみ』まで許す。"""
    assert _parse_calls('- 手順:\n  ```call_tool\n  {"name": "ls"}\n  ```') != []


# ---- 一括(バッチ)で返したとき、結果が呼び出しと取り違えられないこと -------------------------------

def _blk(name):
    return '```call_tool\n{"name": "%s"}\n```\n' % name


def _echo(name, args):
    return True, "out-" + args["name"]


def _head_lines(nxt):
    return [ln for ln in nxt.splitlines() if " → " in ln]


def test_each_result_carries_the_position_of_its_call():
    nxt, calls = ST.step(_blk("a") + _blk("b") + _blk("c"), _echo)
    assert len(calls) == 3
    heads = _head_lines(nxt)
    assert [h.split(" ")[0] for h in heads] == ["#1", "#2", "#3"]
    for h, tool in zip(heads, "abc"):
        assert "[%s]" % tool in h
    assert nxt.index("out-a") < nxt.index("out-b") < nxt.index("out-c")


def test_an_unreadable_block_is_named_and_the_others_keep_their_ids():
    reply = _blk("a") + '```call_tool\n{oops\n```\n' + _blk("c")
    nxt, calls = ST.step(reply, _echo)
    assert [a["name"] for _n, a in calls] == ["a", "c"]
    heads = _head_lines(nxt)
    assert [h.split(" ")[0] for h in heads] == ["#1", "#3"]
    assert "[a]" in heads[0] and "[c]" in heads[1]
    notice = nxt.split("--- 注意 ---")[1]
    assert "ブロック2" in notice and "読めなかった" in notice
    assert "ブロック1" not in notice and "ブロック3" not in notice


def test_a_block_that_is_not_an_object_is_named_with_its_reason():
    nxt, calls = ST.step(_blk("a") + '```call_tool\n[1, 2]\n```\n', _echo)
    assert len(calls) == 1
    assert "ブロック2" in nxt and "オブジェクトではない" in nxt


def test_more_than_eight_calls_in_one_reply_are_all_run_in_order():
    ran = []

    def run(name, args):
        ran.append(args["name"])
        return True, "ok"
    nxt, calls = ST.step("".join(_blk("t%d" % i) for i in range(1, 21)), run)
    want = ["t%d" % i for i in range(1, 21)]
    assert ran == want and [a["name"] for _n, a in calls] == want
    heads = _head_lines(nxt)
    assert [h.split(" ")[0] for h in heads] == ["#%d" % i for i in range(1, 21)]
    assert "--- 注意 ---" not in nxt, "nothing was left unrun, so there is nothing to resend"


def test_a_single_call_works_as_before_apart_from_its_id():
    nxt, calls = ST.step(_blk("a"), _echo)
    assert len(calls) == 1 and "out-a" in nxt
    assert "#1 call_tool → ok [a]" in nxt
    assert "--- 注意 ---" not in nxt


def test_only_unreadable_blocks_still_tell_the_model_instead_of_ending_silently():
    nxt, calls = ST.step('```call_tool\n{oops\n```', _echo)
    assert calls == [] and nxt is not None and "ブロック1" in nxt


def test_ordinary_code_blocks_are_not_counted_as_positions():
    reply = "```python\nprint(1)\n```\n" + _blk("a")
    nxt, _calls = ST.step(reply, _echo)
    assert "#1 call_tool → ok" in nxt


def test_result_turn_without_ids_keeps_the_old_header():
    assert "call_tool → ok\nx" in ST.result_turn([("call_tool", True, "x")])
