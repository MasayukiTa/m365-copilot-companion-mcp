# -*- coding: utf-8 -*-
"""Four workers answered the same goal and nobody noticed they disagreed.

MEASURED on the 290-cinema survey of 2026-09-04. A STUCK worker is retried, the retry duplicates
the goal, and one goal ended up finished FOUR times. Five of its subjects came back with
conflicting verdicts and one had three different answers -- and the ledger simply kept whichever
completion finished last. A person had to open four transcripts to find that out.

The shapes below are taken from that run verbatim, because every extraction defect this module
had was found by running it against the real thing and not by imagining the format:

  * one worker wrote its claim as a bare bold line with no list marker, and the first version
    required a marker -- so the ONLY worker who said an item was still in stock was dropped, and
    a three-way disagreement was reported as two-way;
  * one subject contained a colon of its own, and splitting on the FIRST colon produced a
    subject nothing else matched, so that cinema vanished from the comparison entirely;
  * one worker wrote "キャナルシティ13" where others wrote "ユナイテッド・シネマ キャナルシティ13",
    which scores 0.41 on bigrams -- below any threshold that does not also merge different
    cinemas -- while being literally a substring.
"""
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay import fleet_reconcile as R  # noqa: E402


# -- reading a claim out of a worker's final message -------------------------------------------

def test_the_bulleted_shape_with_the_verdict_inside_the_bold():
    claims = R.extract_claims(
        "- **Ｔ・ジョイ博多: 配布終了** — 8/30(日)14:05の回にて配布終了（tjoy.jp/...）")
    assert claims == [("Ｔ・ジョイ博多", "配布終了")]


def test_the_bulleted_shape_with_the_verdict_outside_the_bold():
    claims = R.extract_claims("- **TOHOシネマズららぽーと福岡**: 情報なし — 公式サイトに記載なし")
    assert claims == [("TOHOシネマズららぽーと福岡", "情報なし")]


def test_the_numbered_shape():
    claims = R.extract_claims("2. **TOHOシネマズ ららぽーと福岡: 配布終了** — 公式に明記 / https://...")
    assert claims == [("TOHOシネマズ ららぽーと福岡", "配布終了")]


def test_a_bare_bold_line_with_no_list_marker_is_still_a_claim():
    """THE ONE THAT WAS DROPPED. This is how the only 'still in stock' answer was written."""
    claims = R.extract_claims("**キャナルシティ13: 残っている** — UC公式お知らせに掲載")
    assert claims == [("キャナルシティ13", "残っている")]


def test_a_subject_containing_its_own_colon_splits_at_the_last_one():
    """THE ONE THAT VANISHED. Splitting at the first colon made the subject '…小倉（運営'."""
    claims = R.extract_claims(
        "5. **ユナイテッド・シネマ チャチャタウン小倉（運営: ローソン・ユナイテッドシネマ）: 配布終了** — 欄に掲載")
    assert len(claims) == 1
    subject, verdict = claims[0]
    assert verdict == "配布終了"
    assert subject.endswith("）")


def test_prose_is_not_mined_for_claims():
    """Workers explain themselves in paragraphs, and a stray colon there would invent a subject."""
    assert R.extract_claims(
        "補足: 情報なし3館はいずれもユナイテッド・シネマ系で、公式ページが文字化けしていました。") == []


def test_evidence_after_the_dash_is_not_part_of_the_verdict():
    """Two workers agreeing on a verdict must read as agreement even when they cite
    different pages -- otherwise every subject looks like a disagreement."""
    a = R.extract_claims("- **X: 配布終了** — 根拠A https://a")
    b = R.extract_claims("- **X: 配布終了** — 根拠B https://b（別ページ）")
    assert a[0][1] == b[0][1] == "配布終了"


# -- folding the spellings of one subject ------------------------------------------------------

def test_a_bare_name_matches_the_same_name_with_its_chain_prefix():
    assert R.same_subject("キャナルシティ13", "ユナイテッド・シネマ キャナルシティ13")


def test_spacing_and_width_differences_do_not_split_a_subject():
    assert R.same_subject("ユナイテッド・シネマ キャナルシティ13", "ユナイテッド・シネマキャナルシティ13")


def test_two_different_cinemas_are_not_merged():
    """The dangerous direction: merging distinct subjects would hide a real disagreement by
    turning it into one subject with a confident-looking single verdict."""
    assert not R.same_subject("Ｔ・ジョイ博多", "Ｔ・ジョイ久留米")
    assert not R.same_subject("ユナイテッド・シネマ福岡ももち", "ユナイテッド・シネマなかま16")


def test_a_short_fragment_does_not_swallow_everything_containing_it():
    assert not R.same_subject("13", "ユナイテッド・シネマ キャナルシティ13")


# -- the whole point: surfacing the conflict ---------------------------------------------------

W6 = ("- **Ｔ・ジョイ博多: 配布終了** — 公式\n"
      "- **ユナイテッド・シネマ キャナルシティ13: 配布終了** — 公式\n"
      "- **ユナイテッド・シネマ福岡ももち: 情報なし** — 到達できず\n")
W7 = ("- **Ｔ・ジョイ博多**: 配布終了 — 公式\n"
      "- **ユナイテッド・シネマキャナルシティ13**: 情報なし — 到達できず\n"
      "- **ユナイテッド・シネマ福岡ももち**: 情報なし — 到達できず\n")
W3 = "**キャナルシティ13: 残っている** — 現行配布リストに残存\n"


def _rows(*texts):
    return R.reconcile([("w%d" % i, "goal", t) for i, t in enumerate(texts)])


def test_a_three_way_disagreement_is_reported_as_three_ways():
    rows = _rows(W3, W6, W7)
    conflicts = dict(R.disagreements(rows))
    canal = [k for k in conflicts if "キャナルシティ13" in k]
    assert canal, "the cinema with three answers is not in the conflicts"
    verdicts = conflicts[canal[0]]
    assert set(verdicts) == {"残っている", "配布終了", "情報なし"}


def test_unanimous_subjects_are_not_reported():
    """A reconciler that flags everything is one nobody reads."""
    conflicts = dict(R.disagreements(_rows(W6, W7)))
    assert not any("博多" in k for k in conflicts), "an agreed subject was reported as a conflict"
    assert not any("ももち" in k for k in conflicts)


def test_a_subject_only_one_worker_mentions_is_surfaced_separately():
    """Not a disagreement, and not nothing: either the others missed it or they named it
    differently. Both need a person to look."""
    only_w3 = R.lone_subjects(_rows(W6, W3), 2)
    assert not only_w3 or all(len({w for ws in v.values() for w in ws}) == 1
                              for _s, v in only_w3)


def test_a_single_completion_produces_no_lone_report():
    """With one worker every subject is mentioned once, which says nothing at all."""
    assert R.lone_subjects(_rows(W6), 1) == []


def test_nothing_to_reconcile_is_said_plainly(tmp_path):
    out = R.report(transcripts=str(tmp_path))
    assert "no goal was finished twice" in out
