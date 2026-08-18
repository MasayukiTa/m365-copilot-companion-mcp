"""収集の幅。ここが Stage 1 の律速で、ターン数ではない。

前回の収集は12プロンプトを10周して120ターンになった。計画の検出力計算が要求するのは
**不一致対 約40** で、12単位はそれを何周しても作れない。同じ12件へターンを足すのは
行を増やして情報を増やさない行為である。

だからここで測るのは「プロンプトが何種類あるか」と「走行が何を買ったと自称するか」で、
応答の中身ではない。
"""
import io

from relay import settle_collect as C


def test_the_prompt_set_is_wide_enough_to_support_the_planned_statistic():
    """~40 不一致対を狙うなら、クラスタは少なくともその数だけ要る。"""
    assert len(C.PROMPTS) >= 40, (
        "%d プロンプトでは、何ターン走らせても Stage 1 は検出力不足のまま" % len(C.PROMPTS))


def test_no_prompt_is_repeated_inside_the_set():
    """重複はクラスタ数を静かに減らす -- 一覧の長さは嘘をつかないが、意味は変わる。"""
    assert len(set(C.PROMPTS)) == len(C.PROMPTS)


def test_the_set_still_ranges_over_answer_lengths():
    """長さとストリーミングの形が、安定判定が決めている当のもの。
    全部が短い（あるいは全部が長い）集合は、幅ではなく数だけを増やしている。"""
    short = [p for p in C.PROMPTS if "one line" in p or "Just the" in p
             or "only:" in p or "exactly the word" in p or "Two sentences" in p]
    long_ = [p for p in C.PROMPTS if any(n in p for n in
                                         ("150 words", "160 words", "170 words",
                                          "180 words", "200 words", "250 words"))]
    assert len(short) >= 4, "短い応答が足りない"
    assert len(long_) >= 4, "長い応答が足りない"


def test_the_set_includes_shapes_that_stream_in_bursts():
    """表・コードブロック・列挙はチャンク境界で止まる。安定判定が最も誤りやすい形。"""
    bursty = [p for p in C.PROMPTS
              if "table" in p.lower() or "Write a " in p or "List " in p
              or "Enumerate" in p or "query" in p]
    assert len(bursty) >= 8, "バースト気味に届く形が足りない"


def test_the_collector_says_how_many_clusters_a_run_will_produce():
    """1時間使う前に、その走行が何を買うのかを言うこと。
    ターン数はクラスタ数ではない -- そしてクラスタ数のほうが統計に効く。"""
    src = io.open(C.__file__, encoding="utf-8").read()
    assert "clusters = min(turns, len(PROMPTS))" in src
    assert "repeats" in src and "underpowered" in src


def test_the_cluster_label_is_still_attached_while_it_is_known():
    """どの質問だったかはトレースから復元できない -- トレースは答えを持っている。
    ラベル付けが落ちると、次の再生でまた 120 を独立標本と読むことになる。"""
    src = io.open(C.__file__, encoding="utf-8").read()
    assert "settle_trace_set_cluster" in src
