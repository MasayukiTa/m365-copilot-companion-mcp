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
