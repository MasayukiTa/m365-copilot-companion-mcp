"""Operator CLI:  python -m tools.sqllib <command>

Everything here is optional: the library works without ever running it. It exists to
certify, deprecate, inspect, edit and move entries.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import store


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m tools.sqllib")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("certify", help="mark an entry certified (a person's OK)")
    p.add_argument("id", type=int)
    p.add_argument("--by", required=True)
    p = sub.add_parser("deprecate", help="retire an entry")
    p.add_argument("id", type=int)
    p.add_argument("--note")
    p = sub.add_parser("list")
    p.add_argument("--status", choices=store.STATUSES)
    p = sub.add_parser("show")
    p.add_argument("id", type=int)
    p = sub.add_parser("edit", help="edit SQL/question; validated/certified entries return to draft")
    p.add_argument("id", type=int)
    p.add_argument("--sql")
    p.add_argument("--sql-file")
    p.add_argument("--question")
    p.add_argument("--notes")
    p = sub.add_parser("import")
    p.add_argument("path")
    p.add_argument("--trust-status", action="store_true",
                   help="keep validated/certified/deprecated from the file (default: all draft)")
    p.add_argument("--by")
    p = sub.add_parser("export")
    p.add_argument("path")
    p.add_argument("--status", choices=store.STATUSES)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "certify":
            _print(store.certify(args.id, args.by))
        elif args.cmd == "deprecate":
            _print(store.deprecate(args.id, args.note))
        elif args.cmd == "list":
            for e in store.list_entries(args.status):
                print("%d\t%s%s\t%s" % (e["id"], e["status"], "*" if e["needs_review"] else "",
                                         e["question"]))
        elif args.cmd == "show":
            e = store.get(args.id)
            e["history"] = store.history(args.id)
            _print(e)
        elif args.cmd == "edit":
            sql = args.sql
            if args.sql_file:
                with open(args.sql_file, "r", encoding="utf-8-sig") as fh:
                    sql = fh.read()
            _print(store.update_entry(args.id, sql=sql, question=args.question, notes=args.notes))
        elif args.cmd == "import":
            _print(store.import_jsonl(args.path, args.trust_status, args.by))
        elif args.cmd == "export":
            print("exported %d" % store.export_jsonl(args.path, args.status))
    except (store.SqllibNotConfigured, KeyError, ValueError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
