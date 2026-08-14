"""CompanionBench: deterministic, machine-verifiable episodes for THIS product.

SWE-bench measures a different job. It says nothing about whether the companion edits the
right cell, refuses an instruction hidden in a spreadsheet, or resumes a job after the
browser was restarted -- which is the entire product. This suite exists so harness changes
can be judged against the work the harness actually does.

Importing this package registers every episode into bench.companionbench.pools.REGISTRY.
"""
from bench.companionbench import episode, pools          # noqa: F401
from bench.companionbench.episodes import core           # noqa: F401  (registers episodes)

__all__ = ["episode", "pools"]
