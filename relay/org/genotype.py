# -*- coding: utf-8 -*-
"""A typed genotype for the fleet's organisation (slice E1-a): which values may be searched, and
what a candidate means once the unwritten ones are filled in.

WHAT DECISION THIS SERVES. Whether a candidate organisation (tree shape, budgets, policy
numbers, effort level) is a valid candidate and a DIFFERENT program from the base, before anyone
spends runs on it. Nothing here runs a candidate, applies one, or writes a file.

NOT IMPORTED BY ANY RUNTIME MODULE. Only scripts/org_genotype_report.py (run by hand) uses it,
the same arrangement as scripts/capability_profile.py. Runtime behaviour is unchanged.

A GENE EXISTS ONLY WHERE A READER EXISTS. Each row of GENES names the function that reads the
value in production. A value nothing reads would make an A/B run the same program twice and
report a p-value about noise (the rule relay/selfimprove/manifest.py states at its parameters).
Role and protocol TEXT is not a gene: no path swaps the wording of a run, so a text gene would be
exactly that label. Text is referenced by the hash relay/org/definitions already records
(`base`), never stored here. Free text of any kind is refused (typed values only).

NOTHING IS COPIED. Ranges are read from fanout.DEPTH_SETTING_BOUNDS, fanout_budget.BOUNDS and
manifest.PARAMETER_TYPES; defaults are read through the live readers. Where fanout.py's comment
at HIERARCHICAL_MERGE_READY says the nested merge is "default off" the reader returns "on"
(HIERARCHICAL_SETTING_DEFAULT); the reader's value is the one used.

IDENTITY IS THE MATERIALISED VALUES. A candidate that names a gene at its base value, and one
that leaves it out, are the same program and get the same hash (the manifest.materialize incident,
where two names gave one harness_id). `same_program` compares hashes of the filled-in values.

manifest.apply_genome is NOT used: manifest.py is frozen and skips any key outside components and
parameters, so a tree gene would pass through it as a no-op and an A/A would look like a pair.
"""
from __future__ import annotations

import hashlib
import json

SCHEMA_VERSION = 1
HASH_LEN = 16


class GenotypeError(ValueError):
    """A candidate that is not a valid genotype (unknown, forbidden, out of range, wrong type)."""


class Gene:
    """One searchable value: its name, kind ("int" | "enum"), allowed range, and live reader."""

    def __init__(self, name, kind, allowed, reader, source):
        self.name, self.kind, self.allowed = name, kind, allowed
        self.reader, self.source = reader, source

    def read(self):
        return self.reader()


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _fanout():
    from relay import fanout
    return fanout


def _budget():
    from relay import fanout_budget
    return fanout_budget


def _manifest():
    from relay.selfimprove import manifest
    return manifest


def _budget_reader(key, field):
    return lambda: _budget().limits_from_settings()[field]


def _param_reader(name):
    return lambda: _manifest().base_manifest()["parameters"][name]


def _effort_reader():
    from relay import fleet_runner
    return fleet_runner.settings_effort()


def _build_genes():
    fo, fb, mf = _fanout(), _budget(), _manifest()
    from relay import effort
    genes = [
        Gene("fanout_max_depth", "int", fo.DEPTH_SETTING_BOUNDS, fo.configured_max_depth,
             "relay/fanout.py configured_max_depth"),
        Gene("fanout_hierarchical_merge", "enum", ("off", "on"), fo.hierarchical_merge_setting,
             "relay/fanout.py hierarchical_merge_setting"),
    ]
    for key, field in ((fb.KEY_TOTAL, "total"), (fb.KEY_ACTIVE, "active"),
                       (fb.KEY_TURNS, "turns"), (fb.KEY_WALL, "wall_min")):
        genes.append(Gene(key, "int", fb.BOUNDS[key], _budget_reader(key, field),
                          "relay/fanout_budget.py limits_from_settings"))
    for name, bounds in mf.PARAMETER_TYPES.items():
        genes.append(Gene(name, "int", bounds, _param_reader(name),
                          "relay/selfimprove/manifest.py base_manifest parameters"))
    genes.append(Gene("effort", "enum", tuple(sorted(effort.LEVELS)), _effort_reader,
                      "relay/fleet_runner.py settings_effort"))
    return {g.name: g for g in genes}


def gene_table():
    """{name: Gene}. Built on each call so ranges are always read from their source modules."""
    return _build_genes()


def current():
    """The live value of every gene, read through the production readers. Never writes."""
    return {name: g.read() for name, g in gene_table().items()}


def _canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _norm(value):
    """Line endings normalised and surrounding space removed, as definitions._norm does."""
    if isinstance(value, str):
        return value.replace("\r\n", "\n").strip()
    if isinstance(value, dict):
        return {str(k): _norm(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    return value


def _check(table, name, value):
    gene = table[name]
    if gene.kind == "int":
        lo, hi = gene.allowed
        if not _is_int(value) or not lo <= value <= hi:
            raise GenotypeError("%s=%r is outside %d..%d (integers only)" % (name, value, lo, hi))
    else:
        if not isinstance(value, str) or value not in gene.allowed:
            raise GenotypeError("%s=%r is not one of %s" % (name, value, list(gene.allowed)))


def validate(genes):
    """Raise GenotypeError unless `genes` names only known genes with in-range typed values.
    A forbidden name (the manifest's FORBIDDEN_COMPONENTS) gets its own message."""
    if not isinstance(genes, dict):
        raise GenotypeError("genes must be an object")
    table = gene_table()
    forbidden = _manifest().FORBIDDEN_COMPONENTS
    for name, value in genes.items():
        if name in forbidden:
            raise GenotypeError("%s is forbidden: it decides what is allowed or judges the run" % name)
        if name not in table:
            raise GenotypeError("unknown gene %r" % (name,))
        _check(table, name, value)


def materialize(genes, base=None):
    """The full gene values a candidate stands for: `base` (default: the live values) with the
    named genes overridden. Raises GenotypeError on an invalid candidate or base."""
    validate(genes or {})
    full = dict(base) if base is not None else current()
    validate(full)
    full.update(genes or {})
    return {name: full[name] for name in sorted(full)}


def genotype_hash(genes, base=None):
    """Hash of the materialised values. Independent of key order, line endings and of whether a
    base-valued gene was written out."""
    full = materialize(genes, base)
    return hashlib.sha256(_canonical({"v": SCHEMA_VERSION, "genes": _norm(full)}
                                     ).encode("utf-8")).hexdigest()[:HASH_LEN]


def same_program(a, b, base=None):
    """True when two candidates materialise to the same values (an A/A wearing two names)."""
    return genotype_hash(a, base) == genotype_hash(b, base)


def diff(a, b, base=None):
    """{gene: (value in a, value in b)} for the genes whose materialised values differ."""
    fa, fb = materialize(a, base), materialize(b, base)
    return {k: (fa[k], fb[k]) for k in fa if fa[k] != fb[k]}


def base_org_id():
    """The organisation-text version the base runs under (definitions.genotype_id), or ""."""
    from relay.org import definitions
    return definitions.genotype_id(definitions.snapshot())


def genotype(genes, parent="", base=None):
    """The record for one candidate: schema_version, materialised genes, base org id, parent, id."""
    full = materialize(genes, base)
    return {"schema_version": SCHEMA_VERSION, "genes": full, "base": base_org_id(),
            "parent": str(parent or ""), "id": genotype_hash(full)}
