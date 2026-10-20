# -*- coding: utf-8 -*-
"""Public synthetic data, goals and a grader for the sibling-duplication and fan-out on/off experiments.

WHAT DECISION THIS SERVES. Two questions the roadmap cannot answer from the fleet's existing records:
  (1) do siblings of one fan-out campaign repeat EXPENSIVE calls often enough for a shared board (or
      a tool-result cache) to pay? The earlier measurement only saw millisecond read_file calls, so
      its duplicate-time share could not have reached the 5% threshold whatever the siblings did;
  (2) does fan-out beat a single worker on the SAME goal (the paired comparison C3 and C8 need)?
Both need goals whose sibling calls take real time and share a prefix of work. This script makes the
data and the goals; scripts/exp_collect.py measures; scripts/exp_fleet_launch.py runs one goal.

WHY THE DATA IS GENERATED. Workers cannot use web_search / web_fetch (relay/fleet_toolset.py keeps
them out, and that stays), and no company data may appear in an experiment goal. Everything here is
produced by an open, deterministic algorithm from a seed, so a third party can regenerate it and every
expected answer is computed from the generated files by the same rule the goal states.

TWO FAMILIES (6 goals each, 12 in all)
  s01..s06  "series": one 1.5M-row CSV and a baseline script. The goal NAMES the baseline command, so
            every sibling that needs the shared prefix issues the identical command. This is the
            CEILING case: duplicate fingerprints are guaranteed to be possible. It tells how much
            time duplication could cost, not how often real goals look like this.
  t01..t06  "tree": a large generated source tree. The goal states a quantity that needs a whole-tree
            total (the shared prefix) but does NOT say how to get it, so siblings choose their own
            tool calls. This is the NATURAL case: it shows how often independent siblings happen to
            issue the same call.
Every goal is one line, in the shape relay/splittability.py judges SPLIT (a bracketed list of six
named items and "1件ずつ調べて"), and asks for the result as `name=integer` lines so that
`check` can grade it and tell a merge that LOST a child's right answer from a child that was wrong.

  python scripts/exp_public_data.py make   --root DIR [--rows N] [--lags N] [--files-per-pkg N] [--kb-per-file N]
  python scripts/exp_public_data.py verify --root DIR
  python scripts/exp_public_data.py check  --root DIR --goal s01 --final FILE [--child FILE ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

SEED = 20261020
STATIONS = ["S%02d" % i for i in range(1, 13)]
PACKAGES = ["pkg_%s" % c for c in "abcdefghijkl"]
SERIES_GOALS = ["s%02d" % i for i in range(1, 7)]
TREE_GOALS = ["t%02d" % i for i in range(1, 7)]
WINDOWS = [20, 30, 40, 50, 60, 70]
#: tree quantity per goal: what is counted in every package, and the rule that defines a counted line.
TREE_METRICS = [
    ("class", "行頭(空白なし)が `class ` で始まる行"),
    ("def", "行頭の空白を除いて `def ` で始まる行"),
    ("todo", "`TODO` を含む行(大文字小文字を区別しない)"),
]
FILLER_WORDS = ["alpha", "beta", "gamma", "delta", "sigma", "kappa", "omega", "lambda", "theta", "zeta",
                "river", "stone", "cloud", "field", "north", "south", "east", "west", "amber", "cobalt"]
#: Substrings the counting rules look for. No filler word contains any of them (a test holds this).
FORBIDDEN_IN_FILLER = ("class", "def", "todo")

BASELINE_SRC = '''# -*- coding: utf-8 -*-
"""Baseline for the generated observation series (public, synthetic): one threshold per station.

usage: python baseline.py --window W --lags L
Prints, per station, `threshold` (what the goal asks for) and the lag with the largest
autocorrelation. The autocorrelation scan is the expensive part and does not change the thresholds.
"""
import argparse
import csv
import os


def load(path):
    series = {}
    with open(path, newline="", encoding="utf-8") as fh:
        rd = csv.reader(fh)
        next(rd)
        for _idx, station, value in rd:
            series.setdefault(station, []).append(int(value))
    return series


def thresholds(series, window):
    out = {}
    for st in sorted(series):
        v = series[st]
        run = sum(v[:window])
        ma = [run / window]
        for i in range(window, len(v)):
            run += v[i] - v[i - window]
            ma.append(run / window)
        m = sum(ma) / len(ma)
        var = sum((x - m) ** 2 for x in ma) / len(ma)
        out[st] = int(m + var ** 0.5)
    return out


def acf_peak(v, lags):
    n = len(v)
    mean = sum(v) / n
    c = [x - mean for x in v]
    best_lag, best = 0, None
    for lag in range(1, lags + 1):
        s = 0.0
        for i in range(lag, n):
            s += c[i] * c[i - lag]
        if best is None or s > best:
            best_lag, best = lag, s
    return best_lag


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=40)
    ap.add_argument("--lags", type=int, default=70)
    a = ap.parse_args()
    series = load(os.path.join(os.path.dirname(os.path.abspath(__file__)), "readings.csv"))
    th = thresholds(series, a.window)
    for st in sorted(series):
        print("%s threshold=%d acf_peak_lag=%d" % (st, th[st], acf_peak(series[st], a.lags)))


if __name__ == "__main__":
    main()
'''


# --------------------------------------------------------------------------- generation

def lcg(seed):
    """Endless deterministic integers in [0, 2**31)."""
    x = seed & 0x7FFFFFFF
    while True:
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        yield x


def write_series(path, rows, seed=SEED):
    g = lcg(seed)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("idx,station,value\n")
        buf = []
        for i in range(rows):
            buf.append("%d,%s,%d\n" % (i, STATIONS[i % len(STATIONS)], next(g) % 10000))
            if len(buf) >= 50000:
                fh.write("".join(buf))
                buf = []
        fh.write("".join(buf))


_SERIES_CACHE = {}


def series_answers(series_dir, window):
    """{station: rows above that station's threshold} using the baseline script's own functions.
    The CSV is read once per directory: six goals ask about the same file."""
    hit = _SERIES_CACHE.get(series_dir)
    if hit is None:
        ns = {"__name__": "baseline_exp"}
        exec(compile(BASELINE_SRC, "baseline.py", "exec"), ns)
        hit = (ns, ns["load"](os.path.join(series_dir, "readings.csv")))
        _SERIES_CACHE[series_dir] = hit
    ns, series = hit
    th = ns["thresholds"](series, window)
    return {st: sum(1 for x in series[st] if x > th[st]) for st in sorted(series)}, th


def count_lines(lines):
    """The three counted quantities of the tree family, by the rule stated in the goal text."""
    c = d = t = 0
    for ln in lines:
        if ln.startswith("class "):
            c += 1
        if ln.lstrip().startswith("def "):
            d += 1
        if "todo" in ln.lower():
            t += 1
    return {"class": c, "def": d, "todo": t}


def _filler_line(g):
    a, b, c = next(g) % 997, next(g) % 89, next(g) % 31
    w1 = FILLER_WORDS[next(g) % len(FILLER_WORDS)]
    w2 = FILLER_WORDS[next(g) % len(FILLER_WORDS)]
    return "    v%d = %d * %d + %d  # %s %s\n" % (a, a, b, c, w1, w2)


def tree_file_text(g, target_bytes):
    out = ["# generated public test source; no meaning\n"]
    size = len(out[0])
    classes, defs, todos = next(g) % 4, 1 + next(g) % 14, next(g) % 3
    plan = (["class"] * classes) + (["def"] * defs) + (["todo"] * todos)
    while size < target_bytes:
        if plan and next(g) % 7 == 0:
            kind = plan.pop()
            n = next(g) % 100000
            if kind == "class":
                ln = "class Item%d(Base):\n" % n
            elif kind == "def":
                ln = "    def run%d(self):\n" % n
            else:
                ln = "    # TODO: revisit %s\n" % FILLER_WORDS[n % len(FILLER_WORDS)]
        else:
            ln = _filler_line(g)
        out.append(ln)
        size += len(ln)
    # whatever the plan still holds is written at the end, so the file always carries it
    for kind in plan:
        n = next(g) % 100000
        out.append("class Item%d(Base):\n" % n if kind == "class" else
                   "    def run%d(self):\n" % n if kind == "def" else "    # TODO: tail\n")
    return "".join(out)


def write_tree(tree_dir, files_per_pkg, kb_per_file, seed=SEED):
    """Writes the tree and returns {package: {class, def, todo, files}} counted from the written lines."""
    g = lcg(seed + 1)
    counts = {}
    for pi, pkg in enumerate(PACKAGES):
        n_files = files_per_pkg + (pi * 37 % 11) * max(1, files_per_pkg // 10)
        total = {"class": 0, "def": 0, "todo": 0, "files": n_files}
        for fi in range(n_files):
            sub = os.path.join(tree_dir, pkg, "sub%02d" % (fi % 8))
            os.makedirs(sub, exist_ok=True)
            text = tree_file_text(g, kb_per_file * 1024)
            with open(os.path.join(sub, "mod_%04d.py" % fi), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            for k, v in count_lines(text.splitlines()).items():
                total[k] += v
        counts[pkg] = total
    return counts


# --------------------------------------------------------------------------- goals

def _pick(names, start, k=6):
    return [names[(start + i) % len(names)] for i in range(k)]


def series_goal(gid, i, series_dir, lags):
    w = WINDOWS[i]
    items = _pick(STATIONS, 2 * i)
    cmd = "python %s\\baseline.py --window %d --lags %d" % (series_dir, w, lags)
    text = ("合成の観測データの集計です(公開情報のみ。読むだけで、ファイルの変更や削除はしないでください)。"
            "%s\\readings.csv(列は idx,station,value)について、まず基準値を `%s` で得てください"
            "(局ごとに threshold が出ます)。そのうえで、次の6件について1件ずつ調べて、"
            "その局の value が threshold を超える行の数を数えてください。【%s】。"
            "結果は「局名=整数」の形で1行ずつ書いてください。"
            % (series_dir, cmd, "、".join(items)))
    return {"goal_id": gid, "family": "series", "text": text, "items": items, "window": w,
            "prefix_command": cmd}


def tree_goal(gid, i, tree_dir):
    metric, rule = TREE_METRICS[i % len(TREE_METRICS)]
    items = _pick(PACKAGES, 2 * i + 1)
    text = ("合成のソースツリーの集計です(公開情報のみ。読むだけで、変更や削除はしないでください)。"
            "%s の下にある全ての .py ファイルについて、%sの総数を T とします。"
            "次の6件について1件ずつ調べて、そのパッケージ(%s の直下のディレクトリ)の同じ行の数から、"
            "T を12で割った商(切り捨て)を引いた値を求めてください。【%s】。"
            "結果は「パッケージ名=整数」の形で1行ずつ書いてください(負の値もそのまま)。"
            % (tree_dir, rule, tree_dir, "、".join(items)))
    return {"goal_id": gid, "family": "tree", "text": text, "items": items, "metric": metric}


def build_goals(root, lags, tree_counts, series_dir=None, tree_dir=None):
    series_dir = series_dir or os.path.join(root, "series")
    tree_dir = tree_dir or os.path.join(root, "tree")
    goals = []
    for i, gid in enumerate(SERIES_GOALS):
        g = series_goal(gid, i, series_dir, lags)
        ans, _th = series_answers(series_dir, g["window"])
        g["expected"] = {s: ans[s] for s in g["items"]}
        goals.append(g)
    total = {m: sum(c[m] for c in tree_counts.values()) for m, _r in TREE_METRICS}
    for i, gid in enumerate(TREE_GOALS):
        g = tree_goal(gid, i, tree_dir)
        m = g["metric"]
        g["expected"] = {p: tree_counts[p][m] - total[m] // len(PACKAGES) for p in g["items"]}
        g["total"] = total[m]
        goals.append(g)
    return goals


# --------------------------------------------------------------------------- hashing and grading

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_tree(base):
    """{relative path: sha256} for every file under base."""
    out = {}
    for dp, _dn, fns in os.walk(base):
        for fn in sorted(fns):
            p = os.path.join(dp, fn)
            out[os.path.relpath(p, base).replace("\\", "/")] = sha256_file(p)
    return out


def verify_data(root):
    """(ok, changed paths). Compares every generated file to the hashes recorded by `make`."""
    with open(os.path.join(root, "manifest.json"), encoding="utf-8") as fh:
        want = json.load(fh)["hashes"]
    have = {}
    for sub in ("series", "tree"):
        for rel, h in hash_tree(os.path.join(root, sub)).items():
            have["%s/%s" % (sub, rel)] = h
    bad = sorted(k for k in set(want) | set(have) if want.get(k) != have.get(k))
    return (not bad), bad


_ITEM_LINE = re.compile(r"([A-Za-z][A-Za-z0-9_\-]*)\s*[=＝]\s*(-?[0-9][0-9,]*)")


def parse_answer(text):
    """{name: int} from `name=integer` lines. The LAST value given for a name wins."""
    out = {}
    for name, num in _ITEM_LINE.findall(text or ""):
        try:
            out[name] = int(num.replace(",", ""))
        except ValueError:
            continue
    return out


def value_near(text, name, expected):
    """True when `name` is followed, within 30 characters, by the expected integer."""
    for m in re.finditer(re.escape(name), text or ""):
        tail = (text or "")[m.end():m.end() + 30]
        for num in re.findall(r"-?[0-9][0-9,]*", tail):
            try:
                if int(num.replace(",", "")) == expected:
                    return True
            except ValueError:
                pass
    return False


def grade(expected, final_text, child_texts=()):
    """Per item: ok / merge_loss / wrong. `merge_loss` = the final answer is wrong or missing while
    some child's report carries the right value (information the merge dropped or garbled);
    `wrong` = nobody carried it. Without children (a single worker) a miss is `wrong`."""
    got = parse_answer(final_text)
    rows = {}
    for name, want in expected.items():
        if got.get(name) == want:
            rows[name] = "ok"
        elif any(value_near(t, name, want) for t in child_texts):
            rows[name] = "merge_loss"
        else:
            rows[name] = "wrong"
    return rows


# --------------------------------------------------------------------------- commands

def free_gb(path):
    import shutil
    return shutil.disk_usage(path).free / 1e9


def cmd_make(a):
    root = os.path.abspath(a.root)
    if os.path.exists(os.path.join(root, "manifest.json")) and not a.force:
        print("refused: %s already holds a manifest (use --force to rebuild)" % root)
        return 2
    need = (a.rows * 15 + (a.files_per_pkg * 13 * a.kb_per_file * 1024)) / 1e9
    if free_gb(os.path.dirname(root) or ".") - need < a.min_free_gb:
        print("refused: about %.2f GB needed and fewer than %.1f GB would remain" % (need, a.min_free_gb))
        return 3
    series_dir, tree_dir = os.path.join(root, "series"), os.path.join(root, "tree")
    os.makedirs(series_dir, exist_ok=True)
    os.makedirs(tree_dir, exist_ok=True)
    write_series(os.path.join(series_dir, "readings.csv"), a.rows)
    with open(os.path.join(series_dir, "baseline.py"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(BASELINE_SRC)
    counts = write_tree(tree_dir, a.files_per_pkg, a.kb_per_file)
    goals = build_goals(root, a.lags, counts)
    hashes = {}
    for sub in ("series", "tree"):
        for rel, h in hash_tree(os.path.join(root, sub)).items():
            hashes["%s/%s" % (sub, rel)] = h
    os.makedirs(os.path.join(root, "goals"), exist_ok=True)
    for g in goals:
        with open(os.path.join(root, "goals", g["goal_id"] + ".txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(g["text"] + "\n")
    with open(os.path.join(root, "goals.json"), "w", encoding="utf-8") as fh:
        json.dump(goals, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"seed": SEED, "rows": a.rows, "lags": a.lags, "files_per_pkg": a.files_per_pkg,
                   "kb_per_file": a.kb_per_file, "tree_counts": counts, "hashes": hashes}, fh,
                  ensure_ascii=False, indent=1)
    print("made %d goals, %d files hashed under %s" % (len(goals), len(hashes), root))
    return 0


def cmd_verify(a):
    ok, bad = verify_data(os.path.abspath(a.root))
    print("data intact" if ok else "CHANGED: %s" % ", ".join(bad[:10]))
    return 0 if ok else 1


def _read(path):
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        return fh.read()


def cmd_check(a):
    with open(os.path.join(os.path.abspath(a.root), "goals.json"), encoding="utf-8") as fh:
        goals = {g["goal_id"]: g for g in json.load(fh)}
    g = goals.get(a.goal)
    if g is None:
        print("unknown goal %s" % a.goal)
        return 2
    rows = grade(g["expected"], _read(a.final), [_read(p) for p in (a.child or [])])
    print(json.dumps({"goal_id": a.goal, "items": rows,
                      "ok": sum(1 for v in rows.values() if v == "ok"), "n": len(rows)},
                     ensure_ascii=False))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("--root", required=True)
    m.add_argument("--rows", type=int, default=1500000)
    m.add_argument("--lags", type=int, default=70)
    m.add_argument("--files-per-pkg", type=int, default=400)
    m.add_argument("--kb-per-file", type=int, default=24)
    m.add_argument("--min-free-gb", type=float, default=3.0)
    m.add_argument("--force", action="store_true")
    v = sub.add_parser("verify")
    v.add_argument("--root", required=True)
    c = sub.add_parser("check")
    c.add_argument("--root", required=True)
    c.add_argument("--goal", required=True)
    c.add_argument("--final", required=True)
    c.add_argument("--child", action="append")
    a = ap.parse_args(argv)
    return {"make": cmd_make, "verify": cmd_verify, "check": cmd_check}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
