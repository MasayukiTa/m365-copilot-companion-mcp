# -*- coding: utf-8 -*-
"""sec_report_run -- MCP tool entry for the screen -> estimate -> confirm workflow.

This is the PRODUCTION caller of tools.screen_estimate_confirm.run_workflow. It keeps a
JSON-friendly signature (so the MCP layer can introspect it) and does the glue the pure
workflow deliberately leaves out: read the observation rows from a JSON file, hand them to
run_workflow (which resolves the real registration harness and the engine judge lazily), then
render the plain-Japanese report with tools.sec_report.render_report.

The heavy maths and the registration-order guarantees all live in screen_estimate_confirm;
nothing here re-implements a verdict or a threshold.

Input JSON shape (one object):
    {
      "rows": [{"lot": "L1", "response": 1.2, "factors": {"temp": 0.0},
                "group": "m1", "time": 0}, ...],
      "factors": ["temp", ...],
      "delta_min": 0.3, "k": 1,
      "dataset_id": "...", "exposure_column": "temp",
      "response_column": "y", "unit_column": "lot",
      "rationale": "...", "direction": "positive",
      "group_column": "machine" | null,
      "split_mode": "time" | "random", "screen_frac": 0.5, "seed": null
    }
"""
from __future__ import annotations

import json
import os

from tools.screen_estimate_confirm import Row, run_workflow
from tools.sec_report import render_report


def _rows_from(spec) -> list:
    rows = []
    for r in spec:
        rows.append(Row(lot=str(r["lot"]), response=float(r["response"]),
                        factors=dict(r.get("factors") or {}),
                        group=(r.get("group")),
                        time=(None if r.get("time") is None else float(r["time"]))))
    return rows


def sec_report_run(input_path: str, work_dir: str) -> dict:
    """Run the confirmatory workflow described by `input_path` and return the report.

    `work_dir` is where the confirm file, the lock registry and the engine output are written
    (all under the caller's own worktree). Returns a dict with the rendered report text, the
    per-factor verdicts, and the paths that were written.
    """
    with open(input_path, encoding="utf-8") as fh:
        spec = json.load(fh)
    rows = _rows_from(spec["rows"])
    os.makedirs(work_dir, exist_ok=True)
    confirm_path = os.path.join(work_dir, "confirm_rows.json")
    out_dir = os.path.join(work_dir, "engine_out")
    root = work_dir
    lock_path = os.path.join(work_dir, "registry", "lock_log.jsonl")

    outcomes = run_workflow(
        rows, list(spec["factors"]),
        delta_min=float(spec["delta_min"]), k=int(spec["k"]),
        dataset_id=str(spec["dataset_id"]),
        exposure_column=str(spec["exposure_column"]),
        response_column=str(spec["response_column"]),
        unit_column=str(spec["unit_column"]),
        confirm_path=confirm_path, lock_path=lock_path, out_dir=out_dir, root=root,
        rationale=str(spec["rationale"]),
        direction=str(spec.get("direction", "positive")),
        group_column=spec.get("group_column"),
        split_mode=str(spec.get("split_mode", "time")),
        screen_frac=float(spec.get("screen_frac", 0.5)),
        seed=spec.get("seed"))

    report = render_report(outcomes)
    return {
        "report": report,
        "verdicts": [{"factor": o.factor, "verdict": o.verdict} for o in outcomes],
        "confirm_path": confirm_path,
        "n_confirmed": len(outcomes),
    }
