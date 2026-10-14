"""Run profile -> candidates -> verification end to end.

    python -m tools.reldisc.run --source sqlite:<path> --out <store.sqlite> [--max-pairs N --budget-s S]
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

from .candidates import generate_candidates
from .profile import ProfileConfig, ProfileStats, profile_source
from .source import Source, open_source
from .store import DiscoveryStore
from .verify import PairBudget, verify_pair


@dataclass
class DiscoveryReport:
    profile: ProfileStats
    candidates: int = 0
    verified: int = 0
    reused: int = 0
    unverified: int = 0
    rows_fetched: int = 0
    elapsed_s: float = 0.0
    joins: list = field(default_factory=list)


def discover(source: Source, store: DiscoveryStore, *, profile_cfg: Optional[ProfileConfig] = None,
             pair_budget: Optional[PairBudget] = None, max_pairs: int = 200, budget_s: float = 600.0,
             aliases: Optional[Mapping[str, Iterable[str]]] = None, min_score: float = 0.3,
             top: int = 20) -> DiscoveryReport:
    """Profile the source, generate candidates, verify the best ``max_pairs`` of them.

    ``budget_s`` is the wall-clock for the whole run: profiling may use at most half of it,
    and pairs left when it runs out are recorded as unverified rather than silently dropped.
    """
    cfg = profile_cfg or ProfileConfig()
    pair_budget = pair_budget or PairBudget()
    t0 = time.monotonic()
    run_deadline = t0 + budget_s
    profiles, pstats = profile_source(source, cfg, store, deadline=t0 + budget_s * 0.5)
    by_key = {(p.table, p.column): p for p in profiles}

    cands = generate_candidates(profiles, aliases, min_score=min_score)
    store.put_candidates(cands)
    store.commit()
    report = DiscoveryReport(profile=pstats, candidates=len(cands))

    cache: dict = {}  # distinct-value samples, read once per column and shared by its pairs
    for c in cands[:max_pairs]:
        a, b = by_key[(c.a_table, c.a_column)], by_key[(c.b_table, c.b_column)]
        prior = store.get_verification(c.a_table, c.a_column, c.b_table, c.b_column)
        if prior and prior["a_hash"] == a.sample_hash and prior["b_hash"] == b.sample_hash \
                and prior["status"] == "verified":
            report.reused += 1
            continue
        remaining = run_deadline - time.monotonic()
        budget = PairBudget(time_s=min(pair_budget.time_s, max(0.0, remaining)), rows=pair_budget.rows,
                            sample_values=pair_budget.sample_values, batch=pair_budget.batch)
        res = verify_pair(source, a, b, budget, name_score=c.name_score, seed=cfg.seed, cache=cache)
        reason = res.reason if remaining > 0 else "run_budget"
        store.put_verification(c.a_table, c.a_column, c.b_table, c.b_column, a.sample_hash, b.sample_hash,
                               res.status, reason,
                               res.confidence, {**res.evidence, "a_in_b": res.a_in_b, "b_in_a": res.b_in_a,
                                                "jaccard": res.jaccard, "cardinality": res.cardinality,
                                                "candidate_score": c.score, "reasons": c.reasons})
        if res.status == "verified":
            report.verified += 1
        else:
            report.unverified += 1

        if (report.verified + report.unverified) % 10 == 0:
            store.commit()  # a long run survives an interruption with its finished pairs

    store.commit()
    report.rows_fetched = source.rows_fetched
    report.elapsed_s = round(time.monotonic() - t0, 3)
    report.joins = store.top_joins(top)
    return report


def format_joins(joins: list) -> str:
    head = f"{'conf':>5}  {'A (referencing)':<34} {'B (key side)':<34} {'A<B':>5} {'B<A':>5} {'jacc':>5}  card"
    lines = [head, "-" * len(head)]
    for j in joins:
        ev = j["evidence"]
        fmt = lambda v: "  -  " if v is None else f"{v:5.2f}"  # noqa: E731
        lines.append(
            f"{j['confidence']:5.2f}  {j['a_table'] + '.' + j['a_column']:<34.34} "
            f"{j['b_table'] + '.' + j['b_column']:<34.34} {fmt(ev.get('a_in_b'))} {fmt(ev.get('b_in_a'))} "
            f"{fmt(ev.get('jaccard'))}  {ev.get('cardinality', '')}")
    if len(lines) == 2:
        lines.append("(no verified joins)")
    return "\n".join(lines)


def _parse_aliases(items: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for item in items:
        key, _, rest = item.partition("=")
        if key and rest:
            out[key] = [v for v in rest.split(",") if v]
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.reldisc.run", description=__doc__)
    ap.add_argument("--source", required=True, help="sqlite:<path> or odbc:<NAME> (MCP_DB_<NAME>)")
    ap.add_argument("--out", required=True, help="path of the discovery store (SQLite file) to create/update")
    ap.add_argument("--max-pairs", type=int, default=200, help="verify at most this many candidate pairs")
    ap.add_argument("--budget-s", type=float, default=600.0, help="wall-clock budget for the whole run")
    ap.add_argument("--pair-budget-s", type=float, default=10.0, help="time budget per verified pair")
    ap.add_argument("--pair-rows", type=int, default=50_000, help="rows transferred per verified pair")
    ap.add_argument("--sample-rows", type=int, default=20_000, help="rows sampled per table")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--alias", action="append", default=[], metavar="WORD=SYN[,SYN]",
                    help="extra name synonyms, e.g. lot=ロット (repeatable)")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args(argv)

    source = open_source(args.source)
    store = DiscoveryStore(args.out)
    try:
        rep = discover(
            source, store,
            profile_cfg=ProfileConfig(sample_rows=args.sample_rows, seed=args.seed),
            pair_budget=PairBudget(time_s=args.pair_budget_s, rows=args.pair_rows),
            max_pairs=args.max_pairs, budget_s=args.budget_s, aliases=_parse_aliases(args.alias), top=args.top)
    finally:
        source.close()
        store.close()
    p = rep.profile
    print(f"tables={p.tables_done} (skipped {p.tables_skipped}) columns profiled={p.columns_profiled} "
          f"reused={p.columns_reused} candidates={rep.candidates} verified={rep.verified} "
          f"reused_verifications={rep.reused} unverified={rep.unverified} "
          f"rows_fetched={rep.rows_fetched} elapsed_s={rep.elapsed_s}")
    print(format_joins(rep.joins))
    return 0


if __name__ == "__main__":
    sys.exit(main())
