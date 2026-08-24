"""リサイクル報告のテスト。空であることと、測れなかったことを混同しない。"""
from scripts.recycle_report import load, summarise


def test_no_samples_says_what_would_produce_them():
    """『データが無い』で止まると、待ちが見えないまま何セッションも放置される。
    実際この判断は、まさにそれで塩漬けになっていた。"""
    out = summarise([])
    assert out["verdict"] is None
    assert "turns" in out["why"], "何をすれば埋まるのかが書かれていない"


def test_an_unmeasurable_row_is_not_counted_as_freeing_nothing():
    """after が null なのは『0 解放された』ではなく『測れなかった』。
    0 として平均に入れれば、失敗した読み取りを根拠に『リサイクルは無意味』と報告してしまう。"""
    rows = [{"before_mb": 900.0, "after_mb": None, "freed_mb": None, "turns": 3}]
    out = summarise(rows)
    assert out["measured"] == 0
    assert out["verdict"] is None
    assert "unreadable" in out["why"] or "could not be measured" in out["why"]


def test_a_few_samples_do_not_produce_a_verdict():
    """待機中ブラウザの振れは数百MB単位。4本で中央値を読んでも意味が無い。"""
    rows = [{"before_mb": 900.0, "after_mb": 500.0, "freed_mb": 400.0, "turns": 3}] * 4
    out = summarise(rows)
    assert out["verdict"] is None
    assert out["freed_median_mb"] == 400.0, "値そのものは出しておくこと"


def test_a_recycle_that_releases_is_named_as_such():
    rows = [{"before_mb": 900.0, "after_mb": 500.0, "freed_mb": 400.0, "turns": 3}] * 6
    assert summarise(rows)["verdict"] == "recycle-releases"


def test_a_recycle_that_frees_almost_nothing_is_named_as_such():
    rows = [{"before_mb": 900.0, "after_mb": 890.0, "freed_mb": 10.0, "turns": 3}] * 6
    assert summarise(rows)["verdict"] == "recycle-does-not-release"


def test_a_corrupt_line_does_not_hide_the_good_rows(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text('{"before_mb":1,"after_mb":0,"freed_mb":1}\nnot json\n', encoding="utf-8")
    rows, bad = load(str(p))
    assert len(rows) == 1 and bad == 1
