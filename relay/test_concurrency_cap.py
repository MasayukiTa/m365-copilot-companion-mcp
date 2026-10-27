from types import SimpleNamespace

import pytest
from relay import fleet_runner as fr, relay_fleet as rf
from relay import test_admission as fixtures
from tools import settings_keys


def test_count_and_settings(monkeypatch):
    workers = [SimpleNamespace(status=s) for s in
               ['waiting', 'ready', 'refuting', 'researching', 'verifying', 'stuck', 'done', 'pending']]
    assert rf.active_worker_count(workers) == 5
    monkeypatch.setattr(fr, '_settings_int', lambda key, default: {'maxtabs': 23, 'priority_extra_slots': 4}.get(key, default))
    assert fr.lane_setting('maxtabs') == 23
    assert rf.priority_extra_slots() == 4
    assert settings_keys.default('maxtabs') == settings_keys.default('autoscale_max') == 50
    assert settings_keys.default('priority_extra_slots') == 10
    assert rf.concurrency_admits(workers, 5, True, rf.priority_extra_slots())
    assert not rf.concurrency_admits(workers, 5, False, 4)


@pytest.mark.parametrize('socket', [False, True])
@pytest.mark.parametrize('extra', [4, 10])
def test_live_admission_priority_and_resume(monkeypatch, tmp_path, socket, extra):
    monkeypatch.setattr(rf, 'avail_phys_mb', lambda: 1000000.0)
    monkeypatch.setattr(rf, 'free_disk_gb', lambda path=None: 1000000.0)
    settings = tmp_path / 'settings.txt'
    settings.write_text('priority_extra_slots=%d\n' % extra, encoding='utf-8')
    monkeypatch.setattr(fr, '_settings_path', lambda: str(settings))
    state = {'control': {}}
    orig = (fixtures._install_fake_socket_worker(state) if socket else fixtures._install_fake_worker(state))
    added = []
    observations = []
    stop = [False]
    try:
        def tick(workers):
            active = rf.active_worker_count(workers)
            if not observations and active == 50:
                observations.append((active, sum(w.status == 'pending' for w in workers)))
                added.extend({'text': 'priority%d' % i, 'priority': True} for i in range(12))
            elif len(observations) == 1 and len(workers) == 64:
                observations.append((active, sum(w.priority and w.status == 'pending' for w in workers),
                                     sum(not w.priority and w.status == 'pending' for w in workers)))
                for w in workers:
                    if w.status == 'waiting':
                        state['control'][w.name] = 'done'
                        break
            elif (len(observations) == 2 and active == 50 + extra and
                  sum(w.priority and w.status == 'pending' for w in workers) == 11 - extra):
                observations.append((active, sum(w.priority and w.status == 'pending' for w in workers),
                                     sum(not w.priority and w.status == 'pending' for w in workers)))
                for w in workers:
                    if w.status == 'waiting':
                        state['control'][w.name] = 'done'
            elif len(observations) >= 3:
                for w in workers:
                    if w.status == 'waiting':
                        state['control'][w.name] = 'done'
            ticks[0] += 1
            if ticks[0] > 100:
                stop[0] = True
        ticks = [0]
        result = rf.run_relay_fleet(fixtures.FakeContext(), ['normal%d' % i for i in range(52)],
                                    'http://agent', max_concurrent=50, add_box=added, stop_box=stop,
                                    poll_s=0, on_tick=tick, notify=lambda *a, **k: None)
        assert observations == [(50, 2), (50 + extra, 12 - extra, 2),
                                (50 + extra, 11 - extra, 2)]
        assert len(result) == 64 and all(r['outcome'] == 'DONE' for r in result)
    finally:
        fixtures._restore_worker(orig)
