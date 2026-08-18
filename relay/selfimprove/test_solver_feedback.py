"""Solver feedback is evidence, and the tests are mostly about it not becoming more than that.

The easy version of this module would total up complaints and hand the loudest one to the
gate. That would be worse than having no feedback at all: a solver's account of why it failed
is exactly the kind of plausible, self-serving story that survives review because it sounds
like an explanation. So what is pinned here is the refusals -- no verdict, no score, nothing a
gate reads, and a hypothesis that still has to survive a paired evaluation like any other.
"""
import json

from relay.selfimprove import solver_feedback as F


# ---- reading what the solver actually said ------------------------------------------------

def test_a_clean_json_reply_is_read():
    reply = json.dumps({
        "missing_information": ["no schema for the input CSV"],
        "tool_friction": ["read_file needed three calls to page through one file"],
        "suggested_harness_change": "raise the read chunk size",
    })
    got = F.parse(reply)
    assert got["missing_information"] == ["no schema for the input CSV"]
    assert got["suggested_harness_change"] == "raise the read chunk size"
    assert got["memory_harmful"] == [], "言及されなかった項目は空であって欠落ではない"
    assert "parse_error" not in got


def test_json_wrapped_in_prose_or_a_fence_is_still_read():
    """モデルは JSON の前後に文章やコードフェンスを付ける。
    句読点を理由に本物のフィードバックを捨てるほうが損。"""
    inner = json.dumps({"tool_friction": ["the gate asked twice"]})
    for reply in ("Here is my feedback:\n```json\n%s\n```\nHope that helps." % inner,
                  "Sure! %s" % inner):
        assert F.parse(reply)["tool_friction"] == ["the gate asked twice"]


def test_an_unreadable_reply_reports_that_rather_than_inventing_one():
    """読めなかったことは、数えられる事実として残す -- 部分的に捏造した答えより有用。"""
    for reply in ("", "I did not have any problems, thanks!", "{not json"):
        got = F.parse(reply)
        assert got["parse_error"], reply
        assert got["missing_information"] == []


def test_a_single_string_where_a_list_was_asked_for_is_accepted():
    assert F.parse(json.dumps({"tool_friction": "one thing"}))["tool_friction"] == ["one thing"]


def test_an_empty_answer_is_a_real_answer():
    """空の回答は推測より望ましい -- そう prompt にも書いてある。"""
    got = F.parse(json.dumps({k: [] for k in F.LIST_FIELDS}))
    assert "parse_error" not in got
    assert all(got[k] == [] for k in F.LIST_FIELDS)


def test_one_verbose_solver_cannot_flood_the_tally():
    """40件の不満は2件の20倍の情報ではない。
    上限が無いと『どの摩擦が共通か』が『どのソルバが饒舌か』にすり替わる。"""
    got = F.parse(json.dumps({"tool_friction": ["item %d" % i for i in range(50)]}))
    assert len(got["tool_friction"]) == F.MAX_ITEMS_PER_FIELD


def test_an_invented_field_is_surfaced_rather_than_dropped():
    """ソルバが勝手なフィールドを作るのは、prompt が読み手の期待からずれた兆候。"""
    got = F.parse(json.dumps({"tool_friction": [], "my_own_field": "hi"}))
    assert got.get("unexpected_fields") == ["my_own_field"]


# ---- aggregating without letting one voice dominate ---------------------------------------

def _entry(eid, **fields):
    return F.collect(eid, json.dumps(fields))


def test_an_item_is_counted_once_per_episode_not_once_per_mention():
    """同じソルバが繰り返しても観測は1件。生の言及数で数えると、
    饒舌な1回が静かな5回に勝ってしまう。"""
    entries = [_entry("e1", tool_friction=["slow reads", "slow reads", "slow reads"]),
               _entry("e2", tool_friction=["slow reads"])]
    got = F.tally(entries)
    rows = got["fields"]["tool_friction"]
    assert rows[0]["item"] == "slow reads"
    assert rows[0]["episodes"] == 2, "言及数ではなくエピソード数で数える"
    assert rows[0]["where"] == ["e1", "e2"]


def test_every_item_carries_the_episode_it_came_from():
    """エピソードに紐づかない項目は、実際に何が起きたかと突き合わせられない。
    検証できないフィードバックを事実として扱うのが、この節が警告している失敗。"""
    got = F.tally([_entry("ep_alpha", missing_information=["no schema"])])
    assert got["fields"]["missing_information"][0]["where"] == ["ep_alpha"]


def test_unreadable_replies_are_counted_not_hidden():
    got = F.tally([_entry("e1", tool_friction=["x"]), F.collect("e2", "not json at all")])
    assert got["episodes"] == 2 and got["parse_errors"] == 1


# ---- and it must not become a decision ----------------------------------------------------

def test_a_single_complaint_does_not_become_a_hypothesis():
    """1件は逸話。逸話を所見に見せかけないことが、このモジュールの仕事の半分。"""
    got = F.to_hypotheses(F.tally([_entry("e1", tool_friction=["annoying"])]))
    assert got == []


def test_a_recurring_complaint_becomes_something_to_TEST():
    entries = [_entry("e%d" % i, tool_friction=["read_file pages awkwardly"]) for i in range(3)]
    got = F.to_hypotheses(F.tally(entries))
    assert len(got) == 1
    h = got[0]
    assert h["raised_by"] == 3
    assert h["evidence_episodes"] == ["e0", "e1", "e2"]
    assert "if that is a real harness limit" in h["hypothesis"], (
        "仮説が『ソルバがそう言った』を根拠にしている -- それは主張であって証拠ではない")


def test_a_hypothesis_does_not_come_with_a_genome():
    """どのつまみが不満に応えるかは、このモジュールには分からない。
    ここで当て推量で1つ選ぶのは、報告の中に決定を紛れ込ませること。"""
    entries = [_entry("e%d" % i, missing_tool_capability=["no way to diff two sheets"])
               for i in range(2)]
    assert F.to_hypotheses(F.tally(entries))[0]["genome"] is None


def test_nothing_here_returns_a_verdict_a_gate_could_read():
    """ソルバの言い分が受理判定に触れた瞬間、もっともらしい自己弁護が承認になる。"""
    entries = [_entry("e%d" % i, tool_friction=["x"]) for i in range(5)]
    tallied = F.tally(entries)
    blob = json.dumps({"tally": tallied, "hypotheses": F.to_hypotheses(tallied)})
    for word in ("keep", "reject", "verdict", "accept", "p_value", "significant"):
        assert word not in blob.lower(), "受理判定に読める語 %r が出力に含まれている" % word


def test_the_prompt_asks_for_the_shape_the_reader_expects():
    """prompt と parser が別々に育つと、返ってきた JSON を読めなくなる。"""
    text = F.prompt()
    for field in F.FIELDS:
        assert field in text, field
    assert "JSON only" in text


# ---- WHERE: Phase 7's missing axis, populated from Phase 6 --------------------------------

def test_feedback_naming_one_component_attributes_to_it():
    """『functional な失敗だった』は行動につながらない。
    『memory が違うものを思い出している』はつながる -- そしてそれが Phase 5 との接続点。"""
    assert F.where(_entry("e1", memory_harmful=["recalled last week's schema"])) == "memory"
    assert F.where(_entry("e2", tool_friction=["paging is awkward"])) == "tool"
    assert F.where(_entry("e3", missing_information=["no schema"])) == "context"
    assert F.where(_entry("e4", review_overhead="two rounds for a typo")) == "reviewer"


def test_feedback_naming_two_components_attributes_to_neither():
    """2つ挙がったのは『どちらかが原因』の証拠ではなく『不満が複数あった』の証拠。
    最初のものや声の大きいものを選ぶのは、所見の捏造。"""
    both = _entry("e1", memory_harmful=["stale"], tool_friction=["awkward"])
    assert F.where(both) == F.UNATTRIBUTED


def test_silence_is_not_an_attribution():
    """黙っていることを実在のセルに畳み込むと、持っていない帰属を報告することになる。

    アーカイブには既に同型の事故が記録されている -- 既定値に解決した descriptors が
    全行を1セルに潰し、1セルの多様性マップは『品質最大・多様性ゼロ』を報告した。"""
    assert F.where(_entry("e1")) == F.UNATTRIBUTED
    assert F.where(F.collect("e2", "not json")) == F.UNATTRIBUTED


def test_a_useful_memory_is_also_an_attribution():
    """軸は『どこが悪かったか』ではなく『どこが効いたか』。
    エピソードを支えている memory は、誤らせている memory と同じくらいその軸の事実。"""
    assert F.where(_entry("e1", memory_useful=["the prior schema note"])) == "memory"


def test_the_distribution_shows_how_much_is_unattributed():
    """帰属できた数件を『ハーネスの地図』として読む前に、
    大半が unattributed なら軸がまだ情報を運んでいないことを読者は知る必要がある。"""
    entries = [_entry("e1", memory_harmful=["x"]),
               _entry("e2"),
               _entry("e3", memory_harmful=["x"], tool_friction=["y"])]
    got = F.where_distribution(entries)
    assert got["counts"]["memory"] == 1
    assert got["counts"][F.UNATTRIBUTED] == 2
    assert got["attributed"] == 1 and got["total"] == 3
    assert got["attribution_rate"] == round(1 / 3, 4)


def test_free_text_is_never_guessed_into_a_component():
    """`suggested_harness_change` は何についてでも書ける自由記述。
    そこから component を推測するのは、帰属を読むのではなく作ること。"""
    assert "suggested_harness_change" not in F.FIELD_TO_COMPONENT
    assert F.where(_entry("e1", suggested_harness_change="make memory better")) == \
        F.UNATTRIBUTED
