"""Which instances of a slice still have no answer.

The run driver used to end by printing how many rows the predictions file held. That
number counts rows, and a row exists whether or not anyone worked the instance: a batch
whose submit threw still reached the capture step, which wrote an empty patch per
instance and moved on. The run then reported "DONE: 40 predictions" for a slice where 22
instances had never been submitted to anything.

So: coverage is a non-empty patch, not a row. This is the predicate the driver loops on.
"""
import io, json, os, sys

# A real fix is kilobytes. 105,722,582 bytes was measured once -- a whole checkout's worth
# of vendored and generated files, captured because the worker regenerated them. It is not
# an answer, it is not gradeable, and it was 92% of a 115 MB predictions file on a disk
# that had 2.7 GB left. Treat oversize as uncovered so the instance is worked again.
MAX_PATCH_BYTES = 1_000_000


def covered(preds_path):
    """instance_id -> patch length, for rows that count as answered."""
    if not os.path.exists(preds_path):
        return {}
    rows = json.load(io.open(preds_path, encoding="utf-8-sig"))
    out = {}
    for r in rows:
        n = len(r.get("patch") or "")
        if 0 < n <= MAX_PATCH_BYTES:
            out[r["instance_id"]] = n
    return out


def missing(ids, preds_path):
    have = covered(preds_path)
    return [i for i in ids if i not in have]


def main():
    ids_file, preds = sys.argv[1], sys.argv[2]
    ids = [x.strip() for x in io.open(ids_file, encoding="utf-8-sig") if x.strip()]
    miss = missing(ids, preds)
    if len(sys.argv) > 3 and sys.argv[3] == "--count":
        print(len(miss))
    else:
        print("\n".join(miss))


if __name__ == "__main__":
    main()
