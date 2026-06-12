"""Fix CJK-adjacent **bold** that GitHub/CommonMark renders as literal asterisks.

Rule: a `**` strong span fails to render when the char immediately BEFORE the opening
`**` is a CJK letter (both-flanking open) or the char immediately AFTER the closing `**`
is a CJK letter (both-flanking close). Fix = insert a normal space on the offending side
so the delimiter run is no longer wedged between letters. Skips fenced code blocks.

  python bench/fix_readme_bold.py [README.md]  (writes in place; prints a summary)
"""
import re
import sys

CJK = "ぁ-ゖァ-ヺー一-鿿々"
cjk_re = re.compile("[" + CJK + "]")
span_re = re.compile(r"\*\*(?:[^*\n]|\*(?!\*))+?\*\*")


def fix_line(ln):
    out = []
    i = 0
    changed = 0
    for m in span_re.finditer(ln):
        out.append(ln[i:m.start()])
        before = ln[m.start() - 1:m.start()]
        after = ln[m.end():m.end() + 1]
        pre = " " if (before and cjk_re.match(before)) else ""
        post = " " if (after and cjk_re.match(after)) else ""
        if pre or post:
            changed += 1
        out.append(pre + m.group() + post)
        i = m.end()
    out.append(ln[i:])
    return "".join(out), changed


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "README.md"
    lines = open(path, encoding="utf-8").read().split("\n")
    in_fence = False
    total = 0
    for idx, ln in enumerate(lines):
        if ln.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        new, c = fix_line(ln)
        if c:
            lines[idx] = new
            total += c
    open(path, "w", encoding="utf-8").write("\n".join(lines))
    print("fixed %d CJK-adjacent bold span(s) in %s" % (total, path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
