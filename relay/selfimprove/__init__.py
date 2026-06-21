"""Self-improvement controller primitives.

The guardrails here encode the judgment a human currently supplies to the improvement loop
(see bench/SELF_IMPROVEMENT_CONTROLLER.md): burned-instance hygiene, overfit detection, statistical
significance gating, infra-vs-real outcome classification, and durable process discipline.
"""
from .guards import (
    BurnedRegistry,
    overfit_lint,
    is_domain_general,
    mcnemar_exact_p,
    significance_gate,
    classify_outcome,
    partition_outcomes,
    proc_alive,
    launch_detached,
    done_after_last_start,
)
from .archive import (
    Archive,
    genome_id,
    descriptors,
    cell_key,
)

__all__ = [
    "BurnedRegistry",
    "overfit_lint",
    "is_domain_general",
    "mcnemar_exact_p",
    "significance_gate",
    "classify_outcome",
    "partition_outcomes",
    "proc_alive",
    "launch_detached",
    "done_after_last_start",
    "Archive",
    "genome_id",
    "descriptors",
    "cell_key",
]
