# -*- coding: utf-8 -*-
"""Assemble Skill proposals out of the ledger, and refuse when the ledger cannot support one.

WHAT THIS IS FOR. Every Skill in `skills/` was typed by a person at `/skill-create`. Work that
has been done twenty times and works is therefore worth exactly as much as somebody's spare
afternoon. This proposes the Skill instead, out of what the record actually shows, so that the
only human step left is the one that cannot be automated: deciding the output was good.

IT PROPOSES. IT DOES NOT INSTALL, AND IT NEVER EDITS AN EXISTING SKILL.
Two rules force that, and both were learned the hard way in this repository:

* A SKILL.md is human-edited text. Rewriting one to "improve" it destroys a decision somebody
  made, with no record that it was ever there.
* A Skill is trusted by the exact digest of its bundle (`relay/skills.py::confirm_approval`).
  Writing so much as one extra file into a trusted bundle flips it to `changed`, which revokes
  a working Skill as a side effect of having an opinion about it.

So proposals land in `.fleet/skill_proposals/`, outside skill discovery entirely. A new Skill
arrives as a complete bundle to be moved into `skills/` by a person, where it is discovered
UNTRUSTED and goes through the approval gate that already exists. An improvement to an existing
Skill arrives as a block of text to paste, next to the name of the file to paste it into.
Nothing here can make a Skill live, and that is the design, not a missing feature.

EVERY LINE IS QUOTED FROM THE RECORD. A proposal carries: how many times the work ran and how
many finished; the paths the instructions named; the output contract they stated; and the
segments that were present in an instruction that finished and absent from one that did not.
Nothing is paraphrased and no reason is supplied for why anything helped -- see
`tools/skill_lessons` for why a generated explanation of a difference is worse than no
explanation. Where there is nothing to quote, `refused` says so and no file is written.

IT WRITES GOAL TEXT, SO IT WRITES ONLY UNDER .fleet. Goal text is business content -- customer
names, telephone numbers, internal paths. `.fleet/` is git-ignored, as is `skills/`. Nothing
here may write into a tracked file, which is the rule this repository has broken three times.

    python -m tools.skill_draft                 # what could be proposed, and what was refused
    python -m tools.skill_draft --write         # write the proposals under .fleet
    python -m tools.skill_draft --show NAME
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import skill_lessons  # noqa: E402
from tools.skill_candidates import SKILLS_DIR, candidates, existing_skill_names  # noqa: E402

#: Where proposals go. Outside `skills/`, so nothing here is discovered, digested or trusted.
PROPOSALS_DIR = os.path.join(REPO, ".fleet", "skill_proposals")

#: A proposal needs at least this much quotable grounding. A "Skill" that says only "this work
#: happens a lot" is a note, and a store full of notes is what makes a skill catalogue useless
#: to search -- the failure the operator named when they said a catalogue of 10,000 names cannot
#: be handed to a model.
_PATH = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|、。，]+")
_CONTRACT_MARKS = ("出力形式", "1行1名", "列のみ", "最後の行に", "厳守", "の形式", "だけ出力",
                   "出力は", "形式だけ", "DONE")


def _paths_in(text):
    seen, out = set(), []
    for p in _PATH.findall(text or ""):
        p = p.rstrip(".,、。")
        low = p.replace("\\", "/").lower()
        if low not in seen:
            seen.add(low)
            out.append(p)
    return out


def _contracts_in(text):
    return [m for m in _CONTRACT_MARKS if m in (text or "")]


def _slug(text):
    """A bundle name from the work itself: ascii, hyphenated, short, and stable.

    Japanese goals give no ascii words, so the fallback is a digest -- an opaque name a person
    will rename. That is better than transliterating, which invents a spelling nobody chose and
    which changes the day the transliteration table does.
    """
    wordish = re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", text or "")
    stop = {"the", "and", "for", "with", "from", "this", "that", "you", "your", "please"}
    keep = [w.lower() for w in wordish if w.lower() not in stop][:4]
    if len(keep) >= 2:
        return "-".join(keep)
    import hashlib
    return "work-" + hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:8]


def _applicable_lessons(goal, lessons, min_similarity=0.35):
    """Lessons drawn from work that resembles this one.

    DELIBERATELY LOOSER THAN THE PAIRING THRESHOLD. Pairing asks "are these the same job?", and
    must be strict or it compares unrelated work. This asks "was this lesson learned somewhere
    near here?", where a lesson about which search tool to use is worth carrying across jobs
    that share little but their setting. The tradeoff is visible: each lesson is printed with
    the similarity that admitted it, so an operator reading a proposal can throw one out.
    """
    w = skill_lessons.words(goal)
    out = []
    for p in lessons:
        sim = skill_lessons.similarity(w, skill_lessons.words(p["goal_worked"]))
        if sim < min_similarity:
            continue
        for seg in p["added"]:
            out.append({"text": seg, "similarity": round(sim, 2)})
    # Strongest evidence first, and never the same advice twice.
    out.sort(key=lambda x: -x["similarity"])
    kept, seen = [], []
    for item in out:
        wv = skill_lessons.words(item["text"])
        if len(wv) < 3 or any(skill_lessons.similarity(wv, s) >= 0.6 for s in seen):
            continue
        seen.append(wv)
        kept.append(item)
    return kept[:8]


def _body(group, paths, contracts, lessons, name):
    """The proposal text. Sections are omitted rather than filled with a placeholder.

    A template with `TODO` in it gets approved with the TODO still in it. If a section has no
    evidence behind it, it is not in the file.
    """
    goal = group["examples"][0]
    lines = ["---", "name: %s" % name,
             'description: "PROPOSED DRAFT -- not reviewed. %s"'
             % goal[:160].replace('"', "'").replace("\n", " "),
             "---", "",
             "# %s （提案・未承認）" % name, "",
             "> このファイルは `tools/skill_draft` が台帳から組み立てた**提案**である。",
             "> 内容は過去の実行から引用しただけで、**出力が良かったことの保証は無い**",
             "> （台帳は `DONE` を実行側の自己申告としてしか持たない）。",
             "> 人が読み、直し、`skills/` に移して承認して初めて有効になる。", "",
             "## 実績", "",
             "- 同じ作業の実行回数: **%d**（うち完了 %d）" % (group["runs"], group["done"]),
             ""]

    lines += ["## 依頼文（実際に投入されたもの）", "", "```", goal.strip(), "```", ""]

    if paths:
        lines += ["## 参照先（過去の依頼文が名指ししていた場所）", ""]
        lines += ["- `%s`" % p for p in paths]
        lines += [""]

    if contracts:
        lines += ["## 出力の形（依頼文が指定していた語）", "",
                  "過去の依頼文はこれらを明示していた。これが満たされているかは機械的に確認できる。",
                  ""]
        lines += ["- `%s`" % c for c in contracts]
        lines += [""]

    if lessons:
        lines += ["## 失敗した実行に無く、完了した実行にあった文", "",
                  "同種の作業で、**行き詰まった依頼文には無く、完了した依頼文にはあった**部分。",
                  "なぜ効いたのかは台帳に書かれていないので、**理由は書かない**。原文のまま引用する。",
                  ""]
        for item in lessons:
            lines += ["- （類似度 %.2f） %s" % (item["similarity"],
                                             item["text"].strip().replace("\n", " "))]
        lines += [""]

    lines += ["## 人が決めること", "",
              "1. 上の依頼文を、**短い指示で再現できる手順**に書き直す。",
              "2. 理想の出力がどれかを決める（台帳はこれを持っていない）。",
              "3. `skills/%s/` に置き、承認する。" % name, ""]
    return "\n".join(lines)


def propose(ledger=None, skills_dir=SKILLS_DIR, include_benchmarks=False):
    """Every Skill the record can support, and every one it cannot, with the reason.

    THE REFUSALS ARE RETURNED, NOT DROPPED. A generator that silently emits only what it managed
    is impossible to judge: nobody can tell a corpus with little in it from a generator that is
    quietly failing. The counts of both are what makes this measurable.
    """
    got = candidates(include_benchmarks=include_benchmarks)
    lessons = skill_lessons.pairs(ledger=ledger, include_benchmarks=include_benchmarks)
    have = {n.lower() for n in existing_skill_names(skills_dir)}

    out = {"new": [], "amend": [], "refused": [],
           "lesson_pairs": len(lessons), "candidates": len(got["qualified"])}

    for group in got["qualified"]:
        goal = group["examples"][0]
        paths = _paths_in(" ".join(group["examples"]))
        contracts = _contracts_in(" ".join(group["examples"]))
        applicable = _applicable_lessons(goal, lessons)
        name = _slug(goal)

        if not paths and not contracts and not applicable:
            out["refused"].append({
                "name": name, "runs": group["runs"], "goal": goal,
                "why": "依頼文が場所も出力の形も指定しておらず、類似作業の教訓も無い。"
                       "引用できるものが無いので手順書は組めない。"})
            continue

        proposal = {"name": name, "runs": group["runs"], "done": group["done"], "goal": goal,
                    "paths": paths, "contracts": contracts, "lessons": applicable,
                    "body": _body(group, paths, contracts, applicable, name)}
        if name in have:
            proposal["existing"] = name
            out["amend"].append(proposal)
        else:
            out["new"].append(proposal)

    out["new"].sort(key=lambda p: -p["runs"])
    out["amend"].sort(key=lambda p: -p["runs"])
    out["refused"].sort(key=lambda p: -p["runs"])
    return out


def write(result, directory=PROPOSALS_DIR):
    """Write proposals under .fleet. Returns the paths written.

    A proposal is rewritten every run rather than versioned: it is derived entirely from the
    ledger, so the current one is the only one that is true, and a directory of stale proposals
    is a directory nobody reads.
    """
    written = []
    for proposal in result["new"]:
        folder = os.path.join(directory, proposal["name"])
        try:
            os.makedirs(folder)
        except OSError:
            pass
        path = os.path.join(folder, "SKILL.md")
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(proposal["body"])
        written.append(path)

    for proposal in result["amend"]:
        # NEVER into the bundle: an added file changes its digest and revokes its trust. This is
        # a block of text with the name of the file a person may choose to paste it into.
        path = os.path.join(directory, "%s.amend.md" % proposal["name"])
        try:
            os.makedirs(directory)
        except OSError:
            pass
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# `skills/%s/SKILL.md` への追記案（自動では反映しない）\n\n"
                     "既存の Skill は人が書いたものなので上書きしない。"
                     "束の中にファイルを増やすと digest が変わり、**承認済みの Skill が"
                     "`changed` に落ちて失効する**ので、ここに置くだけにしてある。\n\n"
                     % proposal["name"])
            fh.write(proposal["body"])
        written.append(path)

    stamp = os.path.join(directory, "last_run.txt")
    try:
        with io.open(stamp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("%s\nnew=%d amend=%d refused=%d lesson_pairs=%d\n"
                     % (time.strftime("%Y-%m-%d %H:%M:%S"), len(result["new"]),
                        len(result["amend"]), len(result["refused"]), result["lesson_pairs"]))
    except OSError:
        pass
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true", help="write proposals under .fleet")
    ap.add_argument("--all", action="store_true", help="include benchmark traffic")
    ap.add_argument("--show", help="print one proposal by name")
    args = ap.parse_args(argv)

    result = propose(include_benchmarks=args.all)

    if args.show:
        for proposal in result["new"] + result["amend"]:
            if proposal["name"] == args.show:
                print(proposal["body"])
                return 0
        print("no proposal named %r" % args.show)
        return 2

    print("qualified past work: %d    failure/success pairs available as lessons: %d"
          % (result["candidates"], result["lesson_pairs"]))
    print("  proposals for new Skills : %d" % len(result["new"]))
    print("  additions to existing    : %d" % len(result["amend"]))
    print("  refused for lack of evidence: %d" % len(result["refused"]))
    print()
    print("%-34s %-5s %-6s %-9s %s" % ("name", "runs", "paths", "contract", "lessons"))
    for proposal in result["new"][:20]:
        print("%-34s %-5d %-6d %-9d %d"
              % (proposal["name"][:34], proposal["runs"], len(proposal["paths"]),
                 len(proposal["contracts"]), len(proposal["lessons"])))
    if result["refused"]:
        print()
        print("refused (nothing to quote):")
        for proposal in result["refused"][:5]:
            print("  runs=%-4d %s" % (proposal["runs"], proposal["goal"][:64].replace("\n", " ")))

    if args.write:
        written = write(result)
        print()
        print("wrote %d files under %s" % (len(written), PROPOSALS_DIR))
        print("nothing is active: move a bundle into skills/ and approve it to use it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
