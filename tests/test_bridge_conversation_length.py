"""死活確認が積み続けた会話に、長さの上限を入れる。

観測(2026-08-19): Edge のタブ1枚が 1,340.9 MB。タイトルはこのプローブの指示文そのもの。
新しく開かれた生存確認タブではなく、ブリッジが持つ**単一の常設会話**で、そこへ
10分ごと(既定144回/日)のプローブと実ユーザのターンが積み続けていた。長さを縛るものが
無かったので、Copilot のトークン上限か、マシンのメモリか、先に来たほうで止まる。
16GB の箱では後者で、空き 1.3GB になり、2000MB の床を要求する別部品が開始できなかった。

タブを閉じるのは修正ではない。**ログイン状態はタブではなくブラウザプロファイルと
サーバ側の承認記録が持つ**ので、ページは開き直せる。常設ページの価値はコンポーザが
すでに立っていること。プローブが UI 経路を要るのは、コンセントカードと「意味的に断る」
モデルがそこにしか現れないから。青天井なのは会話であって、縛るべきもそこ。

--- テストの書き方について ---
ブリッジ本体は Playwright とページオーナースレッドを前提にしていて import できない。
最初の版はそれを言い訳に全部を「ソースを grep する」テストにし、結果として
**この機能が一度も動かない欠陥**(タイマースレッドから Playwright を直接呼んでいた)を
5本とも素通しした。ここでは (1) 判断を import 可能な純関数へ出して本物のテストにし、
(2) 残る構造的な不変条件は grep ではなく ast で読む。文字列の有無ではなく
呼び出しの位置関係を見るので、コメントアウトや書き換えで素通りしない。
"""
import ast
import os

from tools.tool_probe import should_recycle_conversation as should_recycle

BRIDGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "bridge", "copilot_bridge.py")


def _source():
    with open(BRIDGE, encoding="utf-8") as f:
        return f.read()


def _tree():
    return ast.parse(_source())


def _func(name):
    for node in ast.walk(_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError("%s が無い" % name)


def _calls(node):
    """(呼び出し名, その呼び出しを囲む Lambda があるか) の列。"""
    out = []

    class V(ast.NodeVisitor):
        def __init__(self):
            self.in_lambda = 0

        def visit_Lambda(self, n):
            self.in_lambda += 1
            self.generic_visit(n)
            self.in_lambda -= 1

        def visit_Call(self, n):
            f = n.func
            name = getattr(f, "id", None) or getattr(f, "attr", None)
            out.append((name, self.in_lambda > 0))
            self.generic_visit(n)

    V().visit(node)
    return out


def _names_assigned_zero(node):
    got = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and n.value.value == 0:
            for t in n.targets:
                if isinstance(t, ast.Name):
                    got.add(t.id)
    return got


def _names_read(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


# ---- 判断そのもの（純関数なので本物のテスト） ----------------------------------------------------

def test_a_long_and_idle_conversation_is_replaced():
    assert should_recycle(120, 120, idle_s=3600, min_idle_s=1800)


def test_a_short_conversation_is_left_alone():
    assert not should_recycle(119, 120, idle_s=3600, min_idle_s=1800)


def test_a_long_conversation_is_not_replaced_under_someone():
    """リサイクルはエージェント側の文脈を黙って捨てる。会話の途中で起きてはいけない。"""
    assert not should_recycle(500, 120, idle_s=60, min_idle_s=1800)


def test_recycling_can_be_switched_off_entirely():
    assert not should_recycle(10_000, 0, idle_s=10_000, min_idle_s=1800)


def test_the_idle_bar_is_not_the_probe_collision_guard():
    """TOOL_PROBE_MIN_IDLE_SEC は 30 秒で、『いま送信中か』を見るためのもの。
    『誰かが会話の最中か』には短すぎる。取り違えると席を外した30秒後に文脈が消える。"""
    src = _source()
    i = src.index("BRIDGE_RECYCLE_MIN_IDLE_SEC = float(")
    assert "1800" in src[i:i + 200], "既定が 30 秒側に寄っている"


# ---- この機能が実際に動くこと ------------------------------------------------------------------

def test_the_recycle_runs_on_the_page_thread():
    """最初の版はタイマースレッドから直接 PAGE.goto を呼んでいた。Playwright sync API は
    スレッド親和性違反で即例外を出し、呼び出し元の except がそれを飲むので、
    **機能は一度も動かないのに全テストが緑**だった。位置関係を見るので素通りしない。"""
    fn = _func("_recycle_long_conversation")
    calls = _calls(fn)
    opens = [(name, in_lambda) for name, in_lambda in calls
             if name == "_open_fresh_conversation"]
    assert opens, "会話を開き直していない"
    for _name, in_lambda in opens:
        assert in_lambda, ("_open_fresh_conversation がページスレッドへ委譲されず"
                           "直接呼ばれている")
    assert any(name == "_run_bounded_page_probe_call" for name, _ in calls), (
        "ページスレッドへの委譲経路を通っていない")


def test_the_recycle_holds_the_page_lock_while_it_navigates():
    """アイドル判定は過去についての助言でしかない。判定とナビゲーションの間に
    実ターンがロックを取り、送信中に goto が重なり得る。"""
    fn = _func("_recycle_long_conversation")
    names = [n for n, _ in _calls(fn)]
    assert "acquire" in names and "release" in names


# ---- 数えること ---------------------------------------------------------------------------------

def test_turns_are_counted_where_the_message_is_actually_sent():
    """呼び出し側で数えると4経路(枯渇再送・consent再送・auto-unlock再送・プローブ再確認)を
    取りこぼす。1ターンで3回送信して1計上になり得た。"""
    tree = _tree()
    senders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if getattr(f, "attr", None) == "send" and getattr(getattr(f, "value", None),
                                                              "id", None) == "DRIVER":
                senders.append(node)
    assert len(senders) == 1, ("DRIVER.send が %d 箇所ある。計上点が分岐すると"
                               "また数え漏れる" % len(senders))
    counted = _func("_send_counted")
    assert "_CONVERSATION_TURNS" in _names_read(counted)


def test_the_counter_is_reset_where_the_session_is_swapped():
    """定期リサイクルの中だけでリセットすると、枯渇リサイクル後にカウンタが
    旧会話の 120+ を指したままになり、次の良プローブがほぼ新品の会話を即座に
    リサイクルする。/new も同じ穴。差し替え点で戻せば3つの呼び出し元が揃う。"""
    fn = _func("_open_fresh_conversation")
    assert "_CONVERSATION_TURNS" in _names_assigned_zero(fn)


# ---- 枯渇側の安全網を壊さないこと ---------------------------------------------------------------

def test_the_periodic_recycler_does_not_share_the_exhaustion_budget():
    """_BRIDGE_RECYCLES の上限(既定8)は『新しい会話でも枯渇するなら原因は長さではない』
    を検出するための網。定期リサイクルがこれを食うと、数日の平常運転で上限に達し、
    本物の枯渇時に網が無くなる。"""
    fn = _func("_recycle_long_conversation")
    assert "MAX_BRIDGE_RECYCLES" not in _names_read(fn), "枯渇の上限を共有している"
    assert "_PERIODIC_RECYCLES" in _names_read(fn), "定期側が専用カウンタを持っていない"
    assert "_BRIDGE_RECYCLES" in _names_assigned_zero(fn), (
        "会話を入れ替えたなら『長さが原因』という仮説は再び有効。枯渇側の予算を戻すこと")


def test_a_failed_recycle_is_filed_with_the_existing_vocabulary():
    """新しい kind を作ると、既にある再起動ラダーが拾わなくなる。"""
    from tools.tool_probe import PROBE_KINDS
    src = _source()
    body = src[src.index("def _recycle_long_conversation("):]
    body = body[:body.index("\ndef ", 10)]
    assert '"agent_unreachable"' in body
    assert "agent_unreachable" in PROBE_KINDS


def test_the_recycle_runs_after_the_authoritative_record_and_only_after_a_good_probe():
    """先にリサイクルすると、健康状態が『checking』と確定値の間に housekeeping 由来の
    失敗が挟まる。失敗直後にリサイクルすると、失敗の証拠を白紙で置き換えてしまい、
    次のプローブは『一度も動作を示していないブリッジ』を健全と報告する。"""
    src = _source()
    # アンカーは確定記録そのもの。logger 行を代理にすると、記録が動いたとき気づかない。
    record = src.index("tool_probe.record_probe(ok, kind, detail=")
    call = src.index("_recycle_long_conversation()", record)
    assert record < call, "確定記録より前にリサイクルしている"
    assert "if ok:" in src[record:call], "失敗したプローブの直後でもリサイクルしている"


# ---- 反復拒否の対策を薄めないこと ---------------------------------------------------------------

def test_the_challenge_rotation_is_not_simplified_away():
    """会話が定期的に新しくなっても、1周期に約120ターンは同じ会話に積まれる。
    同一文言の反復で Copilot が完了トークンを出さなくなった事故は、それで再発する。

    new_probe_challenge() は呼ばない。呼ぶと実リポジトリの .fleet/probe_challenge/ を
    書き換え、稼働中ブリッジの進行中チャレンジを壊し得る。しかも run_id が毎回
    埋まるので『2回呼んで違う』は変種が1個でも常に真で、何も証明しない。
    """
    from tools import tool_probe
    assert len(tool_probe._CHALLENGE_INSTRUCTION_VARIANTS) >= 2, (
        "実際に使われるのはこちらのリスト。PROBE_INSTRUCTION_VARIANTS ではない")
    assert len(tool_probe.PROBE_INSTRUCTION_VARIANTS) >= 2


# ---- 何を測っているかを偽らないこと -----------------------------------------------------------

def test_the_memory_figure_is_labelled_with_the_population_it_covers():
    """タブ単位の内訳は CDP から取れないので、この数字はブラウザ単位の粗い値。
    それを『タブの値』として出すと、次に読む人がそのまま引用する。
    docstring だけでなくログ本文に断りが要る。

    断り文は「全 Edge プロセス」だった。2026-08-24 に母集団をこのブリッジ自身の
    ブラウザへ絞ったので、その断りは嘘になった -- 端末上の msedge は45プロセス
    6,181MB あり、このブリッジのブラウザは 1,002MB で、59% はどちらでもなかった。
    守るべき不変条件は「どの母集団かを必ず書く」ことで、変わったのは母集団のほう。"""
    src = _source()
    body = src[src.index("def _report_recycle_memory_effect("):]
    body = body[:body.index(chr(10) + "def ", 10)]
    assert "all Edge processes" not in body, "絞ったのに古い断りが残っている"
    assert "this bridge" in body, "どのブラウザの値か書いていない"
    assert "coarse" in body


def test_the_memory_effect_is_measured_a_probe_later_not_immediately():
    """goto の直後に測っても、レンダラの解放は遅延し、旧会話の DOM を抱えた
    プロセス自体はまだ生きている。ほぼ確実に偽陰性になり、ログが自称する問い
    （navigating away が解放したか）に答えられない。"""
    recycler = _func("_recycle_long_conversation")
    names = [n for n, _ in _calls(recycler)]
    assert names.count("_edge_working_set_mb") == 1, "リサイクル内で after まで測っている"
    reporter = _func("_report_recycle_memory_effect")
    assert "_edge_working_set_mb" in [n for n, _ in _calls(reporter)]


def test_a_resumed_conversation_does_not_start_the_budget_over():
    """起動時の auto-resume は前の会話を開き直す。カウンタはプロセスと一緒に 0 から
    始まるので、種を入れないと 1.3GB まで育った当の会話が『新品』として扱われ、
    もう一周ぶん伸びる。"""
    src = _source()
    i = src.index('print("resumed session %s')
    window = src[max(0, i - 1200):i]
    assert "_CONVERSATION_TURNS = int(latest.get(\"turns\")" in window, (
        "再開した会話の長さを引き継いでいない")


def test_the_seed_is_labelled_as_a_lower_bound():
    """ストアが数えるのは永続化を頼まれたターンだけ。プローブの合成ターン
    (1日144回、多数派)は入らない。実測値のように書くと次に読む人が引用する。"""
    src = _source()
    i = src.index("_CONVERSATION_TURNS = int(latest.get(")
    window = src[max(0, i - 900):i]
    assert "LOWER BOUND" in window and "under-reads" in window
