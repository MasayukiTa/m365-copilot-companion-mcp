"""Cross-dataset sentinel: the reward-hacking tripwire for the self-improvement loop.

A genome optimised against one grader/slice can post gains that are grader- or dataset-specific
rather than real capability (a confound / reward hacking; cf. project_swe_eval_host_confound and
SELF_GROWTH_L4_DESIGN.md section 5). The defence is a small FIXED *sentinel* set drawn from a
DIFFERENT distribution than the validation slice. A candidate that wins on the slice but REGRESSES
on the sentinel is flagged and MUST NOT be kept.

The sentinel is intentionally REUSED every iteration -- it is a regression *canary*, not a headline
score. It is therefore EXEMPT from the "burn after use" rule that governs validation instances
(guards.BurnedRegistry): burning it would defeat its purpose, since the whole point is to re-check
the same known-good set each iteration. Because it is reused, its pass-rate is NOT a capability
number and must NEVER be reported as one -- only its delta-vs-baseline (regressed / lost / gained)
is meaningful.
"""
from __future__ import annotations

import json
import os
from typing import Iterable

DEFAULT = os.path.join(os.path.dirname(__file__), "sentinel.json")


class Sentinel:
    """A small fixed canary set, json-backed, used to catch grader/dataset-specific gains.

    File schema (default relay/selfimprove/sentinel.json):
        {"instance_ids": [...], "baseline_resolved": [...], "note": str}

    baseline_resolved = the instance ids the current frozen scaffold resolves on the sentinel (the
    canary's "known good"). A later candidate that drops any of these is regressing -- a tripwire.
    """

    def __init__(self, path: str = DEFAULT):
        self.path = path
        self._members: list[str] = []
        self._baseline: list[str] = []
        self.note: str = ""
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self._members = list(data.get("instance_ids", []))
            self._baseline = list(data.get("baseline_resolved", []))
            self.note = data.get("note", "")

    def set_members(self, instance_ids: Iterable[str]) -> None:
        """Replace the sentinel membership (order preserved, de-duplicated)."""
        seen, out = set(), []
        for i in instance_ids:
            if i not in seen:
                seen.add(i)
                out.append(i)
        self._members = out

    def set_baseline(self, resolved_ids: Iterable[str]) -> None:
        """Record the frozen scaffold's known-good resolved set (clamped to members)."""
        members = set(self._members)
        seen, out = set(), []
        for i in resolved_ids:
            if i in members and i not in seen:
                seen.add(i)
                out.append(i)
        self._baseline = out

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        rec = {"instance_ids": self._members, "baseline_resolved": self._baseline, "note": self.note}
        with open(self.path, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False, indent=2) + "\n")

    def members(self) -> list[str]:
        return list(self._members)

    def baseline(self) -> set[str]:
        return set(self._baseline)

    def check(self, candidate_resolved: Iterable[str]) -> dict:
        """Compare a candidate's sentinel result against the frozen baseline.

        Only ids that are in members() are considered. A previously-passing canary that the
        candidate now fails is a regression (the tripwire); a newly-resolved canary is a gain.
        """
        members = set(self._members)
        baseline = self.baseline() & members
        candidate = {i for i in candidate_resolved if i in members}
        lost = [i for i in self._members if i in baseline and i not in candidate]
        gained = [i for i in self._members if i in candidate and i not in baseline]
        return {
            "regressed": len(lost) > 0,
            "lost": lost,
            "gained": gained,
            "n_members": len(self._members),
            "n_baseline": len(baseline),
            "n_candidate_on_sentinel": len(candidate),
        }


def sentinel_verdict(gate_keep: bool, sentinel_result: dict) -> dict:
    """Combine the significance-gate keep decision with the sentinel tripwire.

    keep iff the gate said keep AND the sentinel did not regress. If the gate said keep but the
    sentinel regressed, this is the reward-hacking tripwire: keep becomes False and the reason names
    the lost canaries (a gain that does not generalise to a different distribution).
    """
    regressed = bool(sentinel_result.get("regressed"))
    keep = bool(gate_keep) and not regressed
    if not gate_keep:
        reason = "gate did not keep; sentinel not decisive"
    elif regressed:
        lost = sentinel_result.get("lost", [])
        reason = ("gate kept but sentinel REGRESSED on %d canary(ies) [%s]; likely grader/"
                  "dataset-specific gain (reward-hacking tripwire) -> reverting"
                  % (len(lost), ", ".join(lost)))
    else:
        reason = "gate kept and sentinel held (no regression)"
    return {"keep": keep, "reason": reason}
