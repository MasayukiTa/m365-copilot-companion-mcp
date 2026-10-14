"""Re-apply an edit made to the OLD single-file ui/FleetCockpit.cs onto the split cockpit parts.

WHY THIS EXISTS. ui/FleetCockpit.cs was split into `partial class CockpitWindow` parts
(ui/FleetCockpit.<Theme>.cs) by pure moves: not one method body changed, only which file holds it.
A branch that was cut before the split still edits ui/FleetCockpit.cs at the old line numbers, and
git cannot carry such an edit across a move (a rebase conflicts, or worse, "succeeds" by
re-adding the moved method to the main part). The moved text is intact, though, so a hunk can be
placed by CONTENT: the hunk's old-side lines (context + removed) occur, contiguously, in exactly one
part. This tool finds that part and applies the hunk there.

RECIPE (in the worktree of the branch being moved onto the split):
  1. Make the patch of what the branch changed in the cockpit, against its OLD base:
         git diff <old-base> HEAD -- ui/FleetCockpit.cs > cockpit_edit.patch
     (<old-base> = `git merge-base HEAD <pre-split main>`; a patch from `git format-patch` /
      `git show <commit> -- ui/FleetCockpit.cs` works too.)
  2. Move the branch onto the split without the cockpit file's own history, e.g.
         git checkout -B <branch>-resplit <split-branch>
         git cherry-pick <commits>            # conflicts in ui/FleetCockpit.cs: take the split
                                              #   side:  git checkout --ours -- ui/FleetCockpit.cs
     or simply start from the split branch and copy over the non-cockpit files.
  3. python tools/resplit_cockpit_patch.py cockpit_edit.patch            # dry run: where each hunk goes
     python tools/resplit_cockpit_patch.py cockpit_edit.patch --apply    # write the parts
  4. Build (python bench/ui_build_check.py) and run the cockpit tests, then commit.

A hunk that cannot be placed (context moved apart, or the old side was edited since) is REPORTED
with the nearest preceding method name, never guessed at; the exit status is 1 and nothing is
written unless every hunk was placed (use --partial to write the placed ones anyway).

Only hunks for a file named ui/FleetCockpit.cs are read. Line endings and the byte-order mark of
each part are preserved.
"""
import argparse
import glob
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def parse_patch(text):
    """Yield (old_start, old_block, new_block, header) per hunk of ui/FleetCockpit.cs."""
    hunks = []
    cur_file = None
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("+++ "):
            p = ln[4:].split("\t")[0].strip()
            cur_file = p
            i += 1
            continue
        m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", ln)
        if m and cur_file and cur_file.replace("\\", "/").endswith("ui/FleetCockpit.cs"):
            old_start = int(m.group(1))
            old, new = [], []
            i += 1
            while i < len(lines):
                s = lines[i]
                if s.startswith("@@ ") or s.startswith("diff --git") or s.startswith("--- ") and \
                        i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
                    break
                if s.startswith("\\"):
                    i += 1
                    continue
                if s.startswith("-"):
                    old.append(s[1:])
                elif s.startswith("+"):
                    new.append(s[1:])
                elif s.startswith(" "):
                    old.append(s[1:])
                    new.append(s[1:])
                elif s == "":
                    # a blank context line whose leading space was stripped by an editor/mailer
                    old.append("")
                    new.append("")
                else:
                    break
                i += 1
            # drop the trailing empty element produced by the final newline of the patch
            while old and new and old[-1] == "" and new[-1] == "" and i >= len(lines):
                old.pop()
                new.pop()
            hunks.append({"old_start": old_start, "old": old, "new": new, "header": ln})
            continue
        i += 1
    return hunks


def load_parts(ui_dir):
    main = os.path.join(ui_dir, "FleetCockpit.cs")
    names = [main] if os.path.isfile(main) else []
    names += sorted(glob.glob(os.path.join(ui_dir, "FleetCockpit.*.cs")))
    parts = []
    for p in names:
        raw = open(p, "rb").read()
        bom = raw.startswith(b"\xef\xbb\xbf")
        txt = raw[3:].decode("utf-8") if bom else raw.decode("utf-8")
        crlf = "\r\n" in txt
        ls = txt.replace("\r\n", "\n").split("\n")
        parts.append({"path": p, "bom": bom, "crlf": crlf, "lines": ls})
    return parts


def find_block(lines, block):
    n = len(block)
    if n == 0:
        return []
    hits = []
    first = block[0]
    for i in range(0, len(lines) - n + 1):
        if lines[i] == first and lines[i:i + n] == block:
            hits.append(i)
    return hits


def nearest_method(parts, hunk):
    """Best-effort label for a failure message: the last 'name(' declaration before the first
    changed line, searched in every part by the hunk's first non-blank context line."""
    probe = next((x for x in hunk["old"] if x.strip()), "")
    for p in parts:
        for i, l in enumerate(p["lines"]):
            if l == probe:
                for j in range(i, -1, -1):
                    m = re.match(r"^    [A-Za-z_][\w<>\[\],. ]*\s+([A-Za-z_]\w*)\s*\(", p["lines"][j])
                    if m:
                        return "%s near %s() (%s)" % (os.path.basename(p["path"]), m.group(1), "line %d" % (j + 1))
    return "no part contains the first line of the hunk"


def place(parts, hunk):
    """Return (part_index, start, old_len, new_block, note) or (None, reason)."""
    old, new = hunk["old"], hunk["new"]
    # candidate trims: (drop leading context n, drop trailing context m)
    lead = 0
    while lead < len(old) and lead < len(new) and old[lead] == new[lead]:
        lead += 1
    trail = 0
    while trail < len(old) - lead and trail < len(new) - lead and old[len(old) - 1 - trail] == new[len(new) - 1 - trail]:
        trail += 1
    tries = []
    for total in range(0, lead + trail + 1):
        for dl in range(0, min(lead, total) + 1):
            dt = total - dl
            if dt <= trail:
                tries.append((dl, dt))
    for dl, dt in tries:
        o = old[dl:len(old) - dt]
        nw = new[dl:len(new) - dt]
        if not o:
            continue
        # keep at least one non-blank anchor line so a trimmed hunk is not placed on blank lines alone
        if not any(x.strip() for x in o):
            continue
        cands = []
        for pi, p in enumerate(parts):
            for h in find_block(p["lines"], o):
                cands.append((pi, h))
        if len(cands) == 1:
            pi, h = cands[0]
            note = "" if (dl, dt) == (0, 0) else "context trimmed by %d/%d line(s)" % (dl, dt)
            return (pi, h, len(o), nw, note), None
        if len(cands) > 1:
            return None, "ambiguous: %d places match (%s)" % (
                len(cands), ", ".join("%s:%d" % (os.path.basename(parts[a]["path"]), b + 1) for a, b in cands[:4]))
    return None, "old side not found in any part (%s)" % nearest_method(parts, hunk)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("patch")
    ap.add_argument("--ui-dir", default=os.path.join(REPO, "ui"))
    ap.add_argument("--apply", action="store_true", help="write the parts (default: dry run)")
    ap.add_argument("--partial", action="store_true", help="with --apply: write placed hunks even if some failed")
    a = ap.parse_args(argv)

    text = open(a.patch, encoding="utf-8", errors="replace", newline="").read()
    hunks = parse_patch(text)
    if not hunks:
        print("no hunks for ui/FleetCockpit.cs in %s" % a.patch)
        return 2
    parts = load_parts(a.ui_dir)
    if not parts:
        print("no FleetCockpit*.cs under %s" % a.ui_dir)
        return 2

    placed, failed = [], []
    for k, h in enumerate(hunks):
        res, why = place(parts, h)
        if res is None:
            failed.append((k, h, why))
        else:
            placed.append((k, h, res))
    for k, h, (pi, st, ol, nw, note) in placed:
        print("hunk %d  %-34s line %5d  -%d +%d lines %s" % (
            k + 1, os.path.basename(parts[pi]["path"]), st + 1, ol, len(nw), note))
    for k, h, why in failed:
        print("hunk %d  FAILED: %s\n         %s" % (k + 1, why, h["header"]))

    # overlap check per part
    by_part = {}
    for k, h, (pi, st, ol, nw, note) in placed:
        by_part.setdefault(pi, []).append((st, ol, nw, k))
    for pi, items in by_part.items():
        items.sort()
        for (s1, l1, _n1, k1), (s2, _l2, _n2, k2) in zip(items, items[1:]):
            if s1 + l1 > s2:
                print("hunks %d and %d overlap in %s" % (k1 + 1, k2 + 1, os.path.basename(parts[pi]["path"])))
                return 1
    if failed and not a.partial:
        print("%d of %d hunk(s) could not be placed; nothing written." % (len(failed), len(hunks)))
        return 1
    if not a.apply:
        print("dry run: %d hunk(s) placeable, %d not. Re-run with --apply to write." % (len(placed), len(failed)))
        return 1 if failed else 0
    for pi, items in by_part.items():
        p = parts[pi]
        for st, ol, nw, _k in sorted(items, reverse=True):
            p["lines"][st:st + ol] = nw
        body = "\n".join(p["lines"])
        if p["crlf"]:
            body = body.replace("\n", "\r\n")
        data = body.encode("utf-8")
        if p["bom"]:
            data = b"\xef\xbb\xbf" + data
        with open(p["path"], "wb") as fh:
            fh.write(data)
        print("wrote %s (%d hunk(s))" % (os.path.basename(p["path"]), len(items)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
