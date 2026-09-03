"""Stage SWE-bench Pro instances (multi-language, shallow, disk-safe) and build fleet goals.

Grader-non-leak: NO hidden-test acceptance gate -- the agent solves from the problem statement,
self-verifies with the repo's own test command, then declares DONE; patches are graded OFFLINE
on the eval host. Shallow fetch of the exact base_commit keeps each worktree small (one machine has only
~8GB free, so this is the same capacity discipline as grading).

  python bench/pro_stage_goals.py              # 4-language smoke (one per language)
  python bench/pro_stage_goals.py --all        # all 50
  python bench/pro_stage_goals.py --ids a,b,c
"""
import argparse, json, os, subprocess, shutil

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SW = os.path.join(REPO, ".fleet", "swe")
WORK = os.path.join(SW, "work")
#: WHICH SLICE. Overridable because the original 50 are all BURNED -- every one of them was
#: used in a measured run on 2026-06-24, and re-measuring on burned instances produces a score
#: that has already seen its own answers. The fresh draw lives beside it under its own name so
#: the two cannot be confused by anyone reading a path.
SLICE_FILE = os.environ.get("SWE_SLICE_FILE") or os.path.join(SW, "pro_slice50_full.json")
FULL = json.load(open(SLICE_FILE, encoding="utf-8"))
BY_ID = {r["instance_id"]: r for r in FULL}
TESTHINT = {"python": "pytest -x", "go": "go test ./...", "js": "npm test", "ts": "npm test"}


def run(cmd, cwd=None, timeout=900):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


IDX = {inst: i for i, inst in enumerate(sorted(BY_ID))}  # short, stable worktree names


def wt_for(inst):
    # SHORT path (p00..p49) so deep repos (ansible) don't blow the Windows 260-char limit,
    # PLUS six hex of the instance id, because the number alone means different things in
    # different slices.
    #
    # IDX is the instance's position in the slice file, so p01 was one instance under a
    # four-instance smoke slice and a different one under the fresh forty. Measured
    # 2026-08-31: a worker solving an ansible instance in p01 had its file reads routed into a
    # NodeBB container after the next run restaged, and reported in its own words that
    # respawn.py "does not exist in the container" before declaring the task impossible.
    # Nothing said a path had been reassigned under it.
    #
    # With the id in the name, a path from another slice simply does not appear in the current
    # map, and instance_for returns None -- which the router turns into a refusal. Refusing is
    # recoverable; silently addressing the wrong repository is not.
    import hashlib as _h
    tag = _h.sha256(inst.encode("utf-8")).hexdigest()[:6]
    return os.path.join(WORK, "p%02d_%s" % (IDX[inst], tag))


def stage(inst):
    r = BY_ID[inst]
    wt = wt_for(inst)
    url = "https://github.com/%s.git" % r["repo"]
    sha = r["base_commit"]

    # UNDER ROUTING THE LOCAL CLONE IS PURE COST.
    #
    # The container carries the instance's checkout at /app already -- that is what the image
    # IS -- so cloning the same repository onto this machine buys nothing and spends the disk
    # the run keeps running out of. What the local path is still needed for is ADDRESSING:
    # fleet_tool_router.instance_for() maps a working directory to the container that owns it,
    # so the directory has to exist and has to be in the map. It just does not have to have
    # a repository in it.
    # THE IMPORT IS NOT ALLOWED TO FAIL QUIETLY.
    #
    # Run as `python bench/pro_stage_goals.py`, sys.path[0] is bench/ and `import relay`
    # raises ImportError. Caught and turned into "routing is off", that produced four local
    # clones and four lines reading "ok" on a run whose whole point was that nothing should
    # be cloned here -- the switch was on, the operator was told it was on, and the work
    # happened locally anyway. Put the repository on the path, and if routing was ASKED for
    # and cannot be reached, stop instead of falling back to the behaviour being replaced.
    # BOTH IMPORT FORMS, because this file is run as `python bench/<script>.py` (sys.path[0]
    # is bench/, so `bench.` does not resolve) and imported as `bench.<script>` from the
    # tests. Getting this wrong is the same fail-open one level up.
    try:
        from bench.routing_switch import broker as _broker
    except ImportError:
        from routing_switch import broker as _broker
    _bc = _broker("pro_stage_goals")
    _routed = _bc is not None
    if _routed:
        os.makedirs(wt, exist_ok=True)
        marker = (
            "This directory is an ADDRESS, not a checkout.",
            "The work for " + inst + " happens at /app inside its container; tools that",
            "name a path under here are translated by bench/remote/fleet_tool_router.py.",
            "Reading this directory for the instance's source will find nothing, and",
            "that is not a sign the run failed.",
        )
        with open(os.path.join(wt, "ROUTED_TO_CONTAINER.txt"), "w", encoding="utf-8") as fh:
            fh.write(chr(10).join(marker) + chr(10))
        # AND THE CONTAINER ITSELF, here, because an address with nothing behind it fails at
        # the worker's first tool call rather than at staging, where the failure is legible.
        #
        # NETWORK DEFAULTS TO none, and not only for containment: these repositories are
        # public and the commit that fixes each instance is upstream, so a solver with egress
        # can fetch the answer. That is contamination, and it would raise the score. The
        # images are pre-built with their dependencies, so none is also usually sufficient;
        # SWE_NET=bridge is there for an instance that genuinely cannot build without it, and
        # a run that uses it should say so.
        net = os.environ.get("SWE_NET", "none")
        try:
            _bc.create(inst, "jefzda/sweap-images:" + r["dockerhub_tag"], network=net)
        except Exception as exc:
            return wt, "CONTAINER_FAIL: %s" % (str(exc)[:160],)
        return wt, "routed(net=%s)" % net

    if os.path.isfile(os.path.join(wt, ".git", "HEAD")):
        cur = run(["git", "rev-parse", "HEAD"], cwd=wt).stdout.strip()
        if cur == sha:
            return wt, "exists@base"
        shutil.rmtree(wt, ignore_errors=True)
    os.makedirs(wt, exist_ok=True)
    run(["git", "init", "-q"], cwd=wt)
    run(["git", "config", "core.longpaths", "true"], cwd=wt)  # Windows long-path support
    run(["git", "remote", "add", "origin", url], cwd=wt)
    f = run(["git", "fetch", "--depth", "1", "origin", sha], cwd=wt)
    if f.returncode != 0:
        return wt, "FETCH_FAIL: " + f.stderr.strip()[-200:]
    co = run(["git", "checkout", "-q", "FETCH_HEAD"], cwd=wt)
    if co.returncode != 0:
        return wt, "CHECKOUT_FAIL: " + co.stderr.strip()[-200:]
    return wt, "ok"


def goal(inst, wt):
    r = BY_ID[inst]
    lang = r["repo_language"]
    # IF THIS EVER CUTS, IT SAYS SO. The behavioural contract one block below was being cut at
    # 3000 characters with no marker, mid-sentence, while the goal text told the worker the patch
    # was judged against it -- 4 of 40 on one slice, the longest field 5214 characters. This cap
    # is not currently reached (the longest problem statement measured is 3251), so it is left
    # alone rather than tuned; what is added is the marker, so the same defect cannot recur here
    # silently the way it did there.
    ps = (r["problem_statement"] or "")
    if len(ps) > 6000:
        ps = ps[:6000] + ("\n\n[... the issue text was truncated at 6000 characters; %d were "
                          "omitted. Say so if what you need is missing.]" % (len(ps) - 6000))
    th = TESTHINT.get(lang, "the project's own test command")
    # PUBLIC contract (requirements + interface): provided by the dataset as task input (NOT the
    # hidden tests). The hidden tests bind to THESE named symbols/behaviors -- a smoke miss (ansible)
    # solved the issue's prose but ignored 3 required exception classes the contract named. Passing
    # this is a domain-general, non-overfit strengthening (public fields; hidden tests still unseen).
    req = (r.get("requirements") or "").strip()
    iface = (r.get("interface") or "").strip()
    contract = ""
    if req or iface:
        contract = "\n== Required interface / behavioral contract (the patch is judged against THIS) ==\n"
        if req:
        # THE WHOLE CONTRACT, NOT THE FIRST 3000 CHARACTERS. The header above tells the
        # worker the patch is judged against this, and the steps below tell it to enumerate
        # EVERY symbol the contract names -- while the tail was being cut mid-sentence with
        # no marker that anything was missing. Measured on one slice: 4 of 40 contracts were
        # cut, the longest field ran to 4155 characters, and every graded instance among
        # them failed. The tail costs about a kilobyte on a prompt already four to seven.
            contract += req + "\n"
        if iface:
            contract += "Interface:\n" + iface + "\n"
    text = (
        "You are fixing a real bug in the open-source project **%s** (language: %s).\n"
        "The repository is checked out locally at:\n  %s\n"
        "Read and edit the source with the file tools (grep/glob/read_file/replace_in_file/write_file). "
        "ALWAYS pass working_dir set to that path when you run a command (shell_exec / run_python): "
        "those tools take a command rather than a path, and without working_dir they do not run in "
        "this repository. "
        "Fix ONLY the source to resolve the issue; do NOT edit test files.\n\n"
        "== Issue to fix ==\n%s\n%s\n"
        "How to proceed (the hidden tests bind to the PUBLIC CONTRACT above, not to any plausible implementation):\n"
        "1) INTERFACE-FIRST: enumerate EVERY public symbol the issue/contract names (new classes, exceptions, "
        "functions, options, attributes) and EVERY behavioral guarantee (exact statuses handled, success-after-retry, "
        "messages, defaults, public attributes). Make each a checklist item your patch MUST satisfy.\n"
        "2) Locate the relevant code; confirm the root cause by reading it.\n"
        "3) Implement TO THE CONTRACT using the codebase's IDIOMATIC error channel: if the contract says an operation "
        "must fail/error, RAISE the named exception (Py/JS/TS) or return the error (Go) -- do NOT merely warn-and-continue. "
        "Add ONLY the public surface the contract asks for (no extra/unrequested config options).\n"
        "4) Keep declaration files in lockstep: if you touch a doc-fragment / schema / .d.ts / proto / interface file, "
        "make it agree with the implementation and the contract.\n"
        "5) REPRODUCE-FIRST (do NOT skip -- running the project's existing tests is NOT enough: they pass on the buggy "
        "code too, so they cannot tell you your fix works). From the issue + contract, write a SHORT concrete "
        "reproduction (a throwaway script or direct call) exercising the described behavior for EVERY checklist item -- "
        "not just the first. Run it on the UNPATCHED code and confirm it fails exactly as the issue describes. Apply "
        "your fix, then run the SAME reproduction and confirm every item now behaves correctly (the named exception is "
        "actually raised/returned, the status is handled, the retry succeeds, ...). A partial pass means an INCOMPLETE "
        "fix -- keep going until the reproduction passes for ALL items. Also run the project's own tests (e.g. `%s`) to "
        "catch regressions; avoid real sleep/IO in retry/poll loops.\n"
        "6) Then DONE. (Hidden acceptance tests are graded OFFLINE; you will not see them -- your reproduction is your "
        "only proof, so make it cover every item the contract names.)\n"
        "Reply STUCK: <reason> only if you are certain it is unsolvable."
    ) % (r["repo"], lang, wt, ps, contract, th)
    return {"text": text, "cwd": wt, "checks": []}


def dirsize_mb(wt):
    sz = 0
    for dp, dn, fn in os.walk(wt):
        for f in fn:
            try:
                sz += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return sz // 1024 // 1024


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out", default=os.path.join(SW, "pro_goals.jsonl"))
    a = ap.parse_args()
    if a.all:
        ids = sorted(BY_ID)
    elif a.ids:
        ids = [i.strip() for i in a.ids.split(",") if i.strip()]
    else:
        seen = {}
        for r in sorted(FULL, key=lambda x: x["instance_id"]):
            seen.setdefault(r["repo_language"], r["instance_id"])
        ids = list(seen.values())
    os.makedirs(WORK, exist_ok=True)
    goals, wtmap = [], {}
    for inst in ids:
        wt, st = stage(inst)
        # "routed" IS a successful staging. It was not in this tuple, so a routed batch
        # would have produced zero goals while every line printed "routed" -- a run that
        # looks staged and submits nothing.
        mb = dirsize_mb(wt) if st in ("ok", "exists@base") else 0
        print("%-56s %-7s %5dMB  %s" % (inst[:56], BY_ID[inst]["repo_language"], mb, st))
        if st in ("ok", "exists@base") or st.startswith("routed"):
            goals.append(goal(inst, wt))
            wtmap[inst] = wt
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        for g in goals:
            f.write(json.dumps(g, ensure_ascii=False) + "\n")
    # MERGED, NOT OVERWRITTEN. This file is written once per BATCH and was replaced each
    # time, so by the end of a seven-batch run it named only the seventh batch -- and the
    # ledger join that reads it could therefore cover at most the last batch while the
    # recorder reported the whole 50 as measured. Earlier batches' exclusions and turn counts
    # were silently unavailable.
    _wt_path = os.path.join(SW, "pro_wt_map.json")
    try:
        with open(_wt_path, encoding="utf-8") as _fh:
            _prev = json.load(_fh)
        if isinstance(_prev, dict):
            # MERGE, BUT DROP WHAT NO LONGER EXISTS. Keeping every entry forever was the point
            # of merging -- a map holding only the last batch made earlier batches' evidence
            # unreachable -- but a worktree that has been discarded cannot be diffed or
            # attributed to, and its entry is pure noise. Measured: 15 entries of which 11
            # pointed at directories that were gone, so every later capture re-walked them and
            # every ledger lookup for them returned nothing.
            #
            # Dropping these loses nothing recoverable: the directory is what the patch would
            # have been read from. THIS BATCH'S entries are never dropped, because they are
            # about to be created and may not exist yet at this moment.
            _merged = {k: v for k, v in _prev.items()
                       if k in wtmap or os.path.isdir(str(v))}
            _merged.update(wtmap)      # this batch's entries win for ids it re-staged
            wtmap = _merged
    except (OSError, ValueError):
        pass
    json.dump(wtmap, open(_wt_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("wrote %s with %d goals (+ pro_wt_map.json)" % (a.out, len(goals)))


if __name__ == "__main__":
    main()
