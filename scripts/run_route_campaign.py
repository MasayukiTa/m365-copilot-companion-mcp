"""Run the transport A/B and record whatever comes out, including a refusal.

THE GOAL SET IS CHOSEN SO THE TWO VERSIONS CAN DIFFER

`transport/v1` returns SOCKET for everything; `transport/v2` sends Work IQ goals to a tab and
the rest to a socket. A goal set with no Work IQ in it makes the two versions the same program
on this data, and the run would return a confident null about a difference it never gave the
component a chance to express. So half the goals are Work IQ and half are not.

They are also small on purpose. This is the first measured experiment this loop has ever
completed; the thing being established is that a verdict can be reached at all, and a long run
on a machine with 200 MB of headroom would abort on the floor before it told us that.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay import provenance as PROV  # noqa: E402
from relay.selfimprove import ledger as L  # noqa: E402
from relay.selfimprove import manifest as M  # noqa: E402
from relay.selfimprove import scheduler as S  # noqa: E402

#: THE GOAL SET, REBUILT AFTER READING THE TRANSCRIPTS.
#:
#: The first set had two defects that nine campaigns did not reveal, because nothing read
#: what the arms actually said:
#:
#:   * A Teams goal asking what was decided in a meeting the operator did not organise. The
#:     delegated token only reaches /me, so the transcript is not retrievable and the model
#:     looped -- four identical replies, no DONE, no FAIL. It had looked like a success in
#:     every earlier run because the fleet was feeding the arm its own past notes, so the
#:     answer came from memory rather than from work. An unanswerable goal masked by memory
#:     is worse than a failing one: it reports DONE.
#:   * No acceptance checks. DONE meant "the marker appeared", so an arm could reach 4/4 by
#:     saying the right shape of words. Completion parity between the arms was being read off
#:     a signal that does not distinguish an answer from a performance.
#:
#: The coding goals now write a file and are checked by running it. The Work IQ goals stay on
#: what a delegated token can actually reach.
# REALPATH, BECAUSE `TEMP` IS AN 8.3 SHORT NAME HERE AND THE GOALS HAND IT TO THE HARNESS.
#
# Found in the multiturn set first and fixed there; this set had the same defect and kept it
# through eight measured runs. In one of those a tabs arm spent 33.2 minutes on a single turn
# of one file goal while the socket arm did the same run in 3.5, and a tabs-vs-tabs null came
# back 148.1 MB apart -- inside the range the treatment runs produced. A path the harness has
# to reconcile is variance the transport did not cause, and it lands on whichever arm draws it.
_OUT = os.path.join(os.path.realpath(os.environ.get("TEMP", ".")), "route_campaign_work")

GOALS = [
    {"text": "自分の今日以降の予定を3件、開始日時と件名だけの箇条書きで挙げて。"
             "取得できない場合はその理由を1行で書いて FAIL と出力して。"},
    {"text": "自分宛の受信メールのうち直近3件の件名だけを箇条書きで挙げて。"
             "取得できない場合はその理由を1行で書いて FAIL と出力して。"},
    {"text": "ファイル %s を作成し、文字列が回文かどうかを判定する関数 is_palindrome(s) を"
             "書いて。大文字小文字と空白は無視すること。"
             "書き終えたら python -c \"import sys;sys.path.insert(0,r'%s');"
             "from pal import is_palindrome;print(is_palindrome('A man a plan a canal Panama'))\" "
             "を実行し、True になることを確認して。"
             % (os.path.join(_OUT, "pal.py"), _OUT),
     "checks": [{"type": "file_exists", "path": os.path.join(_OUT, "pal.py")},
                {"type": "shell",
                 "cmd": "python -c \"import sys;sys.path.insert(0,r'%s');"
                        "from pal import is_palindrome;"
                        "assert is_palindrome('A man a plan a canal Panama');"
                        "assert not is_palindrome('hello')\"" % _OUT,
                 "expect_code": 0}]},
    {"text": "ファイル %s を作成し、1 から 100 までの整数のうち 3 と 5 の両方で"
             "割り切れるものを昇順に並べたリストを変数 FIZZBUZZ に入れて。"
             % os.path.join(_OUT, "fb.py"),
     "checks": [{"type": "file_exists", "path": os.path.join(_OUT, "fb.py")},
                {"type": "shell",
                 "cmd": "python -c \"import sys;sys.path.insert(0,r'%s');"
                        "from fb import FIZZBUZZ;"
                        "assert FIZZBUZZ==[15,30,45,60,75,90],FIZZBUZZ\"" % _OUT,
                 "expect_code": 0}]},
]

#: The workload this campaign runs. `--multiturn` swaps in the set with headroom.
#:
#: BOTH ARE KEPT AND THE CHOICE IS RECORDED. The old set is saturated -- 96.4% of its goals
#: finish in one turn -- so a comparison on it can only detect harm. That does not make it
#: worthless: every calibration measured so far was measured ON it, and a floor derived from
#: one workload says nothing about another. Deleting it would leave those numbers describing a
#: goal set nobody could look at.
def _code_revision():
    """HEAD's short hash plus a dirty marker, or "" if git cannot answer. Never raises."""
    import subprocess
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root,
                             capture_output=True, text=True, timeout=15)
        if rev.returncode != 0:
            return ""
        head = (rev.stdout or "").strip()
        st = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                            capture_output=True, text=True, timeout=20)
        dirty = bool((st.stdout or "").strip()) if st.returncode == 0 else True
        return head + ("+dirty" if dirty else "")
    except Exception:
        return ""


def active_goals(argv=None):
    """(goals, name, arm_reset). `arm_reset` runs between arms, or None if the set needs none.

    The name goes into the record so a later reader knows which set ran. The reset exists
    because a set that writes files leaves arm 2 looking at arm 1's finished work.
    """
    import sys as _sys
    argv = _sys.argv if argv is None else argv
    if "--multiturn" in argv:
        from scripts import workload_multiturn as _W
        _W.clean()
        return _W.goals(), "multiturn", _W.reset_outputs
    return GOALS, "saturated-v1", None


RESULTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "docs", "research", "results")
OUT = os.path.join(RESULTS, "route_campaign.json")


def main():
    version = sys.argv[1] if len(sys.argv) > 1 else "transport/v2"
    candidate_first = "--candidate-first" in sys.argv
    candidate = M.apply_genome(M.base_manifest(), {"components": {"transport": version}})
    agent_url = os.environ.get("MCP_FLEET_AGENT_URL", "")
    if not agent_url:
        for line in open(os.path.join(os.path.dirname(OUT), "..", "..", "..", ".env"),
                         encoding="utf-8", errors="ignore"):
            if line.startswith("MCP_FLEET_AGENT_URL="):
                agent_url = line.split("=", 1)[1].strip()
                break
    os.makedirs(_OUT, exist_ok=True)
    # Each campaign starts from an empty workspace. A file left by the previous run makes the
    # file_exists check pass without the arm doing anything.
    for name in ("pal.py", "fb.py"):
        try:
            os.remove(os.path.join(_OUT, name))
        except OSError:
            pass
    print("[campaign] candidate=%s" % version, flush=True)
    print("[campaign] agent=%s" % agent_url[:60], flush=True)

    exp = "route-%s%s-%s-%d" % (version.replace("/", "-"),
                              "-NULL" if "--null" in sys.argv else "",
                              "candfirst" if candidate_first else "ctrlfirst",
                              int(time.time()))
    led = L.HypothesisLedger()
    # BEFORE THE ARMS, NOT AFTER. A proposal written once the numbers are in is not a
    # prediction, and the ledger exists precisely because an automated proposer will
    # rationalise any outcome on request. The first campaign was run before this line
    # existed and is deliberately NOT backfilled -- see the results file.
    led.propose(
        experiment_id=exp,
        candidate_id=M.harness_id(candidate),
        parent_harness_id=M.harness_id(M.base_manifest()),
        target_failure_class="edge_memory_exhaustion",
        hypothesis=(
            "NULL RUN: both arms are the control. Any difference reported is the "
            "instrument's noise, and it is what the decision threshold has to clear."
            if "--null" in sys.argv else
            "Sending goals the fixed Work IQ predicate clears over a socket instead "
            "of a tab lowers peak Edge memory without losing completions, because a "
            "socket carries the conversation without a renderer."),
        changed_components={"transport": version},
        predicted_effect={"peak_edge_mb": "-300 or better", "done": "unchanged"},
        possible_regressions=["a goal that needed a tab falls back and costs a turn",
                              "Work IQ answers formed without Work IQ context"],
        evaluation_plan={
            "arms": "control=tabs everywhere under the base manifest; "
                    "candidate=the route under this manifest",
            "goals": len(GOALS),
            "measured": ["peak Edge memory (a rise over the arm's own start)",
                         "wall clock", "goals reaching DONE", "fallbacks"],
            "rule": "route_evaluator.decide: DONE loss -> reject; >=300 MB gain at equal "
                    "DONE -> keep; otherwise inconclusive",
            "known_bias": "arms run in sequence, so the second inherits the first's Edge "
                          "residue; start_mb is recorded per arm",
        },
        # The key is "authority", and anything else falls through to EXTERNAL_UNTRUSTED --
        # fail-closed, which is why the first attempt at this line was refused. The weakest
        # item decides, so every one has to stand on its own.
        evidence=[
            {"source": "docs/research/results/route_campaign.json",
             "authority": PROV.MACHINE_VERIFIER,
             "note": "first campaign, measured: both arms 4/4 DONE, 0 fallbacks, "
                     "control peak +364.6 MB vs candidate +569.0 MB, floor broke at 952 MB"},
            {"source": "operator, 2026-08-21",
             "authority": PROV.OPERATOR_INSTRUCTION,
             "note": "the memory floor for this machine is 512 MB"},
        ],
    )
    goals, goals_name, arm_reset = active_goals()
    print("[campaign] goals: %s (%d)" % (goals_name, len(goals)), flush=True)
    # CONCURRENCY IS PART OF THE MEASUREMENT, NOT A THROUGHPUT KNOB.
    #
    # The dependent variable is how much memory a run of these goals costs, and that depends on
    # how many workers are alive at once. Changing it changes what the number means, so it is
    # recorded with the result and `run_archive` refuses to put two different settings in one
    # column -- the same rule the goal set and the sampler already live under.
    max_conc = int(os.environ.get("MCP_FLEET_MAX_CONCURRENT", "2"))
    # WHICH BROWSER IS PART OF THE MEASUREMENT, NOT A CONNECTION DETAIL.
    #
    # The fleet's own Edge holds a resident Copilot page that belongs to no arm; its top mover
    # swung 24 to 239 MB across arms while each arm's own new process stayed at 18-20. A
    # dedicated evaluation browser has no such co-tenant. Runs from the two are different
    # measurements, so the URL is recorded and `run_archive` keys columns on it.
    cdp_url = os.environ.get("MCP_FLEET_CDP_URL", "http://127.0.0.1:9222")
    evaluate = S.route_evaluator_for(goals, agent_url=agent_url, max_concurrent=max_conc,
                                     cdp_url=cdp_url,
                                     candidate_first=candidate_first,
                                     warmup="--warmup" in sys.argv,
                                     null_arm="--null" in sys.argv,
                                     # BOTH ARMS ON THE ROUTE. Needed whenever the observable
                                     # lives in `worker_done` rows, which socket_route.record
                                     # writes only while the route is enabled -- a tabs-only
                                     # null pass records nothing and looks normal doing it.
                                     control_socket="--socket-both" in sys.argv,
                                     transcript_dir=os.path.join(RESULTS, "tx", exp),
                                     arm_reset=arm_reset)
    t0 = time.time()
    out = evaluate(candidate, exp)
    out["wall_s"] = round(time.time() - t0, 1)
    out["version"] = version
    # THE CANDIDATE'S PROGRAM CAN CHANGE UNDER A MULTI-NIGHT SERIES. Another session edits this
    # tree while runs happen, so a socket arm on one night is not necessarily the socket arm of
    # the next. The revision is recorded per run: a series that spans a change to the route, the
    # fleet or the sampler is not one series, and without this nothing would say so.
    out["revision"] = _code_revision()
    out["max_concurrent"] = max_conc
    out["cdp_url"] = cdp_url
    out["sidepage_reserve"] = os.environ.get("SWE_SIDEPAGE_RESERVE", "1")
    out["goals"] = goals_name
    infra = out.get("infra") or {}
    if infra.get("aborted"):
        # INFRA_ABORT, never INCONCLUSIVE. "the harness broke" and "the change did nothing"
        # must not pool -- the ledger's docstring says so and this is the first run to test it.
        led.conclude(experiment_id=exp, verdict=L.INFRA_ABORT,
                     actual_effect={"control": out.get("control"),
                                    "candidate": out.get("candidate"),
                                    "min_free_mb": out.get("min_free_mb")},
                     infra_delta=1, note=infra.get("reason", ""))
    else:
        gate = out.get("gate") or {}
        verdict = {"keep": L.KEEP, "reject": L.REJECT}.get(gate.get("verdict"),
                                                           L.INCONCLUSIVE)
        led.conclude(experiment_id=exp, verdict=verdict,
                     actual_effect={"control": out.get("control"),
                                    "candidate": out.get("candidate"),
                                    "memory_gain_mb": out.get("memory_gain_mb")},
                     note=gate.get("reason", ""))
    out["ledger_experiment_id"] = exp
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    # One file per run. Overwriting would erase the mirrored-order run with its pair.
    per_run = os.path.join(RESULTS, "route_campaign_%s.json" % exp)
    with open(per_run, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=2), flush=True)
    print("[campaign] wrote %s" % OUT, flush=True)


if __name__ == "__main__":
    main()
