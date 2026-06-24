"""Capture + cleanup half of the disk-safe batched Pro 50-run (USER has ~8GB free, so we
never stage all 50 at once). After a fleet batch finishes:
  1) git diff each worktree in pro_wt_map.json -> append {instance_id, patch, prefix} to a preds file
  2) DELETE those worktrees so the next batch has room (shallow repos are regenerable via pro_stage_goals)

  python bench/pro_capture.py --preds .fleet/swe/pro_preds_50.json [--keep]   # --keep = don't delete
"""
import argparse, json, os, shutil, subprocess

REPO = r"C:\Users\USER\companion-mcp"
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
        preds = json.load(open(a.preds, encoding="utf-8"))
    have = {p["instance_id"] for p in preds}

    captured = 0
    for inst, p in sorted(wt.items()):
        if not os.path.isdir(p):
            continue
        d = subprocess.run(["git", "-C", p, "diff"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace").stdout
        if inst in have:
            preds = [x for x in preds if x["instance_id"] != inst]  # replace
        preds.append({"instance_id": inst, "patch": d, "prefix": a.prefix})
        captured += 1
        print("%-58s patch=%d bytes" % (inst[:58], len(d)))
        if not a.keep:
            shutil.rmtree(p, ignore_errors=True)

    with open(a.preds, "w", encoding="utf-8") as f:
        json.dump(preds, f, ensure_ascii=False)
    print("captured %d (total preds now %d) -> %s%s"
          % (captured, len(preds), a.preds, "" if a.keep else "  [worktrees deleted]"))


if __name__ == "__main__":
    main()
