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

from tools import skill_candidates, skill_lessons  # noqa: E402
from tools.skill_candidates import SKILLS_DIR, existing_skill_names  # noqa: E402

#: Where proposals go. Outside `skills/`, so nothing here is discovered, digested or trusted.
PROPOSALS_DIR = os.path.join(REPO, ".fleet", "skill_proposals")

#: A proposal needs at least this much quotable grounding. A "Skill" that says only "this work
#: happens a lot" is a note, and a store full of notes is what makes a skill catalogue useless
#: to search -- the failure the operator named when they said a catalogue of 10,000 names cannot
#: be handed to a model.
_PATH = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|、。，]+")
#: Phrases by which an instruction declares the SHAPE OF ITS ANSWER. Having one is part of what
#: makes a job worth writing a procedure for, so what counts here decides what gets proposed.
#:
#: A COMPLETION MARKER IS NOT AN OUTPUT CONTRACT, and treating it as one let every probe through.
#: `DONE` and `最後の行に` were in this list; both come from how a fleet goal ENDS, not from what
#: it asks for. `relay/control_markers.CLOSING_INSTRUCTION` asks every worker for a final DONE
#: line, and an operator writing a goal by hand mirrors it. Measured over the 157 qualified
#: candidates: 52% carried `DONE` and 13% carried nothing else -- so the sieve was passing them
#: on the strength of the protocol's own boilerplate.
#:
#: What that cost was visible in the ranking. The most-run "work" included 「次の足し算の答えを
#: 数字だけで書いてください: 137 + 486」 (52 runs), a documents listing (62) and a filename
#: listing (40) -- smoke probes, not jobs anybody needs a procedure for. Dropping the two
#: markers took the proposals from 95 to 57 and removed every probe; what remains is the
#: telephone-directory classification, the furigana check, the OGF report and the mail searches.
_CONTRACT_MARKS = ("出力形式", "1行1名", "列のみ", "厳守", "の形式", "だけ出力",
                   "出力は", "形式だけ")


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
    """An OPAQUE bundle name. Deliberately not derived from the goal, and this is the point.

    The first version built the name from the goal's ascii words, which read as an obvious
    convenience. On the first real run -- before anything was written to disk -- the top
    proposal's name was the operator's home-directory path turned into a slug, so it carried
    the employee id and the company name side by side, and three more were colleagues'
    surnames taken from a telephone-directory job. The names themselves are not reproduced
    here: writing a leaked identifier into a tracked file in order to explain that it must not
    be written into a tracked file is the same mistake wearing a lesson's clothes, and the
    naming gate caught this paragraph doing exactly that.

    A proposal's name becomes a DIRECTORY name. Directory names appear in listings, in shell
    history, in `git status`, and are one `git add` away from a public repository -- the exact
    transcription this repository has already done three times, most recently putting an
    employee id into a public history that then had to be rewritten.

    A blocklist cannot fix it. The configured secret words catch the company name and the
    employee-id shape; nothing catches a colleague's surname, and goal text is full of them.
    Since no filter over business text can be shown to be complete, the name is not taken from
    business text at all. The readable version lives INSIDE the file, which a person reads
    before deciding anything, and renaming the bundle is part of that decision.
    """
    import hashlib
    return "work-" + hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:10]


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


#: How much of an instruction a proposal quotes before it says what it left out.
#:
#: A PROCEDURE IS NOT A PAYLOAD. Generated from the real ledger, the largest proposal came to
#: 19KB, and almost all of it was a reference table the operator had pasted INTO the instruction
#: -- several hundred colleagues with their mobile numbers. Two things are wrong with carrying
#: that forward. A draft nobody will read is a draft nobody will approve; and a Skill is meant
#: to be handed around (the operator's own words: send it over Teams if you need to), so a
#: procedure that has a staff directory stapled to it travels with one.
#:
#: The cut is on LENGTH rather than on recognising a directory, because recognising "this looks
#: like personal data" is exactly the judgement that cannot be shown to be complete -- the same
#: reason `_slug` is a digest. Length is a property of the text, not a guess about it, and an
#: instruction past this length has stopped being a procedure whatever it contains.
QUOTED_INSTRUCTION_CHARS = 1200


def _quote_instruction(goal):
    """The instruction as a proposal quotes it: readable, and honest about what it dropped."""
    text = (goal or "").strip()
    if len(text) <= QUOTED_INSTRUCTION_CHARS:
        return text
    return (text[:QUOTED_INSTRUCTION_CHARS].rstrip()
            + "\n\n… 以下 %d 文字を省略。" % (len(text) - QUOTED_INSTRUCTION_CHARS)
            + "この長さの大半は、依頼文に貼り付けられた参照データ（名簿・対象行の一覧など）である"
            + "ことが多い。**手順書に載せるのはデータではなく、データの渡し方**。"
            + "元の全文は台帳にあり、ここには引かない。")


def _frontmatter(name, description):
    """The `name:` and `description:` lines, EMITTED BY THE PARSER THAT WILL READ THEM BACK.

    The description used to be pasted between double quotes by hand, with `"` swapped for `'`
    and nothing else escaped. A YAML double-quoted scalar treats backslash as an escape, so an
    instruction naming a Windows path the usual way (`D:\\共有\\経理\\...`) produced `\\共`,
    `\\経`... -- invalid escapes -- and the store refused the proposal on import. Those are
    exactly the jobs `_PATH` exists to find. Quoting by hand is a second YAML serialiser that
    has to be complete; `yaml.safe_dump` is the serialiser the store's `yaml.safe_load` is the
    inverse of, so whatever the instruction contains (backslashes, quotes, `:`, `#`, tabs)
    round-trips to the same string. `width` is unbounded so the value is never folded.
    """
    import yaml
    return yaml.safe_dump({"name": name, "description": description}, allow_unicode=True,
                          sort_keys=False, default_flow_style=False,
                          width=float("inf")).rstrip("\n")


def _body(group, paths, contracts, lessons, name):
    """The proposal text. Sections are omitted rather than filled with a placeholder.

    A template with `TODO` in it gets approved with the TODO still in it. If a section has no
    evidence behind it, it is not in the file.
    """
    # THE PART A PERSON WROTE, not the composed string that was sent. See
    # `skill_lessons.operator_instruction` for what quoting the whole thing produced.
    goal = skill_lessons.operator_instruction(group["examples"][0])
    lines = ["---", _frontmatter(name, "PROPOSED DRAFT -- not reviewed. "
                                 + goal[:160].replace("\n", " ")),
             "---", "",
             "# %s （提案・未承認）" % name, "",
             "> このファイルは `tools/skill_draft` が台帳から組み立てた**提案**である。",
             "> 内容は過去の実行から引用しただけで、**出力が良かったことの保証は無い**",
             "> （台帳は `DONE` を実行側の自己申告としてしか持たない）。",
             "> 人が読み、直し、`skills/` に移して承認して初めて有効になる。", "",
             "## 実績", "",
             "- 同じ作業の実行回数: **%d**（うち完了 %d）" % (group["runs"], group["done"]),
             ""]

    lines += ["## 依頼文（実際に投入されたもの）", "", "```", _quote_instruction(goal), "```", ""]

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


#: How much of a Skill's own vocabulary a job must share before the job is treated as work that
#: Skill already covers. Low, because a SKILL.md is mostly procedure prose while a goal is mostly
#: request -- the two overlap on the subject and little else. What protects against a wrong match
#: is not the number but the consequence: a wrong match produces a suggested ADDITION for a
#: person to read beside the named file, which they discard in a second. A missed match produces
#: a duplicate Skill proposal, which is worse, because two Skills for one job is how a catalogue
#: stops being searchable.
SKILL_MATCH_SIMILARITY = 0.12


def _skill_texts(skills_dir=None):
    """{skill name: its SKILL.md}, for matching work against what is already written down."""
    if skills_dir is None:
        skills_dir = SKILLS_DIR
    out = {}
    for name in existing_skill_names(skills_dir):
        path = os.path.join(skills_dir, name, "SKILL.md")
        try:
            out[name] = io.open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
    return out


def matching_skill(goal, texts):
    """The existing Skill this work belongs to, or "".

    MATCHED ON CONTENT, NOT ON NAME, and the difference is the whole feature. Bundle names became
    opaque digests the moment they were found to be carrying an employee id and colleagues'
    surnames (see `_slug`), so the previous check -- "is this proposal's name already a directory
    in skills/" -- could never be true again and the amendment path was structurally dead: 0
    additions proposed, every run, with five Skills sitting right there.

    That path is the one the operator actually asked for: "skills は同じ作業なのでエージェントが
    協力し合ってどんどん良い skills にしていくのが理想". A lesson learned on today's run of work a
    Skill already covers belongs in THAT Skill, not in a sixth proposal beside it.
    """
    w = skill_lessons.words(goal)
    best, score = "", 0.0
    for name, text in (texts or {}).items():
        sim = skill_lessons.similarity(w, skill_lessons.words(text))
        if sim > score:
            best, score = name, sim
    return best if score >= SKILL_MATCH_SIMILARITY else ""


def propose(ledger=None, skills_dir=None, include_benchmarks=False):
    """Every Skill the record can support, and every one it cannot, with the reason.

    THE REFUSALS ARE RETURNED, NOT DROPPED. A generator that silently emits only what it managed
    is impossible to judge: nobody can tell a corpus with little in it from a generator that is
    quietly failing. The counts of both are what makes this measurable.

    ONE LEDGER FOR BOTH READERS, and the defaults are read NOW, not when this module was
    imported. `ledger` used to reach skill_lessons.pairs() only; the candidates -- the thing a
    proposal is made of -- were read through `candidates()`'s own default, bound at import to
    the operator's real .fleet/socket_route.jsonl. So propose(ledger=X) proposed from the live
    ledger whatever X was, and a redirect of skill_candidates.LEDGER could not reach it either.
    `None` means "the module's current value", looked up at call time, for the ledger and for
    skills_dir alike.
    """
    if ledger is None:
        ledger = skill_candidates.LEDGER
    if skills_dir is None:
        skills_dir = SKILLS_DIR
    got = skill_candidates.candidates(ledger=ledger, include_benchmarks=include_benchmarks)
    lessons = skill_lessons.pairs(ledger=ledger, include_benchmarks=include_benchmarks)
    skill_texts = _skill_texts(skills_dir)

    out = {"new": [], "amend": [], "refused": [], "merged": 0,
           "lesson_pairs": len(lessons), "candidates": len(got["qualified"])}

    # ONE PROPOSAL PER INSTRUCTION, MERGED BEFORE ANYTHING IS WRITTEN.
    #
    # `skill_candidates` groups by the normalised goal, which is the text that was SENT; a
    # fan-out parent and its children therefore land in different groups even though the
    # operator wrote one instruction. Stripping our own composition (operator_instruction)
    # collapses them back together -- and since the bundle name is a digest OF that instruction,
    # the collapsed groups then compete for one filename. Measured: 10 amendments proposed, 5
    # files on disk. Five were silently overwritten by the other five.
    #
    # They are the same work, so they are merged rather than renamed apart: the run counts add
    # up, which is the number a person uses to decide whether a Skill is worth writing, and it
    # was being split across rows that each looked less used than the work really is.
    merged = {}
    for group in got["qualified"]:
        key = _slug(skill_lessons.operator_instruction(group["examples"][0]))
        if key in merged:
            prior = merged[key]
            prior["runs"] += group["runs"]
            prior["done"] += group["done"]
            prior["examples"] = list(prior["examples"]) + list(group["examples"])
            out["merged"] += 1
            continue
        merged[key] = dict(group)

    for group in merged.values():
        goal = skill_lessons.operator_instruction(group["examples"][0])
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
        existing = matching_skill(goal, skill_texts)
        if existing:
            proposal["existing"] = existing
            out["amend"].append(proposal)
        else:
            out["new"].append(proposal)

    out["new"].sort(key=lambda p: -p["runs"])
    out["amend"].sort(key=lambda p: -p["runs"])
    out["refused"].sort(key=lambda p: -p["runs"])
    return out


def write(result, directory=None):
    """Write proposals under .fleet. Returns the paths written.

    A proposal is rewritten every run rather than versioned: it is derived entirely from the
    ledger, so the current one is the only one that is true, and a directory of stale proposals
    is a directory nobody reads.

    `directory=None` MEANS PROPOSALS_DIR AS IT IS NOW. The default used to be
    `directory=PROPOSALS_DIR`, evaluated once when this module was imported -- before
    conftest's redirect of PROPOSALS_DIR could run -- so write(result), which is exactly how
    scripts/selfimprove_driver.refresh_skill_proposals calls it, wrote the operator's real
    .fleet/skill_proposals even under pytest. Reading the module constant at call time is what
    makes the redirect (and any other override of it) reach the write.
    """
    if directory is None:
        directory = PROPOSALS_DIR
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

    # ONE FILE PER SKILL, CARRYING EVERY AMENDMENT FOR IT.
    #
    # NEVER into the bundle: an added file changes its digest and revokes its trust. This is a
    # block of text with the name of the file a person may choose to paste it into -- and the
    # name has to be the SKILL's, not the proposal's digest, or the reader is sent to a
    # `skills/work-<digest>/` that does not exist.
    #
    # And grouped, because naming them after the target made several amendments share one
    # filename and overwrite each other: five proposed, two files on disk, three gone without a
    # word. That is the third time in one sitting that this generator lost work to a shared
    # filename -- the same shape as the proposals that collapsed to one instruction, and as the
    # snapshots that overwrote each other in the OGF folder. A writer that can silently drop its
    # own output is not reporting what it did, so this one groups first and the count of files
    # matches the count of targets.
    by_target = {}
    for proposal in result["amend"]:
        target = proposal.get("existing") or proposal["name"]
        by_target.setdefault(target, []).append(proposal)

    for target, proposals in by_target.items():
        path = os.path.join(directory, "%s.amend.md" % target)
        try:
            os.makedirs(directory)
        except OSError:
            pass
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# `skills/%s/SKILL.md` への追記案 %d 件（自動では反映しない）\n\n"
                     "既存の Skill は人が書いたものなので上書きしない。"
                     "束の中にファイルを増やすと digest が変わり、**承認済みの Skill が"
                     "`changed` に落ちて失効する**ので、ここに置くだけにしてある。\n\n"
                     % (target, len(proposals)))
            for i, proposal in enumerate(proposals, 1):
                fh.write("\n\n---\n\n## 追記案 %d / %d\n\n" % (i, len(proposals)))
                fh.write(proposal["body"])
        written.append(path)

    # THE STAMP IS A NOTE, AND A NOTE MUST NOT COST THE WRITE. It read `result["lesson_pairs"]`
    # directly, so a caller handing over a result without that statistic lost every file this
    # function had just been asked to write -- the KeyError is raised after the proposals are
    # written but the exception still reaches the caller, who has no way to know what landed.
    # `.get` because a missing count is a missing count, not a reason to fail.
    stamp = os.path.join(directory, "last_run.txt")
    try:
        with io.open(stamp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("%s\nnew=%d amend=%d refused=%d merged=%d lesson_pairs=%d\n"
                     % (time.strftime("%Y-%m-%d %H:%M:%S"),
                        len(result.get("new") or []), len(result.get("amend") or []),
                        len(result.get("refused") or []), int(result.get("merged") or 0),
                        int(result.get("lesson_pairs") or 0)))
    except (OSError, TypeError, ValueError):
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
