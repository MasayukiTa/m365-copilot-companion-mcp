"""ワーカーの会話を、モデルがエラーを返し始める前に作り直す。

実測(2026-08-20、この機械): **新しい**会話のタブで DOM ノード926、HTML 550KB、
表示テキスト2KB、それでも JS ヒープが 137-161MB。つまり約150MBは Copilot の
Web アプリ自体で、会話とは無関係。その上に積むのがターンである。
別の実測では、上限の無い会話がタブを 1,340MB まで育てていた。

新しい機構は作っていない。フリートには既にトークン枯渇時のリサイクルがあり、
新会話を開き RECYCLE_PREFIX でゴールを再アンカーし、**ディスク上のファイルから
進捗を再導出させる**（relay_fleet.py 自身のコメント）。足したのは同じ道へ入る
2つ目の理由だけ。健全なうちに引っ越す方が、毎ターン同じエラーが返るように
なってから慌てるより穏当である。
"""
import inspect
import os

from relay import relay_fleet as RF


def _src(fn):
    return inspect.getsource(fn)


# ---- 閾値は測定に置き換わる前提で置かれていること -------------------------------------------------

def test_the_threshold_is_configurable_and_says_it_is_provisional():
    assert RF.FLEET_HEAP_RECYCLE_MB > 0
    src = inspect.getsource(RF)
    i = src.index("FLEET_HEAP_RECYCLE_MB = ")
    head = src[max(0, i - 1200):i]
    assert "PROVISIONAL" in head, "測定で置き換える前提だと書いていない"
    assert "137-161" in head, "実測値が根拠として残っていない"


def test_zero_disables_it_entirely():
    """効かせたくない運用者に、コードを読ませずに止めさせる。"""
    src = _src(RF.RelayWorker._memory_pressure)
    assert "FLEET_HEAP_RECYCLE_MB <= 0" in src


# ---- 連続リサイクルで予算を焼かないこと（危うく壊れたまま出すところだった） --------------------------

def test_the_cooldown_counts_from_the_last_recycle_not_from_the_start():
    """`self.turn` はワーカーの総予算カウンタで、会話を入れ替えてもリセットされない
    （max_turns が数え続ける必要がある）。だから絶対値で下限を見ると、リサイクル
    直後に素通りする。新しいタブのヒープが閾値を下回りきっていなければ、
    毎ターン作り直して max_recycles を焼き切り stuck になる。"""
    src = _src(RF.RelayWorker._memory_pressure)
    assert "_heap_recycle_turn" in src, "前回リサイクルからの相対で数えていない"
    assert "self.turn < FLEET_HEAP_MIN_TURNS" not in src, "絶対値で数えている"


def test_the_marker_is_set_where_the_recycle_happens():
    src = _src(RF.RelayWorker._decide)
    i = src.index("self._recycles += 1")
    assert "_heap_recycle_turn" in src[i:i + 200], (
        "リサイクルしたのに前回位置が更新されない。次のターンでまた発火する")


def test_the_worker_does_not_reset_its_global_turn_budget_on_recycle():
    """リセットすると max_turns が無限になる。ヒープ用の目印を別に持つ理由。"""
    src = _src(RF.RelayWorker._decide)
    i = src.index("self._recycles += 1")
    body = src[i:i + 1400]
    assert "self.turn = 0" not in body, "会話リサイクルで総ターン予算が巻き戻っている"


# ---- 既存の経路をそのまま使っていること ------------------------------------------------------------

def test_it_reuses_the_existing_recycle_road():
    """別経路を作ると、再アンカーも max_recycles 上限も二重管理になる。"""
    src = _src(RF.RelayWorker._decide)
    i = src.index("heavy = ")
    # ブロックの終わりまで見る。最初の `return` で切ると、再アンカーの手前にある
    # stuck 経路の return を境界にしてしまい、通っているものを見落とす。
    j = src.index("DEAD-AGENT", i)
    seg = src[i:j]
    assert "RECYCLE_PREFIX" in seg, "ゴールの再アンカーを通っていない"
    assert "_max_recycles" in seg, "リサイクル回数の上限を共有していない"


def test_exhaustion_still_wins_and_is_not_relabelled():
    """トークン枯渇はヒープとは別の事象。同じ道を通るが、理由は取り違えない。"""
    src = _src(RF.RelayWorker._decide)
    assert "not conversation_exhausted(resp)) and self._memory_pressure()" in src, (
        "枯渇しているのにヒープ由来と記録され得る")
    assert "会話トークン上限" in src and "ヒープ" in src


# ---- 測定が無ければ何もしないこと -----------------------------------------------------------------

def test_an_unreadable_heap_never_triggers_and_never_raises():
    """performance.memory は Chromium 限定で、設定次第で消える。
    読めないことを『重い』と読むと、測れない環境で毎回作り直すことになる。"""
    src = _src(RF.RelayWorker._heap_mb)
    assert "except Exception" in src and "return None" in src
    pressure = _src(RF.RelayWorker._memory_pressure)
    assert "if heap is None:" in pressure and "return False" in pressure


def test_every_turn_records_its_heap_so_the_threshold_can_be_measured():
    src = _src(RF.RelayWorker._decide)
    assert 'metric(self.turn, "heap_mb"' in src, "ターンごとの実測が残らない"


def test_the_transcript_can_carry_a_number_beside_the_turn():
    tx = RF._Transcript(None, "k", "n", "g")     # path=None -> writes nowhere, must not raise
    tx.metric(3, "heap_mb", 412.5, recycles=1)


def test_the_metric_lands_in_the_same_file_as_the_text(tmp_path):
    """別ファイルに出すと、太いターンとリークの区別がつかなくなる。"""
    import json
    tx = RF._Transcript(str(tmp_path), "k", "n", "g")
    tx.user(3, "send")
    tx.metric(3, "heap_mb", 412.5, recycles=1)
    rows = [json.loads(l) for l in open(tx.path, encoding="utf-8") if l.strip()]
    turns = [r for r in rows if r.get("turn") == 3]
    assert {r["role"] for r in turns} >= {"user", "metric"}
    got = [r for r in turns if r["role"] == "metric"][0]
    assert got["name"] == "heap_mb" and got["value"] == 412.5 and got["recycles"] == 1
