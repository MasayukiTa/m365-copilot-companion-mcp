"""Slice JSON -> the one-object-per-line file the grader reads.

The grader needs the instance list to know which instances exist at all: it grades only the
rows whose instance_id carries a patch, and reports the rest as "carry no patch and are NOT
scored". Uploading predictions WITHOUT this file leaves it reading whatever raw file an
earlier run left in that directory -- so a run's patches get scored against another run's
instance list, and the denominator is quietly someone else's.
"""
import io
import json
import sys


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 2:
        print("usage: slice_to_raw_jsonl.py <slice.json> <out.jsonl>")
        return 2
    src, dst = argv
    rows = json.load(io.open(src, encoding="utf-8-sig"))
    if not isinstance(rows, list) or not rows:
        print("%s holds no rows; refusing to write an empty instance list, which would grade "
              "every prediction against nothing" % src)
        return 1
    with io.open(dst, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("%d rows -> %s" % (len(rows), dst))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
