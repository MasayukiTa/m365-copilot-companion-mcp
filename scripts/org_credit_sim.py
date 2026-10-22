# -*- coding: utf-8 -*-
"""Read-only: synthetic credit economy simulation (G6). No real money is modeled.

WHAT DECISION THIS SERVES. Whether a credit rule (reward, cost, upkeep, transfer cap) keeps a
population of agents healthy or lets credits concentrate or agents go bankrupt (rules and config
format: relay/org/credit_sim.py). Every number is a synthetic counter in one in-memory run. It reads
an optional JSON config, prints the result, writes no file, touches no .fleet directory, runs no git,
never calls the network, the fleet or a model, has no clock, and no runtime module imports it.

Usage:
  python scripts/org_credit_sim.py [--config FILE.json] [--format text|json]
Without --config a built-in fabricated example is used.

Exit code: 0 PASS (flags are informational), 1 FAIL (config error or conservation violation),
2 refused (the file cannot be read or is not JSON).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def main(argv=None):
    from relay.org import credit_sim as C
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    config = C.default_config()
    if args.config:
        try:
            with open(args.config, encoding="utf-8-sig") as fh:
                config = json.loads(fh.read())
        except (OSError, ValueError) as exc:
            sys.stderr.write("refused: %s\n" % exc)
            return 2
    report = C.simulate(config)
    if args.format == "json":
        text = C.to_json(report)
    else:
        lines = ["credit simulation (synthetic, no real money): %s" % ("PASS" if report["passed"] else "FAIL")]
        for e in report["config_errors"]:
            lines.append("  config error [%s]: %s" % (e["code"], e["reason"]))
        if report["final"] is not None:
            f, c = report["final"], report["conservation"]
            lines.append("  %d agents, %d rounds, seed %s" % (report["agents"], len(report["rounds"]), config.get("seed")))
            lines.append("  final: gini=%.4f top_share=%.4f bankrupt=%d held=%d"
                         % (f["gini"], f["top_share"], f["bankrupt"], f["held"]))
            lines.append("  conservation: created=%d destroyed=%d held=%d balanced=%s"
                         % (c["created"], c["destroyed"], c["held"], c["balanced"]))
            for k, v in f["balances"].items():
                lines.append("    %s: %d%s" % (k, v, " (bankrupt)" if v == 0 else ""))
            for fl in report["flags"]:
                lines.append("  flag %s from round %d" % (fl["code"], fl["first_round"]))
            for v in report["violations"]:
                lines.append("  violation round %d [%s]: %s" % (v["round"], v["code"], v["reason"]))
        text = "\n".join(lines)
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
