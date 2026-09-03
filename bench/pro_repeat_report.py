# -*- coding: utf-8 -*-
"""How much of the resolve rate is the model, and how much is run-to-run noise?

WHY THIS EXISTS. Every scaffold intervention tried so far was judged by running it once and
comparing the rate to a single earlier run. That design cannot tell a real effect from the
harness disagreeing with itself, and nobody had measured how much it disagrees with itself.

Two runs of the IDENTICAL slice under the IDENTICAL configuration give that number directly.
The instances that change verdict between them are the noise; the ones that do not are signal.
From the discordant count this also states the smallest effect a future comparison could
detect, which is the number that decides whether the next experiment is worth running at all.

    python -m bench.pro_repeat_report --a .fleet/swe/pro_cycle_results.json \
                                      --b .fleet/swe/rep2_results.json
"""
from __future__ import annotations

import argparse
import math
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SW = os.path.join(REPO, ".fleet", "swe")

from bench import pro_ledger_report as PR   # noqa: E402


def verdicts(path, want):
    """{instance: True/False} for instances with a real verdict. Non-measurements are dropped."""
    out = {}
    for inst, row in PR.latest_rows(path).items():
        if want and inst not in want:
            continue
        v = str(row.get("verdict") or "").upper()
        if v == "RESOLVED":
            out[inst] = True
        elif v == "NOT":
            out[inst] = False
    return out


def mcnemar_detectable(discordant, alpha=0.05, power=0.80):
    """The smallest true difference a paired test could detect, given this much disagreement.

    Paired comparison only sees instances that CHANGE, so the discordant count is the entire
    sample size of the test. With b+c pairs split as p and 1-p, detecting a difference d in the
    rate needs roughly n_disc * d_frac where d_frac is the imbalance -- inverted here to report
    the effect size, in percentage points of the full slice, that reaches significance.
    """
    if discordant < 1:
        return None
    z_a, z_b = 1.96, 0.84
    # smallest imbalance k:(discordant-k) that clears the normal-approximation McNemar test
    for k in range(discordant, -1, -1):
        b, c = k, discordant - k
        if b + c == 0:
            continue
        chi = (abs(b - c) - 1) ** 2 / float(b + c)
        if chi < 3.841:                     # chi-square 1df at alpha=0.05
            return (b - c) + 1              # the next larger imbalance is the first detectable
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bench.pro_repeat_report",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("--a", default=os.path.join(SW, "pro_cycle_results.json"))
    ap.add_argument("--b", default=os.path.join(SW, "rep2_results.json"))
    ap.add_argument("--slice", default=os.path.join(SW, "pro_slice_repeat.json"))
    a = ap.parse_args(argv)

    want = PR._slice_ids(a.slice) or set()
    A, B = verdicts(a.a, want), verdicts(a.b, want)
    both = sorted(set(A) & set(B))
    if not both:
        print("両方に判定がある instance がありません（走行が終わっていない可能性）")
        print("  A: %d 件, B: %d 件" % (len(A), len(B)))
        return 1

    tt = [i for i in both if A[i] and B[i]]
    ff = [i for i in both if not A[i] and not B[i]]
    tf = [i for i in both if A[i] and not B[i]]
    ft = [i for i in both if not A[i] and B[i]]
    disc = len(tf) + len(ft)

    print("同一スライスを2回走らせた結果の一致 (%d 件で比較)" % len(both))
    print()
    print("  両方とも解けた           %3d" % len(tt))
    print("  両方とも解けなかった     %3d" % len(ff))
    print("  1回目だけ解けた          %3d   <- 反転" % len(tf))
    print("  2回目だけ解けた          %3d   <- 反転" % len(ft))
    print()
    print("  反転率 %d/%d = %.1f%%" % (disc, len(both), 100.0 * disc / len(both)))
    print("  A: %d/%d = %.1f%%   B: %d/%d = %.1f%%"
          % (sum(A[i] for i in both), len(both), 100.0 * sum(A[i] for i in both) / len(both),
             sum(B[i] for i in both), len(both), 100.0 * sum(B[i] for i in both) / len(both)))

    print()
    print("この反転率のもとで、対応のある比較が検出できる最小の効果:")
    k = mcnemar_detectable(disc)
    if k is None:
        print("  反転ゼロ。決定論的に見えるが、2回では確かめきれない")
    else:
        pts = 100.0 * k / len(both)
        print("  正味 %d 件 = %.1f ポイント以上でなければ有意にならない" % (k, pts))
        print("  （それ未満の効果は、この規模では走らせても『差なし』としか読めない）")

    print()
    print("常に解ける %d / 常に解けない %d / 揺れる %d"
          % (len(tt), len(ff), disc))
    print("  揺れる分を全て取れたときの上限: %.1f%%"
          % (100.0 * (len(tt) + disc) / len(both)))
    print("  「常に解けない」が実際の天井を決める。反復を増やすとこの数は減る方向にしか動かない")
    if ff:
        print()
        print("  2回とも解けなかった instance:")
        for i in ff[:8]:
            print("    %s" % i[:70])
        if len(ff) > 8:
            print("    ... 他 %d 件" % (len(ff) - 8))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
