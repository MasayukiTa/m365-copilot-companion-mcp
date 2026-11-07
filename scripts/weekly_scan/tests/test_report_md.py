"""単体試験: Markdown 報告生成。入力の数値のみが出力に現れることを確認する。"""

import os

from scripts.weekly_scan.report.report_md import render_md, write_md


def _rec():
    return {
        "analysis_id": "screen-ピール値-20261104",
        "kind": "strong_candidate",
        "result_item": "ピール値",
        "what_we_saw": ["品名A/B/Cで一貫して treated が高い"],
        "whats_missing": ["確認用ロットが各群5に未達の層あり"],
        "numbers": {"pooled_delta": 0.18, "n_total": 36, "strength": 0.73},
        "strata": [
            {"product": "A", "n_treated": 10, "n_control": 8,
             "mean_treated": 1.2, "mean_control": 1.0, "delta": 0.2},
        ],
        "proposal": {"change": "ラインL1に揃える", "experiment": "前向きに20ロット",
                     "confirm_design": "品名層別で判定器に通す"},
        "tracking": {"next_trigger": "各群5到達", "class": "C"},
    }


def test_render_md_contains_sections_and_numbers():
    md = render_md(_rec())
    assert "# 週次走査 検出報告: ピール値" in md
    assert "## 何を見てそう判断したか" in md
    assert "## 何が足りないか" in md
    assert "## 改善の提案" in md
    assert "## 追跡項目" in md
    assert "0.18" in md  # pooled_delta
    assert "36" in md    # n_total
    assert "ラインL1に揃える" in md


def test_render_md_does_not_invent_numbers():
    rec = _rec()
    md = render_md(rec)
    # 入力に無い数値（例: 99）が勝手に現れない
    assert "99" not in md
    # strata の delta がそのまま出る
    assert "0.2" in md


def test_write_md_creates_file(tmp_path):
    path = write_md(_rec(), str(tmp_path))
    assert os.path.isfile(path)
    body = open(path, encoding="utf-8").read()
    assert "ピール値" in body
