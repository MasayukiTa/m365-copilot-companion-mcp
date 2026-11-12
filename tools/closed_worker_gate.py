"""Fail-open gateway guard for calls attributed to operator-closed workers."""
import json
import os
import threading
import time

from tools import tool_ledger

REFUSAL = 'this worker was closed by the operator; stop and report'
ALWAYS_SIDE_EFFECTING = {'run_python', 'shell_exec', 'pwsh_exec', 'pwsh_exec_file',
                         'run_in_background', 'run_python_in_background', 'write_file',
                         'edit_and_verify'}
_lock = threading.Lock()
_cache = (None, 0.0, frozenset())


def _closed_workers(state_dir):
    global _cache
    now = time.monotonic()
    with _lock:
        if _cache[0] == state_dir and now - _cache[1] < 5:
            return _cache[2]
        closed = set()
        status_path = os.path.join(state_dir, 'status.json')
        try:
            with open(status_path, encoding='utf-8-sig') as f:
                status = json.load(f)
        except FileNotFoundError:
            status = {}
        workers = status.get('workers', [])
        tomb_dir = os.path.join(state_dir, 'tasks', 'closed')
        try:
            names = os.listdir(tomb_dir)
        except FileNotFoundError:
            names = []
        tombstones = set()
        for name in names:
            if not name.endswith('.json'):
                continue
            with open(os.path.join(tomb_dir, name), encoding='utf-8-sig') as f:
                item = json.load(f)
            tombstones.add(str(item.get('jid', '')))
            for event in item.get('events', []):
                if event.get('worker'):
                    closed.add(str(event['worker']))
        for worker in workers:
            name = str(worker.get('name') or worker.get('worker') or '')
            jid = str(worker.get('jid') or worker.get('task_id') or '')
            if name and (worker.get('status') == 'cancelled' or jid in tombstones and jid):
                closed.add(name)
        _cache = (state_dir, now, frozenset(closed))
        return _cache[2]


def check(tool, row, side_effecting):
    """Return refusal or None; all bookkeeping and read failures permit the call."""
    try:
        worker = row.get('worker', '')
        if not side_effecting or not worker:
            return None
        attr = row.get('attr', '')
        if attr in ('window', 'session-window'):
            tool_ledger._append({'event': 'tool_gate_skipped_inferred', 'worker': worker,
                                 'tool': tool, 'ts': time.time()})
            return None
        if attr not in ('explicit', 'session'):
            return None
        state_dir = os.path.dirname(tool_ledger._repo_path())
        if worker in _closed_workers(state_dir):
            try:
                tool_ledger._append({'event': 'tool_refused_closed_worker', 'worker': worker,
                                     'tool': tool, 'ts': time.time()})
            except Exception:
                pass
            return REFUSAL
    except Exception:
        pass
    return None
