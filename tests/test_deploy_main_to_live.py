import importlib.util
from pathlib import Path
import subprocess

import pytest

spec = importlib.util.spec_from_file_location('deploy', Path(__file__).parents[1] / 'tools/deploy_main_to_live.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


def run(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, stdout=subprocess.PIPE)


@pytest.fixture
def history(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    run('git', 'init', cwd=repo)
    run('git', 'config', 'user.email', 'test@example.com', cwd=repo)
    run('git', 'config', 'user.name', 'Test', cwd=repo)
    (repo / 'file.txt').write_bytes(b'one\ntwo\nthree\n')
    run('git', 'add', '.', cwd=repo)
    run('git', 'commit', '-m', 'old', cwd=repo)
    old = run('git', 'rev-parse', 'HEAD', cwd=repo).stdout.decode().strip()
    (repo / 'file.txt').write_bytes(b'ONE\ntwo\nthree\n')
    run('git', 'commit', '-am', 'new', cwd=repo)
    new = run('git', 'rev-parse', 'HEAD', cwd=repo).stdout.decode().strip()
    live = tmp_path / 'live'
    live.mkdir()
    monkeypatch.chdir(repo)
    return live, old, new


def test_unchanged_and_merge(history):
    live, old, new = history
    f = live / 'file.txt'
    f.write_bytes(b'one\ntwo\nthree\n')
    actions, report = deploy.plan(live, old, new)
    assert actions[0][2] == b'ONE\ntwo\nthree\n'
    assert report[0]['status'] == 'update'
    f.write_bytes(b'one\ntwo\nTHREE\n')
    actions, report = deploy.plan(live, old, new)
    assert actions[0][2] == b'ONE\ntwo\nTHREE\n'
    assert report[0]['status'] == 'merge'


def test_conflict_and_deletion(history):
    live, old, new = history
    f = live / 'file.txt'
    f.write_bytes(b'LOCAL\ntwo\nthree\n')
    assert deploy.plan(live, old, new)[1][0]['status'].startswith('conflict')
    f.unlink()
    assert deploy.plan(live, old, new)[1][0]['status'].startswith('conflict')
