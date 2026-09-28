from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from relay import local_loop_controller as ll
from relay.local_job_store import LocalJobStore


def _args(tmp_path):
    return SimpleNamespace(
        state_dir=str(tmp_path), db=str(tmp_path / 'jobs.sqlite3'),
        cdp_url='http://localhost:9222', commands_file=None,
        poll_seconds=1.0, turn_timeout=1800.0, ui_idle_timeout=300.0,
        rotate_after_turns=5, js_heap_limit_mb=0.0, dom_node_limit=0,
        edge_mb_limit=0.0,
    )


def test_enqueue_campaign_is_durable_and_includes_the_active_root(tmp_path):
    store = LocalJobStore(tmp_path / 'jobs.sqlite3')
    root = ll._job_from_goal('root task', job_id='root_job')
    store.create_job(root, now=1)
    ll._write_controller_marker(tmp_path, 'root_job', ['--job-id', 'root_job'], pid=111, started=1)

    result = ll._enqueue_campaign_goals(
        store, tmp_path, ['follow-up A', 'follow-up B'], now=10,
    )

    assert result['ok'] is True
    assert len(result['job_ids']) == 2
    manifest = ll._read_campaign_manifest(tmp_path)
    ids = [e['job_id'] for e in manifest['entries']]
    assert ids[0] == 'root_job'
    assert set(result['job_ids']).issubset(set(ids))
    by_id = {e['job_id']: e for e in manifest['entries']}
    for job_id in result['job_ids']:
        assert by_id[job_id]['job']['execution_profile'] == 'LOCAL_LOOP'
        assert store.get_job_status(job_id)['status'] == 'READY'


def test_enqueue_starts_a_new_campaign_when_old_manifest_is_terminal_and_unowned(tmp_path):
    store = LocalJobStore(tmp_path / 'jobs.sqlite3')
    old = ll._job_from_goal('old task', job_id='old_job')
    store.create_job(old, now=1)
    store.cancel_job('old_job', 'finished old campaign', now=2)
    ll._write_atomic(tmp_path / ll.LOCAL_LOOP_CAMPAIGN_MANIFEST, {
        'version': 1, 'started': 1, 'updated': 2,
        'entries': [{'job_id': 'old_job', 'job': old, 'enqueued_at': 1}],
    })

    result = ll._enqueue_campaign_goals(store, tmp_path, ['new task'], now=10)
    manifest = ll._read_campaign_manifest(tmp_path)
    ids = [e['job_id'] for e in manifest['entries']]
    assert 'old_job' not in ids
    assert ids == result['job_ids']


def test_campaign_drain_materializes_manifest_job_and_launches_with_isolated_command_file(tmp_path):
    store = LocalJobStore(tmp_path / 'jobs.sqlite3')
    job = ll._job_from_goal('recover from manifest', job_id='queued_job')
    ll._write_atomic(tmp_path / ll.LOCAL_LOOP_CAMPAIGN_MANIFEST, {
        'version': 1, 'started': 1, 'updated': 1,
        'entries': [{
            'job_id': 'queued_job', 'job': job, 'enqueued_at': 1,
            'launch_attempts': 0, 'retry_after': 0,
        }],
    })
    launched = []

    def launcher(argv):
        launched.append(list(argv))
        return 4321

    result = ll._drain_campaign(store, tmp_path, _args(tmp_path), now=10, launcher=launcher)

    assert result['launched'] == ['queued_job']
    assert store.get_job_status('queued_job')['status'] == 'READY'
    assert len(launched) == 1
    argv = launched[0]
    assert argv[:2] == ['--job-id', 'queued_job']
    assert '--commands-file' in argv
    command_path = Path(argv[argv.index('--commands-file') + 1])
    assert command_path.name == 'queued_job.json'
    assert command_path.parent.name == 'local_loop_commands'
    assert 'recover from manifest' not in ' '.join(argv)


def test_campaign_launch_reservation_prevents_duplicate_spawn_until_backoff_expires(tmp_path):
    store = LocalJobStore(tmp_path / 'jobs.sqlite3')
    ll._enqueue_campaign_goals(store, tmp_path, ['one durable task'], now=1)
    calls = []

    def launcher(argv):
        calls.append(list(argv))
        return 999

    first = ll._drain_campaign(store, tmp_path, _args(tmp_path), now=2, launcher=launcher)
    second = ll._drain_campaign(store, tmp_path, _args(tmp_path), now=2.5, launcher=launcher)
    assert len(first['launched']) == 1
    assert second['launched'] == []
    assert len(calls) == 1


def test_campaign_marker_owned_job_is_never_relaunched_by_drain(tmp_path):
    store = LocalJobStore(tmp_path / 'jobs.sqlite3')
    result = ll._enqueue_campaign_goals(store, tmp_path, ['already running'], now=1)
    job_id = result['job_ids'][0]
    ll._write_controller_marker(tmp_path, job_id, ['--job-id', job_id], pid=777, started=2)
    calls = []

    drained = ll._drain_campaign(
        store, tmp_path, _args(tmp_path), now=5,
        launcher=lambda argv: calls.append(list(argv)) or 888,
    )
    assert drained['launched'] == []
    assert calls == []


def test_enqueue_goals_file_is_json_array_and_rejects_empty(tmp_path):
    p = tmp_path / 'goals.json'
    p.write_text(json.dumps([' alpha ', 'beta']), encoding='utf-8')
    assert ll._read_enqueue_goals_file(p) == ['alpha', 'beta']
    p.write_text('[]', encoding='utf-8')
    try:
        ll._read_enqueue_goals_file(p)
    except ValueError as exc:
        assert 'empty' in str(exc).lower()
    else:
        raise AssertionError('empty enqueue file must be rejected')


def test_cli_exposes_enqueue_and_campaign_drain_modes():
    source = Path(ll.__file__).read_text(encoding='utf-8')
    assert '"--enqueue-goals-file"' in source
    assert '"--drain-campaign"' in source
    assert '_enqueue_campaign_goals(' in source
    assert '_drain_campaign(' in source


def test_terminal_campaign_is_archived_so_supervisor_does_not_drain_forever(tmp_path):
    store = LocalJobStore(tmp_path / 'jobs.sqlite3')
    result = ll._enqueue_campaign_goals(store, tmp_path, ['finish me'], now=1)
    job_id = result['job_ids'][0]
    store.cancel_job(job_id, 'terminal for archive test', now=2)

    drained = ll._drain_campaign(store, tmp_path, _args(tmp_path), now=3, launcher=lambda argv: 999)

    assert drained['closed'] is True
    assert not (tmp_path / ll.LOCAL_LOOP_CAMPAIGN_MANIFEST).exists()
    archived = list((tmp_path / ll.LOCAL_LOOP_CAMPAIGN_HISTORY_DIR).glob('*.json'))
    assert len(archived) == 1
    data = json.loads(archived[0].read_text(encoding='utf-8'))
    assert data['closed'] == 3
    assert data['entries'][0]['job_id'] == job_id


def test_marker_owned_manifest_entry_keeps_campaign_active_even_if_store_row_is_missing(tmp_path):
    store = LocalJobStore(tmp_path / 'jobs.sqlite3')
    ll._write_atomic(tmp_path / ll.LOCAL_LOOP_CAMPAIGN_MANIFEST, {
        'version': 1, 'started': 1, 'updated': 1,
        'entries': [{'job_id': 'root_elsewhere', 'joined_at': 1}],
    })
    ll._write_controller_marker(
        tmp_path, 'root_elsewhere', ['--job-id', 'root_elsewhere'], pid=777, started=1,
    )

    drained = ll._drain_campaign(store, tmp_path, _args(tmp_path), now=10, launcher=lambda argv: 999)

    assert drained['closed'] is False
    assert drained['launched'] == []
    assert (tmp_path / ll.LOCAL_LOOP_CAMPAIGN_MANIFEST).exists()
