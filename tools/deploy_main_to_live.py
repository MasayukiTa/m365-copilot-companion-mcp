"""Preview or apply main changes to a live directory using an explicit baseline.

Run from a full clone: python tools/deploy_main_to_live.py LIVE OLD NEW.
Default is dry-run; --apply requires --backup pointing to a new directory outside
LIVE. Stop the application and back up the entire live checkout first. OLD must
be the last successfully deployed main commit, not an inferred Git index. Review
all reported conflicts, including local deletes, binary edits, modes and links.
Only conflict-free runs write anything. A backup of touched files and a JSON
report is made before writes; failures can still leave a partial deployment, so
restore from the backup if needed. The tool does not change live Git metadata or
record the baseline; retain NEW as the baseline after a successful run. Fetch and
inspect main separately. Do not run while other processes modify the live files.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def git(*args, check=True):
    return subprocess.run(['git', *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def tree(ref):
    entries = {}
    for record in git('ls-tree', '-rz', '--full-tree', ref).stdout.split(b'\0'):
        if not record:
            continue
        meta, name = record.split(b'\t', 1)
        mode, kind, oid = meta.split()
        entries[os.fsdecode(name)] = (mode, kind, oid.decode())
    return entries


def blob(entry):
    return None if entry is None else git('cat-file', 'blob', entry[2]).stdout


def plan(live, old, new):
    before, after = tree(old), tree(new)
    actions, report = [], []
    for name in sorted(before.keys() | after.keys()):
        a, b = before.get(name), after.get(name)
        if a == b:
            continue
        path = live / name
        # Refuse links in any component, unusual Git objects, and mode changes.
        if (any(p.is_symlink() for p in [path, *path.parents] if p != live.parent)
                or any(e and (e[0] != b'100644' or e[1] != b'blob') for e in (a, b))):
            report.append({'path': name, 'status': 'conflict: link or unsupported mode'})
            continue
        if path.exists() and not path.is_file():
            report.append({'path': name, 'status': 'conflict: not a file'})
            continue
        current = path.read_bytes() if path.exists() else None
        base, incoming = blob(a), blob(b)
        if current == base:
            result, status = incoming, 'update'
        elif current == incoming:
            result, status = current, 'already present'
        elif None in (current, base, incoming) or any(b'\0' in x for x in (current, base, incoming)):
            report.append({'path': name, 'status': 'conflict: local change, deletion, addition or binary'})
            continue
        else:
            with tempfile.TemporaryDirectory(dir=Path.cwd()) as td:
                files = [Path(td) / str(i) for i in range(3)]
                for f, content in zip(files, (current, base, incoming)):
                    f.write_bytes(content)
                merged = git('merge-file', '-p', *map(str, files), check=False)
            if merged.returncode:
                report.append({'path': name, 'status': 'conflict: merge failed'})
                continue
            result, status = merged.stdout, 'merge'
        report.append({'path': name, 'status': status})
        if current != result:
            actions.append((name, current, result))
    return actions, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('live', type=Path)
    parser.add_argument('old')
    parser.add_argument('new')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args()
    live = args.live.resolve(strict=True)
    if not live.is_dir():
        parser.error('live must be a directory')
    for ref in (args.old, args.new):
        git('rev-parse', '--verify', ref + '^{commit}')
    if git('merge-base', '--is-ancestor', args.old, args.new, check=False).returncode:
        parser.error('old must be an ancestor of new')
    actions, report = plan(live, args.old, args.new)
    print(json.dumps(report, indent=2, ensure_ascii=True))
    if any(r['status'].startswith('conflict') for r in report):
        return 1
    if not args.apply:
        return 0
    if args.backup is None:
        parser.error('--apply requires --backup')
    backup = args.backup.resolve()
    if backup == live or live in backup.parents or backup.exists():
        parser.error('backup must be a new directory outside live')
    backup.mkdir(parents=True)
    (backup / 'report.json').write_text(json.dumps({'old': args.old, 'new': args.new, 'files': report}, indent=2), encoding='utf-8')
    for name, current, _ in actions:
        if current is not None:
            target = backup / 'files' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(current)
    for name, current, result in actions:
        target = live / name
        if (target.read_bytes() if target.exists() else None) != current:
            raise RuntimeError('live file changed during deployment: ' + name)
        if result is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
