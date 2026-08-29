"""Capture + cleanup half of the disk-safe batched Pro 50-run (the dev box has ~8GB free, so we
never stage all 50 at once). After a fleet batch finishes:
  1) git diff each worktree in pro_wt_map.json -> append {instance_id, patch, prefix} to a preds file
  2) DELETE those worktrees so the next batch has room (shallow repos are regenerable via pro_stage_goals)

  python bench/pro_capture.py --preds .fleet/swe/pro_preds_50.json [--keep]   # --keep = don't delete
"""
import argparse, json, os, shutil, subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SW = os.path.join(REPO, ".fleet", "swe")
WTMAP = os.path.join(SW, "pro_wt_map.json")


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
    for inst, p in sorted(wt.items()):
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
        if inst in have:
            preds = [x for x in preds if x["instance_id"] != inst]  # replace
        preds.append({"instance_id": inst, "patch": d, "prefix": a.prefix})
        captured += 1
        print("%-58s patch=%d bytes" % (inst[:58], len(d)))
        if not a.keep:
            shutil.rmtree(p, ignore_errors=True)

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
