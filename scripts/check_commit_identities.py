import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import childproc


def allowed(email):
    email = email.lower()
    return email.endswith(("@users.noreply.github.com", "@local")) or email in {
        "noreply@github.com", "noreply@anthropic.com"
    }


def git(*args):
    result = childproc.run(["git", *args], creationflags=childproc.headless_creationflags())
    result.check_returncode()
    return result


def fallback_base(default_branch):
    # The PR's base is the comparison point even when it is not the default branch.
    if os.environ.get("GITHUB_EVENT_NAME") == "pull_request":
        with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as event_file:
            base = json.load(event_file)["pull_request"]["base"]["sha"]
        git("fetch", "--no-tags", "origin", base)
        return "FETCH_HEAD"
    ref = f"refs/remotes/origin/{default_branch}"
    git("fetch", "--no-tags", "origin", f"+refs/heads/{default_branch}:{ref}")
    return ref


def main():
    before, after, default_branch = sys.argv[1:]
    if set(after) == {"0"}:
        return 0
    try:
        try:
            if not before or set(before) == {"0"}:
                raise ValueError("missing base")
            git("merge-base", "--is-ancestor", before, after)
            result = git("log", "--format=%H%x00%ae%x00%ce", f"{before}..{after}")
        except (subprocess.CalledProcessError, ValueError):
            try:
                git("cat-file", "-e", f"{after}^{{commit}}")
            except subprocess.CalledProcessError:
                git("fetch", "--no-tags", "origin", after)
            base = fallback_base(default_branch)
            result = git("log", "--format=%H%x00%ae%x00%ce", after, "--not", base)
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as error:
        detail = error.stderr if isinstance(error, subprocess.CalledProcessError) else str(error)
        detail = " ".join((detail or type(error).__name__).splitlines())
        print(f"Unable to check commit identities: {detail}", file=sys.stderr)
        return 1
    failed = False
    for line in result.stdout.splitlines():
        commit, author, committer = line.split("\0")
        for role, email in (("author", author), ("committer", committer)):
            if not allowed(email):
                print(f"{commit}: {role} email is not allowed")
                failed = True
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
