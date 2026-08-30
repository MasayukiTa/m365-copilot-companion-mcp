"""Capture + cleanup half of the disk-safe batched Pro 50-run (the dev box has ~8GB free, so we
never stage all 50 at once). After a fleet batch finishes:
  1) git diff each worktree in pro_wt_map.json -> append {instance_id, patch, prefix} to a preds file
  2) DELETE those worktrees so the next batch has room (shallow repos are regenerable via pro_stage_goals)

  python bench/pro_capture.py --preds .fleet/swe/pro_preds_50.json [--keep]   # --keep = don't delete
"""
import argparse, hashlib, json, os, shutil, subprocess, time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SW = os.path.join(REPO, ".fleet", "swe")
WTMAP = os.path.join(SW, "pro_wt_map.json")

# Kept in step with bench/ui_missing_ids.py, which treats anything larger as uncovered.
MAX_PATCH_BYTES = 1_000_000


#: THE DIFF MUST COME FROM WHEREVER THE WORK HAPPENED.
#:
#: This module reads the LOCAL worktree. Under routing the worker edits /app inside the
#: instance's container and the local directory is only an address, so every capture would
#: return an empty patch -- and an empty patch is exactly what a model that solved nothing
#: produces. A routed run would have scored zero and read as a modelling result.
def _routed_diff(inst):
    """Unified diff of the container's checkout, or None if routing is not carrying this run.

    THE IMPORT IS NOT ALLOWED TO FAIL QUIETLY -- the same fail-open that was closed in
    pro_stage_goals.py and left open here. Run as `python bench/pro_capture.py`, sys.path[0]
    is bench/ and `import relay` raises ImportError; caught and turned into "routing is off",
    capture fell back to reading the LOCAL directory, which under routing holds a note instead
    of a checkout. Measured 2026-08-31: a worker had edited seven files inside its container
    and this function reported nothing, so the run recorded 0 predictions and 4 skips with the
    reason "not a worktree root" -- which reads as a staging problem and is not one.
    """
    # BOTH IMPORT FORMS, because this file is run as `python bench/<script>.py` (sys.path[0]
    # is bench/, so `bench.` does not resolve) and imported as `bench.<script>` from the
    # tests. Getting this wrong is the same fail-open one level up.
    try:
        from bench.routing_switch import broker as _broker
    except ImportError:
        from routing_switch import broker as _broker
    bc = _broker("pro_capture")
    if bc is None:
        return None
    # Same two sources as the local path: tracked changes (staged or not) via `diff HEAD`,
    # then each untracked file. `diff --no-index` exits 1 when the files differ, which is the
    # normal case here, so the exit status of the whole script must not be taken from it.
    cmd = ("cd /app && git diff HEAD; "
           "git ls-files --others --exclude-standard | while IFS= read -r f; do "
           "git diff --no-index /dev/null \"$f\" || true; done")
    try:
        res = bc.exec_(inst, cmd, timeout=180)
    except Exception as exc:
        # NOT an empty patch. Returning "" here would be indistinguishable from a worker that
        # changed nothing, which is the confusion this whole function exists to prevent.
        raise RuntimeError("routed capture failed for %s: %s" % (inst, exc))
    return res.get("output") or ""


def _emit(preds, have, inst, d, prefix):
    """Record one captured patch: replace any earlier entry, snapshot it, append it.

    Factored out so the routed path records EXACTLY what the local path records. Written
    twice, these drift, and the drift shows up as a scoring difference between two runs that
    were supposed to differ only in where the work happened.
    """
    if inst in have:
        preds[:] = [x for x in preds if x["instance_id"] != inst]
    try:
        snap_dir = os.path.join(SW, "attempts")
        os.makedirs(snap_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        with open(os.path.join(snap_dir, "%s__%s.json" % (inst[:80], stamp)),
                  "w", encoding="utf-8") as fh:
            json.dump({"instance_id": inst, "patch": d, "captured_at": time.time(),
                       "patch_sha256_16": hashlib.sha256(d.encode("utf-8")).hexdigest()[:16]},
                      fh, ensure_ascii=False)
    except Exception:
        # A snapshot that cannot be written must not cost the capture it is observing.
        pass
    preds.append({"instance_id": inst, "patch": d, "prefix": prefix})
    print("%-58s patch=%d bytes" % (inst[:58], len(d)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preds", default=os.path.join(SW, "pro_preds_50.json"))
    ap.add_argument("--prefix", default="companion")
    ap.add_argument("--keep", action="store_true", help="capture but do NOT delete the worktrees")
    a = ap.parse_args()

    wt = json.load(open(WTMAP, encoding="utf-8"))
    preds = []
    if os.path.exists(a.preds):
        # utf-8-sig, NOT utf-8. PowerShell 5.1's `Set-Content -Encoding utf8` writes a BOM,
        # and this file is created by the run driver, so a plain utf-8 read raises
        # "Unexpected UTF-8 BOM" and takes the capture step down with it -- which is how a
        # run went idle at 14:15 and sat there. Write without a BOM, read tolerating one.
        preds = json.load(open(a.preds, encoding="utf-8-sig"))
    have = {p["instance_id"] for p in preds}

    captured, skipped = 0, []
    # ALREADY CAPTURED, AND ITS CONTAINER RELEASED ON PURPOSE.
    #
    # pro_wt_map.json accumulates across batches, so every later batch re-walks every earlier
    # instance -- whose container this script destroyed after recording its patch. Each one
    # then failed with "no running container" and was reported as a SKIP. Measured on batch 2:
    # captured 8, skipped 8, and by batch 5 there would have been 32 such lines.
    #
    # The skip list is where a real failure is seen. Filling it with instances that succeeded
    # is how a real one stops being noticed.
    already = {p_["instance_id"] for p_ in preds if (p_.get("patch") or "").strip()}

    for inst, p in sorted(wt.items()):
        if inst in already:
            continue
        if not os.path.isdir(p):
            continue
        # THE DIRECTORY EXISTING IS NOT THE WORKTREE EXISTING.
        #
        # These are git worktrees, and the cleanup below is rmtree(ignore_errors=True): on
        # Windows it removes the checked-out files and fails silently on the locked `.git`
        # entry, leaving a directory containing nothing but a pointer into the main
        # repository. `git -C <that>` then resolves to THE HARNESS'S OWN REPOSITORY and
        # `git diff` returns whatever is uncommitted in it -- so this loop would write the
        # harness's working tree into a prediction file as that instance's patch, and a
        # grader would score it. Measured: every surviving worktree reported HEAD as this
        # repository's latest commit and a dirty count of 36, which is the checkout I was
        # editing, not the instance.
        # ROUTED FIRST: the checks below are about the local git worktree, and under routing
        # there is no local worktree to be right or wrong about.
        _rd = None
        try:
            _rd = _routed_diff(inst)
        except RuntimeError as exc:
            skipped.append((inst, str(exc)))
            continue
        if _rd is not None:
            d = _rd
            if len(d) > MAX_PATCH_BYTES:
                skipped.append((inst, "diff of %d bytes exceeds %d; not a fix"
                                % (len(d), MAX_PATCH_BYTES)))
                d = ""
            _emit(preds, have, inst, d, a.prefix)
            captured += 1
            # RELEASE THE CONTAINER once its patch is safely recorded. Forty instances over
            # five batches leave forty containers and forty work directories on a volume with
            # 25 GB free, and the images they are built from are the 183 GB that must not be
            # re-pulled. --keep holds them, for looking at a run that went wrong.
            if not a.keep:
                try:
                    from bench.routing_switch import broker as _b
                except ImportError:
                    from routing_switch import broker as _b
                try:
                    _bc2 = _b("pro_capture cleanup")
                    if _bc2 is not None:
                        _bc2.destroy(inst)
                except Exception as exc:
                    print("WARNING: could not release container for %s: %s"
                          % (inst[:50], str(exc)[:100]))
            continue

        top = subprocess.run(["git", "-C", p, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace").stdout.strip()
        if os.path.normcase(os.path.abspath(top or "")) != os.path.normcase(os.path.abspath(p)):
            skipped.append((inst, "not a worktree root (resolves to %s)" % (top or "?")))
            continue
        # `git diff` alone shows UNSTAGED changes to TRACKED files. A worker that staged its
        # edit, or added a new file, produces nothing under it -- and nothing is exactly what
        # a wrong answer looks like, so the two were indistinguishable. HEAD covers staged and
        # unstaged; untracked files are added separately below.
        d = subprocess.run(["git", "-C", p, "diff", "HEAD"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace").stdout
        extra = subprocess.run(["git", "-C", p, "ls-files", "--others", "--exclude-standard"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace").stdout.split()
        for rel in extra:
            add = subprocess.run(["git", "-C", p, "diff", "--no-index", "/dev/null", rel],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace").stdout
            if add:
                d += add
        # AN OVERSIZE DIFF IS NOT A FIX, AND IT COSTS THE DISK THE RUN NEEDS.
        #
        # Measured 2026-08-29: one instance captured 105,722,582 bytes -- a worker had
        # regenerated vendored and built files, so `git diff HEAD` returned most of a
        # checkout. It made the predictions file 115 MB on a box that had 2.7 GB free, and
        # no grader can score it. Record the fact and the size; do not store the bytes.
        if len(d) > MAX_PATCH_BYTES:
            skipped.append((inst, "diff of %d bytes exceeds %d; not a fix" % (len(d), MAX_PATCH_BYTES)))
            d = ""
        _emit(preds, have, inst, d, a.prefix)
        captured += 1
        if not a.keep:
            # `git worktree remove` FIRST, rmtree only as a fallback.
            #
            # rmtree(ignore_errors=True) cannot delete the locked `.git` entry on Windows, so
            # it leaves a husk -- a directory that still resolves to the MAIN repository, which
            # is how this step came to be capable of submitting the harness's own diff as a
            # prediction. It also leaves the checkout's bulk behind often enough to matter:
            # measured mid-run, 1,110 MB of worktrees with free disk at 3.1 GB against a 3.0 GB
            # admission floor, which is the state that had every worker sitting at turn zero.
            #
            # git removes its own worktree properly, administrative files included, and
            # --force is right here because the checkout has just been read and is finished
            # with.
            done = subprocess.run(["git", "-C", REPO, "worktree", "remove", "--force", p],
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace")
            if done.returncode != 0:
                shutil.rmtree(p, ignore_errors=True)
                if os.path.isdir(p):
                    print("WARNING: could not remove worktree %s (%s)"
                          % (p, (done.stderr or "").strip()[:80]))

    with open(a.preds, "w", encoding="utf-8") as f:
        json.dump(preds, f, ensure_ascii=False)
    for inst, why in skipped:
        # LOUD. A skipped instance is one this run did not measure, and silence here is how a
        # husk's parent-repository diff would have been mistaken for an answer.
        print("SKIPPED %-58s %s" % (inst[:58], why))
    print("captured %d, skipped %d (total preds now %d) -> %s%s"
          % (captured, len(skipped), len(preds), a.preds,
             "" if a.keep else "  [worktrees deleted]"))


if __name__ == "__main__":
    main()
