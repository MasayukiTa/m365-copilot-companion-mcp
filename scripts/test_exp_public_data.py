# -*- coding: utf-8 -*-
"""scripts/exp_public_data.py on a tiny generated tree: the goals split, the answers are right, the
grader tells a lost merge from a wrong child, and a changed file is noticed."""
import json
import os
import re
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import splittability as SP  # noqa: E402
from scripts import exp_public_data as D  # noqa: E402


@pytest.fixture(scope="module")
def made(tmp_path_factory):
    root = str(tmp_path_factory.mktemp("exp") / "data")
    rc = D.main(["make", "--root", root, "--rows", "2400", "--lags", "3", "--files-per-pkg", "3",
                 "--kb-per-file", "2", "--min-free-gb", "0"])
    assert rc == 0
    with open(os.path.join(root, "goals.json"), encoding="utf-8") as fh:
        return root, json.load(fh)


def test_twelve_goals_two_families(made):
    _root, goals = made
    assert [g["goal_id"] for g in goals] == D.SERIES_GOALS + D.TREE_GOALS
    assert {g["family"] for g in goals} == {"series", "tree"}
    assert all(len(g["items"]) == 6 and len(g["expected"]) == 6 for g in goals)


def test_every_goal_is_one_line_and_judged_split(made):
    _root, goals = made
    for g in goals:
        assert "\n" not in g["text"]
        v = SP.judge(g["text"])
        assert v.decision == SP.SPLIT, (g["goal_id"], v.decision, v.reason)


def test_goal_text_is_public_only(made):
    _root, goals = made
    banned = ["reso" + "nac", "ADAMS", "D" + "LP", "社員", "http://", "https://"]
    for g in goals:
        for w in banned:
            assert w.lower() not in g["text"].lower(), (g["goal_id"], w)


def test_filler_never_contains_a_counted_token():
    for w in D.FILLER_WORDS:
        for bad in D.FORBIDDEN_IN_FILLER:
            assert bad not in w


def test_tree_expected_values_match_an_independent_recount(made):
    root, goals = made
    tree = os.path.join(root, "tree")
    per = {}
    for pkg in D.PACKAGES:
        c = d = t = 0
        for dp, _dn, fns in os.walk(os.path.join(tree, pkg)):
            for fn in fns:
                for ln in open(os.path.join(dp, fn), encoding="utf-8").read().split("\n"):
                    c += bool(re.match(r"class ", ln))
                    d += bool(re.match(r"\s*def ", ln))
                    t += "todo" in ln.lower()
        per[pkg] = {"class": c, "def": d, "todo": t}
    for g in goals:
        if g["family"] != "tree":
            continue
        m = g["metric"]
        total = sum(v[m] for v in per.values())
        assert g["total"] == total
        for p, want in g["expected"].items():
            assert want == per[p][m] - total // 12


def test_series_expected_values_match_the_baseline_script_output(made):
    root, goals = made
    g = next(x for x in goals if x["goal_id"] == "s01")
    out = subprocess.run([sys.executable, "-X", "utf8", os.path.join(root, "series", "baseline.py"),
                          "--window", str(g["window"]), "--lags", "2"],
                         capture_output=True, check=True).stdout.decode("utf-8")
    th = {ln.split()[0]: int(ln.split()[1].split("=")[1]) for ln in out.splitlines()}
    rows = [ln.split(",") for ln in open(os.path.join(root, "series", "readings.csv"),
                                         encoding="utf-8").read().splitlines()[1:]]
    for st, want in g["expected"].items():
        assert want == sum(1 for _i, s, v in rows if s == st and int(v) > th[st])


def test_the_series_goal_names_the_exact_shared_command(made):
    root, goals = made
    g = goals[0]
    assert g["prefix_command"] in g["text"]
    assert os.path.join(root, "series") in g["prefix_command"]


def test_grade_separates_a_lost_merge_from_a_wrong_child():
    expected = {"S01": 100, "S02": 200, "S03": 300}
    final = "S01=100\nS02=999\n"
    kids = ["S02 は 200 でした", "S03=301 と数えた"]
    got = D.grade(expected, final, kids)
    assert got == {"S01": "ok", "S02": "merge_loss", "S03": "wrong"}
    assert D.grade(expected, final, []) == {"S01": "ok", "S02": "wrong", "S03": "wrong"}


def test_parse_answer_takes_commas_signs_and_the_last_value():
    assert D.parse_answer("pkg_a=-12\npkg_b = 1,234\npkg_a=-13") == {"pkg_a": -13, "pkg_b": 1234}


def test_verify_notices_a_changed_file(made):
    root, _goals = made
    assert D.verify_data(root) == (True, [])
    p = os.path.join(root, "series", "baseline.py")
    old = open(p, encoding="utf-8").read()
    try:
        open(p, "w", encoding="utf-8", newline="\n").write(old + "# edited\n")
        ok, bad = D.verify_data(root)
        assert not ok and bad == ["series/baseline.py"]
    finally:
        open(p, "w", encoding="utf-8", newline="\n").write(old)


def test_make_refuses_to_overwrite_and_refuses_when_disk_is_short(made, capsys):
    root, _goals = made
    assert D.main(["make", "--root", root, "--rows", "10", "--files-per-pkg", "1", "--kb-per-file", "1",
                   "--min-free-gb", "0"]) == 2
    assert D.main(["make", "--root", root + "_new", "--rows", "10", "--files-per-pkg", "1",
                   "--kb-per-file", "1", "--min-free-gb", "1e9"]) == 3


def test_check_command_prints_the_grade(made, tmp_path, capsys):
    root, goals = made
    g = goals[0]
    f = tmp_path / "final.txt"
    f.write_text("\n".join("%s=%d" % (k, v) for k, v in g["expected"].items()), encoding="utf-8")
    assert D.main(["check", "--root", root, "--goal", g["goal_id"], "--final", str(f)]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["ok"] == 6


def test_parse_answer_prefers_whole_answer_lines_over_numbers_quoted_in_prose():
    """A merge worker's final answer repeats station names with OTHER numbers in a breakdown line
    ('threshold: S07=5411, S08=5413'). 'The last value wins' then replaced the answers (0/6 and a
    false 'merge loss' of 6 in run 07a_s14, whose answer lines were all correct)."""
    text = "S07=57272\nS08=57514\n\n取得状況: 完了\nthreshold 内訳: S07=5411, S08=5413\nDONE"
    assert D.parse_answer(text) == {"S07": 57272, "S08": 57514}
    # a correction on its own line still wins over the earlier answer line
    assert D.parse_answer("S07=1\nS07=2") == {"S07": 2}
    # no whole-line answers at all: fall back to the in-text pairs
    assert D.parse_answer("結果は S07=5 と S08=6") == {"S07": 5, "S08": 6}
