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
from .frozen import (
    frozen_intact,
    snapshot_baseline,
    compute_checksums,
    burned_append_only,
    FROZEN_MANIFEST,
)
from .sentinel import (
    Sentinel,
    sentinel_verdict,
)
from .propose import (
    propose_candidates,
    lint_candidate,
    mutation_generator,
)
from .l2 import (
    run_iteration,
    SpendCeiling,
    run_until,
)
from .policy import (
    DatasetRotation,
    plateaued,
    evaluate_tripwires,
    run_campaign,
)
from .status import status_text
from .targeting import (
    next_target,
    assemble_misses,
    improvement_plan,
)
from .calibration import (
    calibration_report,
    competence,
    recommend_effort,
    classify_instance,
)
from .dashboard import (
    dashboard_state,
    render_text,
)
from .apply import (
    active_genome,
    apply_genome,
    revert,
    safe_commit,
    SCAFFOLD_ALLOWLIST,
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
    "frozen_intact",
    "snapshot_baseline",
    "compute_checksums",
    "burned_append_only",
    "FROZEN_MANIFEST",
    "Sentinel",
    "sentinel_verdict",
    "propose_candidates",
    "lint_candidate",
    "mutation_generator",
    "run_iteration",
    "SpendCeiling",
    "run_until",
    "DatasetRotation",
    "plateaued",
    "evaluate_tripwires",
    "run_campaign",
    "active_genome",
    "apply_genome",
    "revert",
    "safe_commit",
    "SCAFFOLD_ALLOWLIST",
    "dashboard_state",
    "render_text",
    "calibration_report",
    "competence",
    "recommend_effort",
    "classify_instance",
    "next_target",
    "assemble_misses",
    "improvement_plan",
    "status_text",
]
