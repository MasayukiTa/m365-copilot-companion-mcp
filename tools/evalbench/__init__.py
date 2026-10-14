"""Generic evaluation bench for natural-language-to-SQL / analysis systems.

Score answers from one or more systems against the same question set with the same rules:
execution accuracy, result checks, interpretation disclosure, confident-wrong detection,
repeat variance, a dev/test split with edit detection, and cross-system comparison.

Standard library plus openpyxl (for the optional Excel report) only.
"""

__all__ = [
    "normalize",
    "sources",
    "questions",
    "answers",
    "score",
    "stats",
    "aggregate",
    "fairness",
    "report",
    "cli",
]
