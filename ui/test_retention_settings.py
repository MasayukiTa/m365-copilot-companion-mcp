"""会話の保持設定 UI。既定が『消さない』であることと、効果を言葉で出すこと。

この保存層は履歴が失われるのを直すために在る。届いた日から削除を始める既定は、
同じ損失が予定表に載るだけになる。だから 0 が既定で、0 は「消さない」を意味する。
"""
import pathlib
import re

SRC = (pathlib.Path(__file__).resolve().parent / "FleetCockpit.cs").read_text(
    encoding="utf-8-sig", errors="replace")


def test_both_limits_default_to_keeping_everything():
    """既定で削除が始まってはいけない。ここが 0 でなくなったら、
    更新しただけの端末が起動時に履歴を捨てる。"""
    assert re.search(r"int _retDays = 0;", SRC), "保持日数の既定が 0 でない"
    assert re.search(r"int _retMb = 0;", SRC), "上限サイズの既定が 0 でない"


def test_zero_is_shown_as_keep_all_not_as_a_number():
    """0 を数字のまま出すと『0日保持＝即削除』に読める。言葉にする。"""
    assert '"ret_keep"' in SRC
    assert "消さない" in SRC and "keep all" in SRC
    assert SRC.count('_retDays == 0 ? T("ret_keep")') >= 1
    assert SRC.count('_retMb == 0 ? T("ret_keep")') >= 1


def test_the_effect_is_spelled_out_before_it_happens():
    """2つの数字から効果を推測させる設定は、一度入れて後悔するものになる。
    何が消えるのかを文章で出し、無効時と色を変える。"""
    assert "PaintRetentionNote" in SRC
    i = SRC.index("void PaintRetentionNote()")
    body = SRC[i:i + 2200]
    assert "ret_off" in body, "無効時の説明が無い"
    assert "Theme.Warning" in body, "有効時に警告色へ変えていない"
    assert "より古い会話" in body and "起動時に削除" in body, "何がいつ消えるか書いていない"
    assert "ret_whole" in body, "会話単位であることを伝えていない"


def test_it_uses_the_existing_settings_controls():
    """新しい流儀を持ち込まない。既存の節・行・ボタンの部品に乗せる。"""
    i = SRC.index('SectionHeader(T("set_retention_section"))')
    block = SRC[i:i + 1400]
    assert "SettingsStepperRow" in block
    assert 'MiniButton("−")' in block and 'MiniButton("+")' in block
    # 色付きの太い左レール(付箋風)は使わない -- 既存の指示
    assert "BorderThickness = new Thickness(4" not in block


def test_the_keys_match_what_the_store_reads():
    """UI が書く鍵と保存層が読む鍵がずれていれば、設定は黙って効かない。"""
    store = (pathlib.Path(__file__).resolve().parents[1] / "bridge" / "session_store.py"
             ).read_text(encoding="utf-8", errors="replace")
    for key in ("session_retention_days", "session_max_mb"):
        assert 'SaveKey("%s"' % key in SRC, "UI が %s を書いていない" % key
        assert '"%s"' % key in store, "保存層が %s を読んでいない" % key


def test_the_steps_are_usable():
    """1刻みだと目的の値までボタンを1分押し続けることになる。"""
    assert "RetMbStep" in SRC
    i = SRC.index("int RetMbStep()")
    assert "500" in SRC[i:i + 120] and "100" in SRC[i:i + 120]
