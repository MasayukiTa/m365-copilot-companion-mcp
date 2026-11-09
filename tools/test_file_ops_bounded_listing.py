import time

from tools import file_ops


def test_listing_exact_format(tmp_path):
    (tmp_path / 'alpha').mkdir()
    (tmp_path / 'alpha' / 'nested.txt').write_bytes(b'abc')
    (tmp_path / 'z.txt').write_bytes(b'hello')
    assert file_ops.list_directory(str(tmp_path)) == (
        f'1 files, 1 directories in {tmp_path}\n[DIR] alpha\n[FILE] z.txt (5 bytes)')
    assert file_ops.list_directory(str(tmp_path), recursive=True) == (
        f'2 files, 1 directories in {tmp_path}\n[DIR] alpha\n'
        '[FILE] alpha\\nested.txt (3 bytes)\n[FILE] z.txt (5 bytes)'.replace('\\', __import__('os').sep))


def test_recursive_pattern(tmp_path):
    (tmp_path / 'a').mkdir()
    (tmp_path / 'a' / 'one.txt').write_bytes(b'x')
    (tmp_path / 'a' / 'two.md').write_bytes(b'xx')
    assert file_ops.list_directory(str(tmp_path), recursive=True, pattern='*.txt') == (
        f'1 files matching *.txt in {tmp_path}\n[FILE] {__import__("os").path.join("a", "one.txt")} (1 bytes)')


def test_recursive_deadline_returns_partial_result(tmp_path, monkeypatch):
    for name in ('a', 'b', 'c'):
        (tmp_path / name).touch()
    ticks = iter([0, 0, 0, 0, 0, 0, 3])
    monkeypatch.setattr(file_ops.time, 'monotonic', lambda: next(ticks, 3))
    monkeypatch.setenv('MCP_LIST_DIRECTORY_TIMEOUT_SECONDS', '2')
    output = file_ops.list_directory(str(tmp_path), recursive=True)
    assert '(stopped after 2 s; more may exist)' in output.splitlines()[0]
    assert '[FILE] a (0 bytes)' in output
    assert '[FILE] b' not in output


def test_recursive_large_tree_is_bounded(tmp_path):
    for i in range(100):
        directory = tmp_path / f'dir{i:03}'
        directory.mkdir()
        for j in range(200):
            (directory / f'file{j:03}').touch()
    start = time.perf_counter()
    output = file_ops.list_directory(str(tmp_path), recursive=True, max_entries=50)
    elapsed = time.perf_counter() - start
    assert elapsed < 1, elapsed
    assert len(output.splitlines()) == 51
    assert '(truncated at 50; more exist)' in output.splitlines()[0]

