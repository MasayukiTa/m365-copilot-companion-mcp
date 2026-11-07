"""Weekly scan package: B/C confirmation and D re-acquisition drivers.

This package drives the *existing* judge; it never re-implements or alters the
judge, its thresholds, Delta_min, or any correction. The judge is injected as a
callable (judge_fn) so the driver stays a thin, testable shell.
"""
