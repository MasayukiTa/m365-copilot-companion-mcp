"""処理 (d) 報告生成 — pptx 報告。

入力は report_md と同じ DetectionRecord。python-pptx が使える環境では pptx を生成し、
使えない環境では生成をスキップして理由を返す（CI・無依存環境でも落ちない）。

報告に書く数値は DetectionRecord.numbers / strata の値のみ。新たな数値を作らない。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

_KIND_LABEL = {
    "adopted": "採用（順序の成立した効果あり）",
    "strong_candidate": "強い候補（事前登録済み）",
}


def pptx_available() -> bool:
    try:
        import pptx  # noqa: F401
        return True
    except Exception:
        return False


def _fmt_num(v: Any) -> str:
    if isinstance(v, float):
        return ("%.6f" % v).rstrip("0").rstrip(".")
    return str(v)


def build_slide_spec(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """DetectionRecord -> スライド仕様（タイトルと本文行の配列）。

    pptx の有無に関わらず作れる純データ。テストはこれを検証すれば python-pptx 不要。
    """
    ri = rec.get("result_item", "")
    aid = rec.get("analysis_id", "")
    numbers: Dict[str, Any] = rec.get("numbers", {}) or {}
    proposal: Dict[str, Any] = rec.get("proposal", {}) or {}
    tracking: Dict[str, Any] = rec.get("tracking", {}) or {}

    num_lines = ["%s: %s" % (k, _fmt_num(numbers[k])) for k in sorted(numbers)]
    slides: List[Dict[str, Any]] = [
        {
            "title": "週次走査 検出報告: %s" % (ri or aid),
            "body": [
                "解析ID: %s" % aid,
                "区分: %s" % _KIND_LABEL.get(rec.get("kind", ""), rec.get("kind", "")),
                "結果項目: %s" % ri,
            ],
        },
        {"title": "数値（本走査の計算出力）", "body": num_lines or ["（数値なし）"]},
        {"title": "何を見てそう判断したか", "body": rec.get("what_we_saw", []) or ["（記載なし）"]},
        {"title": "何が足りないか", "body": rec.get("whats_missing", []) or ["（記載なし）"]},
        {
            "title": "改善の提案",
            "body": [
                "条件変更: %s" % proposal.get("change", "（記載なし）"),
                "実験案: %s" % proposal.get("experiment", "（記載なし）"),
                "確認設計: %s" % proposal.get("confirm_design", "（記載なし）"),
            ],
        },
        {
            "title": "追跡項目（台帳へ追加）",
            "body": [
                "分類: %s" % tracking.get("class", ""),
                "次の契機: %s" % tracking.get("next_trigger", ""),
            ],
        },
    ]
    return slides


def write_pptx(
    rec: Dict[str, Any], out_dir: str, filename: Optional[str] = None
) -> Tuple[Optional[str], str]:
    """DetectionRecord を pptx に書き出す。

    戻り値: (書き出したパス or None, 状態メッセージ)。
    python-pptx が無ければ (None, 理由) を返しスキップする。
    """
    if not pptx_available():
        return None, "python-pptx が無いため pptx 生成をスキップ（md 報告は生成済み）"
    from pptx import Presentation
    from pptx.util import Pt

    slides = build_slide_spec(rec)
    prs = Presentation()
    blank = prs.slide_layouts[6]
    title_only = prs.slide_layouts[5]
    for i, sp in enumerate(slides):
        layout = title_only if i == 0 else blank
        slide = prs.slides.add_slide(layout)
        left = prs.slide_width // 12
        top = prs.slide_height // 12
        width = prs.slide_width - 2 * left
        height = prs.slide_height - 2 * top
        tb = slide.shapes.add_textbox(left, top, width, height)
        tf = tb.text_frame
        tf.word_wrap = True
        tf.text = sp["title"]
        try:
            tf.paragraphs[0].runs[0].font.size = Pt(24)
        except Exception:
            pass
        for line in sp["body"]:
            p = tf.add_paragraph()
            p.text = str(line)
            try:
                p.runs[0].font.size = Pt(14)
            except Exception:
                pass
    os.makedirs(out_dir, exist_ok=True)
    name = filename or ("report_%s.pptx" % (rec.get("analysis_id", "report")))
    path = os.path.join(out_dir, name)
    prs.save(path)
    return path, "ok"


__all__ = ["pptx_available", "build_slide_spec", "write_pptx"]
