import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tools import search_ops, walk


@pytest.fixture(autouse=True)
def allow_path(monkeypatch):
    monkeypatch.setattr(search_ops, '_validate_path', lambda p: Path(p))


def test_walk_deadline_reports_cut(monkeypatch, tmp_path):
    (tmp_path / 'a').write_text('a')
    times = iter([0, 1, 21])
    monkeypatch.setattr(walk.time, 'monotonic', lambda: next(times))
    deadline = walk.WalkDeadline()
    assert list(walk.iter_files(tmp_path, deadline=deadline)) == []
    assert deadline.cut


@pytest.mark.parametrize('tool,args,normal', [
    (search_ops.find_files, ('missing',), '(no matches)'),
    (search_ops.glob, ('**/*.txt',), None),
])
def test_cut_and_unchanged_empty(monkeypatch, tmp_path, tool, args, normal):
    (tmp_path / 'a.txt').write_text('a')
    normal = normal or f'0 matches under {tmp_path}'
    if tool is search_ops.glob:
        args = ('**/*.missing',)
    assert tool(*args, path=str(tmp_path)) == normal
    monkeypatch.setattr(walk, 'SEARCH_TIMEOUT_SECONDS', 0)
    assert tool(*args, path=str(tmp_path)) == normal + '\n[search cut after 0 seconds; results may be incomplete]'


def test_concurrency_across_both_tools(monkeypatch, tmp_path):
    entered = threading.Barrier(5)
    release = threading.Event()
    start = threading.Barrier(11)
    def blocked(*args, **kwargs):
        entered.wait(timeout=10)
        assert release.wait(10)
        return iter(())
    original = search_ops.iter_files
    monkeypatch.setattr(search_ops, 'iter_files', blocked)
    def call(i):
        start.wait(timeout=10)
        if i % 2:
            return search_ops.glob('**/*.txt', path=str(tmp_path))
        return search_ops.find_files('x', path=str(tmp_path))
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(call, i) for i in range(10)]
        start.wait(timeout=10)
        entered.wait(timeout=10)
        from concurrent.futures import wait
        done, _ = wait(futures, timeout=5)
        busy = [f.result() for f in done]
        assert busy == ['busy: 4 searches already running, retry later'] * 6
        release.set()
        assert sum('busy:' in f.result(timeout=10) for f in futures) == 6
    monkeypatch.setattr(search_ops, 'iter_files', original)
    assert search_ops.find_files('x', str(tmp_path)) == '(no matches)'

@pytest.mark.parametrize('tool,args', [(search_ops.find_files, ('a',)), (search_ops.glob, ('**/*.txt',))])
def test_partial_result_has_cut_note(monkeypatch, tmp_path, tool, args):
    file = tmp_path / 'a.txt'
    file.write_text('a')
    def partial(base, skipped, deadline):
        yield file
        deadline.cut = True
    monkeypatch.setattr(search_ops, 'iter_files', partial)
    result = tool(*args, path=str(tmp_path))
    assert str(file) in result
    assert result.endswith('[search cut after 20 seconds; results may be incomplete]')
