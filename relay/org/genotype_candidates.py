# -*- coding: utf-8 -*-
"""Candidate generation for the organisation genotype (slice E2): propose neighbours of a parent.

WHAT DECISION THIS SERVES. Which single-part changes to a known genotype are worth spending runs
on. This module only LISTS them; it does not run, apply, record or rank a candidate, and it writes
no file. Nothing in production calls it (default off): only scripts/org_genotype_candidates.py,
run by hand, does.

SHAPE, from relay/selfimprove/diversify.py. The parent is never gambled away and is never returned
as a candidate; candidates are distinct (by genotype hash, so a change that lands on an already
seen program is dropped); the list is deterministic for a given seed; and it is never padded with
duplicates, so fewer than N come back when the neighbourhood holds fewer than N distinct programs.

WHAT A MUTATION IS. Exactly one gene (topology depth, nested-merge strategy, budgets, policy
numbers, effort) is moved to another value the gene already allows (genotype.gene_table(): integer
ranges and enum members read from their source modules). Integer genes move to the adjacent value
on either side or to either end of the allowed range; enum genes move to any other member. No
value comes from outside those sets and no text is ever generated, so every candidate passes
genotype.validate. N is the caller's request, not a cap this module adds.
"""
from __future__ import annotations

import random

from relay.org import genotype as G


def neighbours(gene, value):
    """The other allowed values one step from `value` for `gene`, in a fixed order, no repeats."""
    if gene.kind == "enum":
        return [v for v in gene.allowed if v != value]
    lo, hi = gene.allowed
    out = []
    for v in (value - 1, value + 1, lo, hi):
        if (v is not None and (lo is None or lo <= v)
                and (hi is None or v <= hi) and v != value and v not in out):
            out.append(v)
    return out


def single_part_moves(parent_genes, table=None):
    """Every (gene, old, new) move that changes exactly one gene, in a fixed order."""
    table = table if table is not None else G.gene_table()
    moves = []
    for name in sorted(parent_genes):
        if name not in table:
            raise G.GenotypeError("unknown gene %r" % (name,))
        for new in neighbours(table[name], parent_genes[name]):
            moves.append((name, parent_genes[name], new))
    return moves


def generate(parent_genes, n, seed=0, parent_id=""):
    """Up to `n` candidate records derived from `parent_genes` by one single-gene move each.

    `parent_genes` is the full gene mapping of the parent (e.g. genotype.current()); it is
    validated first. `parent_id` defaults to the parent's genotype hash. Each returned item is
    {"genotype": record, "parent": id, "mutation": {"gene", "from", "to"}, "description": text},
    where `record` is genotype.genotype(...) with `parent` set. Deterministic in (parent, n,
    seed); candidates differ from the parent and from each other as programs."""
    parent_genes = dict(parent_genes)
    G.validate(parent_genes)
    try:
        want = int(n)
    except (TypeError, ValueError):
        want = 0
    pid = str(parent_id or G.genotype_hash(parent_genes, parent_genes))
    moves = single_part_moves(parent_genes)
    random.Random(seed).shuffle(moves)
    seen = {G.genotype_hash(parent_genes, parent_genes)}
    out = []
    for name, old, new in moves:
        if len(out) >= want:
            break
        genes = dict(parent_genes)
        genes[name] = new
        h = G.genotype_hash(genes, genes)
        if h in seen:
            continue
        seen.add(h)
        out.append({"genotype": G.genotype(genes, parent=pid, base=genes), "parent": pid,
                    "mutation": {"gene": name, "from": old, "to": new},
                    "description": "%s %s -> %s" % (name, old, new)})
    return out
