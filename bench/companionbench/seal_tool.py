"""Print the sealed form of an answer, so the plaintext never has to be pasted anywhere.

    python -m bench.companionbench.seal_tool "北陸産業,23000"

Copy the hex into the episode's ANSWER_SEAL. Do not copy the answer.

    python -m bench.companionbench.seal_tool --init

writes a fresh random salt to the default location OUTSIDE the working tree, if none
exists. It refuses to overwrite an existing salt: every sealed answer already recorded was
computed under it, and replacing it invalidates them all at once with no error message --
every sealed episode would simply start returning 0.0 and look like a catastrophic
regression.
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys

from bench.companionbench.pools import (DEFAULT_SALT_FILE, SALT_ENV, SALT_FILE_ENV,
                                        SealError, seal, seal_salt)


def _init(path: str) -> int:
    if os.path.isfile(path):
        print("salt already exists; refusing to overwrite it -- every sealed answer on "
              "record was computed under it")
        return 2
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(secrets.token_hex(32) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    print("salt written outside the working tree")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bench.companionbench.seal_tool",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("answer", nargs="?", help="the plaintext answer to seal")
    ap.add_argument("--init", action="store_true",
                    help="create a salt at the default location if none exists")
    ap.add_argument("--check", action="store_true",
                    help="report whether a salt is available, without printing it")
    args = ap.parse_args(argv)

    if args.init:
        return _init(os.environ.get(SALT_FILE_ENV, "").strip() or DEFAULT_SALT_FILE)

    if args.check:
        try:
            seal_salt()
        except SealError as exc:
            print("NO SALT: %s" % exc)
            return 1
        print("salt available (%s / %s / default)" % (SALT_ENV, SALT_FILE_ENV))
        return 0

    if not args.answer:
        ap.error("give an answer to seal, or --init / --check")
    print(seal(args.answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
