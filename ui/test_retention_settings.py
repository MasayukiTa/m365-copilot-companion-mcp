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


# ── 自動再試行 ──────────────────────────────────────────────────────────────

def test_auto_retry_is_on_by_default_with_a_cap():
    """28ゴール6走行で 25% が Copilot の定型拒否だった。毎回落ちる先が入れ替わり、
    同時実行数にも依らず(1でも2でも25%)、落ちたゴールは再投入すると通った。
    つまり一過性で、上限付き再試行がまさにその対処。25%が独立なら2回で6%。

    上限が消えたら、決定的に失敗する課題が無限に回る。ON と上限は必ず対で見る。"""
    assert re.search(r"bool _autoRetry = true;", SRC), "自動再試行が既定 OFF に戻っている"
    m = re.search(r"int _autoRetryMax = (\d+);", SRC)
    assert m, "上限が無い"
    assert 1 <= int(m.group(1)) <= 3, "上限が範囲外(無限ループの危険)"


def test_the_retry_budget_is_counted_by_goal_text():
    """再投入されたゴールは新しいワーカー名を得る。名前で数えると予算が毎回リセットされ、
    上限があっても実質無限になる。"""
    assert "_autoRetryCount[goal]" in SRC


def test_the_auto_retry_note_sits_with_its_own_fields():
    """保持設定のコメントを自動再試行の説明と _autoRetry の間に挟んでしまい、
    あの説明が保持フィールドを説明しているように読めていた。"""
    i = SRC.index("bool _autoRetry = true;")
    above = SRC[max(0, i - 900):i]
    assert "DEFAULT ON SINCE" in above, "自動再試行の説明が離れている"
    assert "_retDays" not in above, "保持フィールドが説明文の間に割り込んでいる"
