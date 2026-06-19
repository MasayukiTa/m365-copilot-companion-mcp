"""refuter_memory.py -- OPT-IN adaptive lens selection for operator B (the refuter).

The fixed panel (PANEL_LENSES = correctness/edge/security, majority vote in
relay/refuter.py) runs EVERY lens on EVERY candidate DONE. That is robust but pays the
full oracle cost (one independent Copilot conversation per lens) on every review, and it
never learns: a lens that essentially never refutes a given KIND of candidate still gets
thrown at it forever.

This module records WHICH lens actually refuted past candidates, bucketed by cheap,
deterministic features of the candidate (domain, size, files-touched, repro, minimality
claim), and predicts, for the current candidate's bucket, each lens's rejection
probability. The selective hook (gated behind MCP_ADAPTIVE_REFUTER=1 at the panel call
site) then throws only the top-k most-likely-to-refute lenses -- cutting oracle calls and
adapting over time -- while keeping an exploration slot so no lens is ever starved of data.

STDLIB ONLY (json/os/math): this rides in the live relay; no numpy/sklearn. Pure and
import-side-effect-free so it is fully unit-testable. The whole thing is dormant unless a
caller opts in -- with the env unset the refuter path is byte-for-byte the old fixed panel.
"""
from __future__ import annotations

import json
import math
import os

# Smoothing / backoff knobs. Kept module-level so tests can reason about them.
_PRIOR = 0.5                # rejection prob with no data at all (max entropy)
_MIN_OBS = 5                # below this many obs for a (bucket,lens), blend toward the
                            # per-lens GLOBAL rate (the specific cell isn't trusted alone)
_EXPLORE_PERIOD = 7         # every Nth select() (by total obs count) force the least-seen lens


def _repo_root() -> str:
    # relay/refuter_memory.py -> repo root is the parent of relay/
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_path() -> str:
    return os.path.join(_repo_root(), ".fleet", "refuter_memory.json")


def extract_features(goal: str, final_response: str) -> dict:
    """Cheap, deterministic, coarse buckets describing a candidate DONE.

    Derived only from the goal and the implementer's claimed-done summary (final_response)
    -- no tool calls, no parsing of diffs -- so it is fast and stable. The buckets are
    intentionally crude; their only job is to group "similar" candidates so the memory can
    learn per-group lens hit rates. Keys: domain, size, files, has_repro, claims_minimal.
    """
    goal = goal or ""
    final_response = final_response or ""
    g_low = goal.lower()
    f_low = final_response.lower()

    # domain: coding if the task domain env says so, or the goal smells like code work.
    coding_env = os.environ.get("MCP_TASK_DOMAIN", "").lower() == "coding"
    coding_words = ("code", "repo", "diff", "patch", "function", "class ", "bug",
                    "test", "file", "import", "def ", "compile", "module")
    domain = "coding" if (coding_env or any(w in g_low for w in coding_words)) else "general"

    # size: coarse buckets from the length of the claimed-done summary.
    n = len(final_response)
    size = "s" if n < 400 else ("m" if n < 1500 else "l")

    # files: single vs multi, via a count of path-like tokens (a/b/c.py, src\x.js, foo.txt).
    path_tokens = 0
    for tok in final_response.replace("\\", "/").split():
        t = tok.strip("`'\"(),:;")
        # a path-like token has a slash OR a dotted filename (name.ext)
        if "/" in t and len(t) > 2:
            path_tokens += 1
        elif "." in t:
            head, _, ext = t.rpartition(".")
            if head and ext.isalnum() and 1 <= len(ext) <= 5 and not ext.isdigit():
                path_tokens += 1
    files = "multi" if path_tokens >= 2 else "single"

    # has_repro: does the summary claim a reproduction / test was exercised?
    repro_words = ("reproduc", "repro ", "test", "テスト", "再現", "regression",
                   "failing case", "pytest", "unittest")
    has_repro = "y" if any(w in f_low for w in repro_words) else "n"

    # claims_minimal: does it assert a small / minimal / surgical change?
    minimal_words = ("minimal", "smallest", "surgical", "one line", "single line",
                     "few lines", "最小", "最小限", "minimally")
    claims_minimal = "y" if any(w in f_low for w in minimal_words) else "n"

    return {
        "domain": domain,
        "size": size,
        "files": files,
        "has_repro": has_repro,
        "claims_minimal": claims_minimal,
    }


def bucket_key(features: dict) -> str:
    """Stable string key for a feature bucket (order-independent)."""
    items = sorted((str(k), str(v)) for k, v in (features or {}).items())
    return "|".join("%s=%s" % (k, v) for k, v in items)


class RefuterMemory:
    """Persistent, smoothed per-(bucket,lens) rejection-rate store with selective ranking.

    Counts live as {key: {"refute": int, "total": int}} where key encodes both the feature
    bucket and the lens. Reads tolerate a missing/corrupt file (start empty); writes are
    atomic and BOM-less (this repo has been bitten by BOMs in json control files).
    """

    def __init__(self, path=None):
        self.path = path or _default_path()
        # data["cells"][bucket_key|lens] = {"refute":, "total":}
        # data["selects"] = running count of select_lenses() calls (drives exploration)
        self.data = {"cells": {}, "selects": 0}
        self._load()

    # --- persistence ---
    def _load(self):
        try:
            # utf-8-sig tolerates a BOM if some other tool wrote one.
            with open(self.path, "r", encoding="utf-8-sig") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                cells = raw.get("cells")
                if isinstance(cells, dict):
                    self.data["cells"] = {
                        k: {"refute": int(v.get("refute", 0)),
                            "total": int(v.get("total", 0))}
                        for k, v in cells.items() if isinstance(v, dict)
                    }
                self.data["selects"] = int(raw.get("selects", 0))
        except (FileNotFoundError, ValueError, OSError, TypeError):
            # missing or corrupt -> start empty (already initialised above)
            pass

    def _save(self):
        # Atomic, BOM-less utf-8 write: tmp file then os.replace. newline="" so no platform
        # translation sneaks bytes into a control file.
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="") as fh:
                json.dump(self.data, fh, ensure_ascii=False, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError:
            # persistence is best-effort: never let a disk hiccup break the review loop.
            pass

    # --- internal cell access ---
    @staticmethod
    def _cell_key(bkey: str, lens: str) -> str:
        return bkey + "::" + lens

    def _cell(self, bkey: str, lens: str) -> dict:
        return self.data["cells"].get(self._cell_key(bkey, lens), {"refute": 0, "total": 0})

    def _global_lens_rate(self, lens: str):
        """Laplace-smoothed rejection rate for `lens` across ALL buckets, or None if the
        lens has never been observed anywhere."""
        ref = tot = 0
        suffix = "::" + lens
        for k, v in self.data["cells"].items():
            if k.endswith(suffix):
                ref += v.get("refute", 0)
                tot += v.get("total", 0)
        if tot == 0:
            return None
        return (ref + 1.0) / (tot + 2.0)

    def _lens_total_obs(self, lens: str) -> int:
        """How many observations exist for `lens` across all buckets (for exploration)."""
        tot = 0
        suffix = "::" + lens
        for k, v in self.data["cells"].items():
            if k.endswith(suffix):
                tot += v.get("total", 0)
        return tot

    # --- public API ---
    def record(self, features: dict, lens: str, refuted: bool):
        """Increment the (bucket, lens) counts with this observed verdict and persist."""
        bkey = bucket_key(features)
        ck = self._cell_key(bkey, lens)
        cell = self.data["cells"].get(ck)
        if cell is None:
            cell = {"refute": 0, "total": 0}
            self.data["cells"][ck] = cell
        cell["total"] += 1
        if refuted:
            cell["refute"] += 1
        self._save()

    def rejection_prob(self, features: dict, lens: str) -> float:
        """Smoothed P(this lens refutes a candidate in this bucket).

        Laplace/Beta smoothing (refute+1)/(total+2) on the specific (bucket,lens) cell, with
        backoff: when the cell has few observations (total < _MIN_OBS) we blend toward the
        per-lens GLOBAL rate by an observation-weighted mix, so a thin cell is pulled toward
        the lens's overall behaviour instead of swinging on one or two samples. No data at
        all anywhere for the lens -> the neutral prior (_PRIOR).
        """
        bkey = bucket_key(features)
        cell = self._cell(bkey, lens)
        tot = cell["total"]
        local = (cell["refute"] + 1.0) / (tot + 2.0)   # always defined (smoothed)

        glob = self._global_lens_rate(lens)
        if glob is None:
            # lens never seen anywhere: if the cell itself is also empty, this is just _PRIOR
            # (refute=0,total=0 -> 1/2 = _PRIOR); otherwise the smoothed local estimate.
            return local if tot > 0 else _PRIOR

        if tot >= _MIN_OBS:
            return local
        # weight the (thin) local estimate by how much data it has, vs the global rate.
        w = tot / float(_MIN_OBS)              # 0 .. <1
        return w * local + (1.0 - w) * glob

    def rank_lenses(self, features: dict, candidate_lenses):
        """candidate_lenses sorted by rejection_prob desc; stable tie-break on original order."""
        order = {lens: i for i, lens in enumerate(candidate_lenses)}
        scored = [(lens, self.rejection_prob(features, lens)) for lens in candidate_lenses]
        scored.sort(key=lambda lp: (-lp[1], order[lp[0]]))
        return scored

    def select_lenses(self, features: dict, candidate_lenses, k: int):
        """Pick up to k lenses to actually run for this candidate.

        Top-k by predicted rejection probability, PLUS a deterministic exploration slot: on
        every _EXPLORE_PERIOD-th select call (by the running select count -- NO RNG, which is
        unavailable in this environment) we force-include the LEAST-observed lens so a lens
        starved of data eventually gets sampled and can correct a stale estimate. Always
        returns >=1 lens; k >= len(candidate) returns all of them.
        """
        cands = list(candidate_lenses)
        if not cands:
            return []
        # advance the select counter first so the period is observable/deterministic.
        n_select = self.data.get("selects", 0)
        self.data["selects"] = n_select + 1
        self._save()

        if k >= len(cands):
            return cands
        k = max(1, k)

        ranked = [lens for lens, _ in self.rank_lenses(features, cands)]
        chosen = ranked[:k]

        # Exploration: every _EXPLORE_PERIOD-th call, ensure the least-observed candidate is
        # in the set (swap it in for the weakest currently-chosen if absent).
        if n_select % _EXPLORE_PERIOD == 0:
            order = {lens: i for i, lens in enumerate(cands)}
            least = min(cands, key=lambda L: (self._lens_total_obs(L), order[L]))
            if least not in chosen:
                # drop the lowest-ranked chosen lens (last in `chosen`, since chosen is ranked)
                chosen = chosen[:-1] + [least]
        return chosen
