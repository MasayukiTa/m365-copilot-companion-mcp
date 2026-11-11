import subprocess
import sys


def allowed(email):
    email = email.lower()
    return email.endswith(("@users.noreply.github.com", "@local")) or email in {
        "noreply@github.com", "noreply@anthropic.com"
    }


def main():
    before, after, default_branch = sys.argv[1:]
    if set(after) == {"0"}:
        return 0
    revisions = [f"{before}..{after}"]
    if set(before) == {"0"}:
        revisions = [after, "--not", f"refs/remotes/origin/{default_branch}"]
    result = subprocess.run(
        ["git", "log", "--format=%H%x00%ae%x00%ce", *revisions],
        check=True, capture_output=True, text=True,
    )
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
