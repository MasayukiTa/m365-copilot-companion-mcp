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
    # SHORT path (p00..p49) so deep repos (ansible) don't blow the Windows 260-char limit
    return os.path.join(WORK, "p%02d" % IDX[inst])


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
    import sys as _sys
    if REPO not in _sys.path:
        _sys.path.insert(0, REPO)
    _asked = (os.environ.get("SWE_BROKER") or "").strip().lower() in ("1", "on", "true", "yes")
    try:
        from relay import broker_client as _bc
        _routed = _bc.enabled()
    except ImportError as _exc:
        if _asked:
            raise SystemExit("SWE_BROKER is set but relay.broker_client could not be "
                             "imported (%s). Refusing to stage locally instead: that is the "
                             "silent fallback this check exists to prevent." % _exc)
        _routed = False
    if _routed:
        os.makedirs(wt, exist_ok=True)
        marker = (
            "This directory is an ADDRESS, not a checkout.",
            "The work for " + inst + " happens at /app inside its container; tools that",
            "name a path under here are translated by relay/fleet_tool_router.py.",
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
    ps = (r["problem_statement"] or "")[:6000]
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
            contract += req[:3000] + "\n"
        if iface:
            contract += "Interface:\n" + iface[:3000] + "\n"
    text = (
        "You are fixing a real bug in the open-source project **%s** (language: %s).\n"
        "The repository is checked out locally at:\n  %s\n"
        "Read and edit the source with the file tools (grep/glob/read_file/replace_in_file/write_file). "
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
            _merged = dict(_prev)
            _merged.update(wtmap)      # this batch's entries win for ids it re-staged
            wtmap = _merged
    except (OSError, ValueError):
        pass
    json.dump(wtmap, open(_wt_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("wrote %s with %d goals (+ pro_wt_map.json)" % (a.out, len(goals)))


if __name__ == "__main__":
    main()
