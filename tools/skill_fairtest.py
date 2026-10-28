# -*- coding: utf-8 -*-
"""Judge whether a generated Skill was earned fairly, not by cheating.

WHY THIS EXISTS. The operator's test for "past work has become a Skill" is not whether a
Skill FILE exists. It is this: hide the finished September report, do NOT let the agent read
the Skills that were authored for it, forbid a long briefing, and give it the kind of short
instruction a person actually types. If an equivalent artefact comes out of THAT, the Skill is
real. If it only comes out when the answer is in front of the model, or only when the prompt is
already a specification, the Skill has not been earned -- it has been cheated.

Two cheats are mechanical, so they can be caught here; the third cannot, and is named so that it
is never mistaken for passing.

1. THE ANSWER IN THE MATCHING FACE. A Skill is selected by its matching text alone -- name,
   description, when_to_use, keywords (relay.skills._skill_match_text) -- before its body is
   ever read (progressive disclosure). That text is meant to describe WHEN the procedure
   applies, not to carry the finished output. If the identifying words of the withheld report
   have been written into that face, the Skill is matched because the answer was pasted into its
   front door. `answer_leak` finds those words. It reads ONLY the matching face, exactly what
   the matcher reads, so it cannot be fooled by hiding the leak in the body: the body is not
   matched on and so is not where a leak would help.

2. THE PROMPT THAT IS ALREADY THE SPEC. "一般的な人間が打つ程度" -- a short, ordinary prompt --
   has to reach the Skill. A Skill whose matching face only fires for a paragraph-long,
   path-and-contract briefing has encoded the long prompt as its trigger, which is the "無駄に
   長いプロンプト" the operator called unfair. `fires_on_short_prompt` asks the REAL matcher
   (SkillStore.match, the confident tier) whether each short prompt selects the Skill. Nothing
   here re-implements the score; a fair test uses the same door the agent uses.

3. WAS THE OUTPUT ANY GOOD. Neither check above, nor any check, says the produced deck was
   equivalent to the withheld one. relay.skills and tools.skill_* are explicit that the only
   judge of an IDEAL output is a person approving one. `fairtest` returns `output_judged=False`
   always, so a caller cannot read a clean cheating report as "the artefact was equivalent".
   It says only: nothing here was rigged; now a human looks at the two decks.

This module READS. It does not write a Skill, install one, or move the withheld report; the
quarantine of the September material is the caller's to do and undo. Goal/report text is
business content, so nothing here copies it into a tracked file.

    python -m tools.skill_fairtest --help
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.skills import (  # noqa: E402
    _match_features,
    _skill_match_text,
)


def _face_text(skill) -> str:
    """The text a Skill is matched on, and nothing else -- the only surface a leak could help.

    `_skill_match_text` returns (name words, metadata text); both are matched on, so both are
    where the answer must not be. The body is deliberately excluded: it is never read before a
    match, so a token that appears only in the body cannot raise a score and is not a cheat.
    """
    name_words, meta_text = _skill_match_text(skill)
    return name_words + " " + meta_text


def answer_leak(skill, ideal_output: str, topic_vocab: str | None = None) -> list[str]:
    """The distinctive feature-tokens of `ideal_output` that have been written into the Skill's
    matching face. Empty when the face describes WHEN the Skill applies without quoting the
    answer.

    DISTINCTIVE, not merely shared. Every Skill's face shares ordinary topic words with any
    report on that topic -- the subject noun, the word "report", a product LINE name -- and sharing those is how
    matching is SUPPOSED to work: those same words are what a short ordinary prompt carries, so a
    face that did NOT have them could never fire. A leak is the report's own IDENTIFYING material
    appearing in the trigger: a product CODE, a customer name, a measured figure. The overlap is
    computed on the matcher's own feature space (via `_match_features`), so 'leak' means 'a token
    that would actually raise this Skill's match score for THIS report', not a coincidental
    substring.

    THE TOPIC VOCABULARY IS WHAT A FAIR FACE IS ALLOWED TO SHARE. `topic_vocab` is a short,
    generic way of naming this kind of task -- the words a person would type in a short prompt.
    Its features are subtracted from the report's before the overlap is taken, so a face that
    shares only those is not a cheat, while the report's codes and figures, which no generic
    description contains, remain and are caught. Defaults to `GENERIC_TASK_WORDS`; a caller with
    a known task passes the vocabulary of THAT task (see the test fixture) so the line between
    'topic' and 'answer' is drawn where the real work draws it, not guessed here.
    """
    report_feats = set(_match_features(ideal_output or "").keys())
    topic_feats = set(_match_features(topic_vocab if topic_vocab is not None
                                     else GENERIC_TASK_WORDS).keys())
    distinctive = report_feats - topic_feats
    face_feats = set(_match_features(_face_text(skill)).keys())
    return sorted(distinctive & face_feats)


#: The ordinary vocabulary of "make the monthly report deck": words any such task shares, which
#: a Skill's face is MEANT to carry and which therefore are not evidence of cheating. Subtracted
#: from the withheld report's features before looking for a leak, so that only the report's own
#: identifying material (codes, names, figures) can register as leaked. A CALLER WITH A KNOWN
#: TASK SHOULD PASS ITS OWN topic_vocab: this default names the task in the abstract, but the
#: specific product line or subject a short prompt would carry (the words a person
#: actually types) belongs in the topic for that task, not in the answer.
GENERIC_TASK_WORDS = (
    "monthly report deck slides chart data extract aggregate make build create summary"
)


def fires_on_short_prompt(store, skill_name: str, prompts) -> dict:
    """For each short prompt, whether the REAL confident matcher selects `skill_name`.

    Uses SkillStore.match -- the same door tools.skill_match (the agent) calls -- so a prompt
    'fires' here exactly when it would fire for the agent. A Skill that needs a paragraph to be
    reached will miss every short prompt, and `any_fires` is False: that is the long-prompt
    dependence the operator called unfair, caught without judging the output.
    """
    results = {}
    for p in prompts:
        got = store.match(p)
        results[p] = bool(got and got.get("name") == skill_name)
    return {"per_prompt": results, "any_fires": any(results.values())}


def fairtest(store, skill_name: str, ideal_output: str, short_prompts,
             topic_vocab: str | None = None) -> dict:
    """Did this Skill earn its match fairly? Reports the two mechanical cheats and refuses to
    pretend about the third.

    `fair` is True only when NOTHING was rigged: the matching face does not quote the withheld
    answer, AND at least one short ordinary prompt reaches the Skill through the real matcher.
    `output_judged` is always False -- whether the produced artefact equals the withheld one is
    a human approval step this module does not and must not replace.

    `topic_vocab`, if given, is the generic vocabulary of this task (what a short prompt carries)
    and is passed to `answer_leak` so that sharing topic words is not mistaken for a leak.
    """
    skill = store._model_invocable_trusted().get(skill_name)
    if skill is None:
        return {
            "skill": skill_name,
            "fair": False,
            "reason": "skill is not a trusted, model-invocable Skill",
            "cheating_tokens": [],
            "short_prompt": {"per_prompt": {}, "any_fires": False},
            "output_judged": False,
        }
    leaked = answer_leak(skill, ideal_output, topic_vocab)
    short = fires_on_short_prompt(store, skill_name, short_prompts)
    fair = (not leaked) and short["any_fires"]
    return {
        "skill": skill_name,
        "fair": fair,
        "cheating_tokens": leaked,
        "short_prompt": short,
        "output_judged": False,
    }


def _demo() -> int:
    print("skill_fairtest judges whether a generated Skill was matched fairly:")
    print("  answer_leak(skill, ideal_output)   distinctive report tokens in the matching face")
    print("  fires_on_short_prompt(store, name, prompts)  does a short prompt reach it")
    print("  fairtest(store, name, ideal, prompts)        both, plus output_judged=False")
    print("It reads only. Quarantining the September report, and judging the output, are not its"
          " job.")
    return 0


def main(argv=None) -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable help summary")
    args = ap.parse_args(argv)
    if args.json:
        print(json.dumps({
            "reads_only": True,
            "checks": ["answer_leak", "fires_on_short_prompt"],
            "output_judged": False,
        }, ensure_ascii=False))
        return 0
    return _demo()


if __name__ == "__main__":
    raise SystemExit(main())
