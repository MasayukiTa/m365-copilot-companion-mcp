import json

import pytest

from tools import closed_worker_gate as gate, tool_ledger


@pytest.fixture
def state(tmp_path, monkeypatch):
    root = tmp_path / '.fleet'
    root.mkdir()
    monkeypatch.setattr(tool_ledger, 'LEDGER_PATH', str(root / 'tool_events.jsonl'))
    gate._cache = (None, 0.0, frozenset())
    (root / 'status.json').write_text(json.dumps({'workers': [
        {'name': 'w1', 'status': 'cancelled', 'jid': 'j1'},
        {'name': 'w2', 'status': 'running', 'jid': 'j2'}]}), encoding='utf-8')
    return root


def call(worker='w1', attr='explicit', side_effecting=True):
    row = {}
    tool_ledger.record_call('run_python', {}, worker=worker, record=row)
    row['attr'] = attr
    return gate.check('run_python', row, side_effecting)


def events(root):
    return [json.loads(line) for line in (root / 'tool_events.jsonl').read_text(encoding='utf-8').splitlines()]


def test_closed_explicit_refused(state):
    assert call() == gate.REFUSAL
    row = events(state)[-1]
    assert (row['event'], row['worker'], row['tool']) == ('tool_refused_closed_worker', 'w1', 'run_python')
    assert row['ts'] > 0


def test_open_passes(state):
    assert call('w2') is None


def test_read_error_fails_open(state):
    (state / 'status.json').write_text('{', encoding='utf-8')
    assert call() is None


def test_no_identity_passes(state):
    assert gate.check('run_python', {}, True) is None


@pytest.mark.parametrize('attr', ['window', 'session-window'])
def test_inferred_passes_and_logs(state, attr):
    assert call(attr=attr) is None
    assert events(state)[-1]['event'] == 'tool_gate_skipped_inferred'


def test_read_only_passes(state):
    assert call(side_effecting=False) is None


def test_tombstone_jid_refuses(state):
    (state / 'status.json').write_text(json.dumps({'workers': [
        {'name': 'w1', 'status': 'running', 'jid': 'j1'}]}), encoding='utf-8')
    folder = state / 'tasks' / 'closed'
    folder.mkdir(parents=True)
    (folder / 'j1.json').write_text(json.dumps({'jid': 'j1', 'events': [{'worker': 'w1'}]}), encoding='utf-8')
    assert call() == gate.REFUSAL


def test_tombstone_event_for_reused_name_passes(state):
    (state / 'status.json').write_text(json.dumps({'workers': [
        {'name': 'w1', 'status': 'running', 'jid': 'new-job'}]}), encoding='utf-8')
    folder = state / 'tasks' / 'closed'
    folder.mkdir(parents=True)
    (folder / 'old-job.json').write_text(json.dumps({
        'jid': 'old-job', 'events': [{'worker': 'w1'}]}), encoding='utf-8')
    assert call() is None


def test_direct_registration_refuses_before_execution(state, monkeypatch):
    from tools import registry

    ran = []
    def write_something():
        ran.append(True)
        return 'executed'

    monkeypatch.setattr(tool_ledger, '_identity_for',
                        lambda *args: ('', 'w1', 'session'))
    wrapped = registry._wrap_for_evidence(write_something, write_something, True)
    assert wrapped() == gate.REFUSAL
    assert ran == []
