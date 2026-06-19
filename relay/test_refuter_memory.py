"""Tests for relay/refuter_memory.py -- the OPT-IN adaptive lens selector for operator B.

Pure / no browser. Proves: feature bucketing; Laplace/backoff smoothing (a lens that
keeps refuting a bucket rises above a never-refuted lens); rank ordering; top-k selection
plus the deterministic exploration slot eventually sampling the least-observed lens;
persistence round-trip; and tolerance of a missing/corrupt store. Uses a tempfile path, so
the real .fleet/refuter_memory.json is never touched.

Run:  .venv\\Scripts\\python.exe relay\\test_refuter_memory.py
"""
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import relay.refuter_memory as rm

results = []


def check(name, cond):
    results.append(bool(cond))
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


def _tmp():
    fd, p = tempfile.mkstemp(suffix=".json", prefix="reftest_")
    os.close(fd)
    os.remove(p)              # we want a non-existent path (start empty)
    return p


def main():
    LENSES = ["correctness", "edge", "security"]

    # --- extract_features buckets ---
    os.environ.pop("MCP_TASK_DOMAIN", None)
    f_code = rm.extract_features("fix the bug in repo foo.py diff",
                                 "patched src/a.py and tests/test_a.py, minimal one-line change")
    check("feat_domain_coding", f_code["domain"] == "coding")
    check("feat_files_multi", f_code["files"] == "multi")
    check("feat_repro_y", f_code["has_repro"] == "y")     # mentions tests
    check("feat_minimal_y", f_code["claims_minimal"] == "y")

    f_gen = rm.extract_features("summarize this article", "Here is a short summary.")
    check("feat_domain_general", f_gen["domain"] == "general")
    check("feat_size_small", f_gen["size"] == "s")
    check("feat_files_single", f_gen["files"] == "single")
    check("feat_repro_n", f_gen["has_repro"] == "n")

    os.environ["MCP_TASK_DOMAIN"] = "coding"
    check("feat_domain_env_coding",
          rm.extract_features("do a thing", "done")["domain"] == "coding")
    os.environ.pop("MCP_TASK_DOMAIN", None)

    check("bucket_key_stable",
          rm.bucket_key({"a": "1", "b": "2"}) == rm.bucket_key({"b": "2", "a": "1"}))

    # --- record / rejection_prob smoothing ---
    p = _tmp()
    mem = rm.RefuterMemory(path=p)
    feats = f_gen
    # no data anywhere -> neutral prior
    check("prob_prior_no_data", abs(mem.rejection_prob(feats, "edge") - 0.5) < 1e-9)

    # 'edge' refutes this bucket repeatedly; 'security' is recorded but never refutes.
    for _ in range(8):
        mem.record(feats, "edge", refuted=True)
    for _ in range(8):
        mem.record(feats, "security", refuted=False)
    p_edge = mem.rejection_prob(feats, "edge")
    p_sec = mem.rejection_prob(feats, "security")
    check("prob_refuter_rises", p_edge > 0.75)
    check("prob_never_refute_low", p_sec < 0.25)
    check("prob_refuter_above_clean", p_edge > p_sec)

    # backoff: a thin cell (few obs) for 'correctness' in a NEW bucket blends toward the
    # global 'correctness' rate rather than swinging fully on one sample.
    other = dict(feats); other["size"] = "l"
    for _ in range(10):
        mem.record(feats, "correctness", refuted=True)     # global correctness ~ high
    mem.record(other, "correctness", refuted=False)        # 1 obs in the new bucket
    p_thin = mem.rejection_prob(other, "correctness")
    # 1 obs, refuted=False -> raw local would be (0+1)/(1+2)=0.333; global is high, so the
    # blended estimate must sit ABOVE the pure-local value (pulled toward the global rate).
    check("backoff_blends_to_global", p_thin > 0.333)

    # --- rank_lenses ordering ---
    ranked = mem.rank_lenses(feats, LENSES)
    names = [l for l, _ in ranked]
    check("rank_edge_first", names[0] in ("edge", "correctness"))   # both refute this bucket
    check("rank_security_last", names[-1] == "security")
    check("rank_is_sorted_desc",
          all(ranked[i][1] >= ranked[i + 1][1] for i in range(len(ranked) - 1)))

    # --- select_lenses top-k ---
    sel = mem.select_lenses(feats, LENSES, k=2)
    check("select_returns_k", len(sel) == 2)
    check("select_picks_top", "security" not in sel)       # weakest lens excluded by top-k
    check("select_k_ge_len_all", len(mem.select_lenses(feats, LENSES, k=5)) == len(LENSES))
    check("select_min_one", len(mem.select_lenses(feats, LENSES, k=0)) >= 1)

    # exploration slot: 'security' is the least-observed once we ALSO pile obs onto the
    # others; within one EXPLORE_PERIOD window the least-observed lens must appear at least
    # once even though top-k would otherwise drop it.
    p2 = _tmp()
    mem2 = rm.RefuterMemory(path=p2)
    fx = rm.extract_features("g", "f")
    # make edge & correctness data-rich and high-refute; leave security with no data.
    for _ in range(20):
        mem2.record(fx, "edge", refuted=True)
        mem2.record(fx, "correctness", refuted=True)
    saw_security = False
    for _ in range(rm._EXPLORE_PERIOD + 1):
        if "security" in mem2.select_lenses(fx, LENSES, k=2):
            saw_security = True
            break
    check("explore_samples_least_observed", saw_security)

    # --- persistence round-trip ---
    p3 = _tmp()
    m_a = rm.RefuterMemory(path=p3)
    fz = rm.extract_features("repo bug", "fixed file a.py")
    for _ in range(4):
        m_a.record(fz, "edge", refuted=True)
    m_b = rm.RefuterMemory(path=p3)          # fresh instance loads from disk
    check("persist_roundtrip",
          m_b.rejection_prob(fz, "edge") == m_a.rejection_prob(fz, "edge"))
    check("persist_no_bom", open(p3, "rb").read(1) != b"\xef")   # BOM-less write

    # --- corrupt / missing tolerated ---
    pc = _tmp()
    with open(pc, "w", encoding="utf-8") as fh:
        fh.write("{ this is not valid json ::::")
    m_c = rm.RefuterMemory(path=pc)          # must not raise
    check("corrupt_starts_empty", abs(m_c.rejection_prob(fz, "edge") - 0.5) < 1e-9)
    pm = _tmp()
    m_d = rm.RefuterMemory(path=pm)          # missing file
    check("missing_starts_empty", m_d.data["cells"] == {})

    # cleanup temp files
    for pth in (p, p2, p3, pc, pm):
        try:
            os.remove(pth)
        except OSError:
            pass

    print("\n=== %d/%d refuter-memory checks passed ===" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
