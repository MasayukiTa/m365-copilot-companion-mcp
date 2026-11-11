import os
from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).with_name("check_commit_identities.py")


def test_commit_identities(tmp_path):
    def git(*args, email="test@local"):
        env = dict(os.environ, GIT_AUTHOR_NAME="Test", GIT_COMMITTER_NAME="Test",
                   GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_EMAIL=email)
        return subprocess.run(["git", *args], cwd=tmp_path, env=env, check=True,
                              capture_output=True, text=True).stdout.strip()

    git("init")
    git("commit", "--allow-empty", "-m", "baseline")
    base = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-m", "allowed")
    allowed = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-m", "disallowed", email="someone@example.org")
    head = git("rev-parse", "HEAD")
    def check(end):
        return subprocess.run([sys.executable, str(SCRIPT), base, end, "main"],
                              cwd=tmp_path, capture_output=True, text=True)

    assert check(allowed).returncode == 0
    result = check(head)
    assert result.returncode == 1
    assert "author" in result.stdout and "committer" in result.stdout


def test_fallback_ranges(tmp_path):
    import json

    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(work)], check=True, capture_output=True)

    def git(*args, email="test@local"):
        env = dict(os.environ, GIT_AUTHOR_NAME="Test", GIT_COMMITTER_NAME="Test",
                   GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_EMAIL=email)
        return subprocess.run(["git", *args], cwd=work, env=env, check=True,
                              capture_output=True, text=True).stdout.strip()

    git("checkout", "-b", "main")
    git("commit", "--allow-empty", "-m", "baseline", email="old@example.org")
    base = git("rev-parse", "HEAD")
    git("remote", "add", "origin", str(remote))
    git("push", "origin", "main")
    git("commit", "--allow-empty", "-m", "bad", email="bad@example.org")
    head = git("rev-parse", "HEAD")

    clean_env = {k: v for k, v in os.environ.items()
                 if k not in ("GITHUB_EVENT_NAME", "GITHUB_EVENT_PATH")}

    def check(before, env=None):
        return subprocess.run([sys.executable, str(SCRIPT), before, head, "main"],
                              cwd=work, env=clean_env if env is None else env,
                              capture_output=True, text=True)

    for before in ("f" * 40, "0" * 40):
        result = check(before)
        assert result.returncode == 1
        assert head in result.stdout
        assert base not in result.stdout
        assert "author" in result.stdout and "committer" in result.stdout
        assert "Traceback" not in result.stderr

    # An existing but unrelated base must take the same fallback.
    git("checkout", "--orphan", "unrelated")
    git("commit", "--allow-empty", "-m", "unrelated")
    result = check(git("rev-parse", "HEAD"))
    assert head in result.stdout and base not in result.stdout

    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"base": {"sha": base}}}))
    env = dict(clean_env, GITHUB_EVENT_NAME="pull_request", GITHUB_EVENT_PATH=str(event))
    result = check("0" * 40, env)
    assert result.returncode == 1 and head in result.stdout and base not in result.stdout


def test_git_failure_is_one_line(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), "0" * 40, "f" * 40, "main"],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 1
    assert len(result.stderr.splitlines()) == 1
    assert "Unable to check commit identities" in result.stderr
    assert "Traceback" not in result.stderr
