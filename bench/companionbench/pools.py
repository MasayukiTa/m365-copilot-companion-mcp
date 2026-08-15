"""Three pools, and the reason the third one is sealed.

    evolution    the optimiser may read, re-run and mine this freely
    regression   re-run every candidate; catches known breakage
    sealed       read at milestones only; the optimiser must not be able to inspect it

The split is not bureaucracy. Repeatedly measuring against a set converts it into
optimisation feedback, and a number reported from a set that has been optimised against is
not an estimate of generalisation -- it is a training score with a misleading name. The
regression pool is explicitly NOT held out for that reason: it is run constantly, so it
tells you nothing about unseen work, only that what used to pass still does.

WHY SEALING NEEDS A MECHANISM

"Do not look at the sealed set" is not a control when the thing being asked runs with
filesystem tools and is being optimised to score well. It has to be structurally unreadable
rather than merely off-limits.

So a sealed episode's expected answer is stored ONLY as a salted SHA-256. The grader can
still check an answer (hash what the agent produced, compare), but reading the file gives
an optimiser nothing to fit: it cannot invert the hash, and without the salt it cannot even
build a rainbow table against a small answer space like "3" or "OK".

The salt lives outside the repository -- an environment variable, or a file the working
tree does not contain. If it is absent, sealed episodes REFUSE TO GRADE rather than falling
back to plaintext comparison. A holdout that silently degrades into a readable one is worse
than no holdout, because the number it produces still looks trustworthy.
"""
from __future__ import annotations

import hashlib
import hmac
import os

EVOLUTION = "evolution"
REGRESSION = "regression"
SEALED = "sealed"
POOLS = (EVOLUTION, REGRESSION, SEALED)

# Where the sealed salt comes from. Deliberately NOT a path inside the repo: anything the
# working tree contains is readable by the same tools the optimiser drives.
SALT_ENV = "COMPANIONBENCH_SEAL_SALT"
SALT_FILE_ENV = "COMPANIONBENCH_SEAL_SALT_FILE"

#: Last-resort salt location: the operator's home directory, resolved at runtime so no
#: absolute path is written into the source. Outside every checkout, which is the property
#: that matters. A fresh clone has no salt and its sealed episodes refuse to grade -- the
#: correct behaviour, since a holdout that travels with the repo is not a holdout.
DEFAULT_SALT_FILE = os.path.join(os.path.expanduser("~"), ".companionbench_seal_salt")

#: WHAT THE SEAL DOES AND DOES NOT DO, stated plainly because the overclaim is tempting.
#: It keeps the answer key out of the working tree, which is the realistic leak: an
#: optimiser given this repository can read every file in it, and a plaintext expected
#: answer sitting in bench/ is an invitation to fit to it. It does NOT defend against a
#: process that can read the operator's home directory -- same user, same machine, and the
#: grader must be able to read the salt to grade at all. Treat sealed results as a
#: generalisation check under an honest optimiser, not as a security boundary.
SEAL_THREAT_MODEL = "keeps the key out of the tree; not a boundary against a local reader"


class SealError(RuntimeError):
    """Raised when a sealed answer cannot be checked. Never downgraded to a plaintext path."""


def seal_salt() -> str:
    """The salt, or raise. Absent salt must stop the grade, not weaken it."""
    salt = os.environ.get(SALT_ENV, "").strip()
    if salt:
        return salt
    path = os.environ.get(SALT_FILE_ENV, "").strip() or DEFAULT_SALT_FILE
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                salt = (fh.read() or "").strip()
        except FileNotFoundError:
            salt = ""
        except OSError as exc:
            raise SealError("sealed salt file unreadable: %s" % exc) from exc
        if salt:
            return salt
    raise SealError(
        "no sealed salt (%s or %s). Sealed episodes refuse to grade rather than compare "
        "plaintext answers -- a holdout that silently becomes readable still reports a "
        "number that looks trustworthy." % (SALT_ENV, SALT_FILE_ENV))


def seal(answer: str, salt: str | None = None) -> str:
    """The stored form of a sealed answer: HMAC-SHA256 over the salt.

    HMAC rather than a bare hash of salt+answer so the construction has no length-extension
    surprises if this is ever extended to structured answers.
    """
    key = (salt if salt is not None else seal_salt()).encode("utf-8")
    return hmac.new(key, ("%s" % answer).encode("utf-8"), hashlib.sha256).hexdigest()


def sealed_matches(produced: str, sealed_hex: str, salt: str | None = None) -> bool:
    """Constant-time compare of a produced answer against its sealed form."""
    return hmac.compare_digest(seal(produced, salt), (sealed_hex or "").lower())


class PoolRegistry:
    """Which episodes belong to which pool, and what may read what.

    Membership is declared here rather than on the episode so that moving an episode
    between pools is one visible edit in one file -- and so an episode cannot assign
    itself to `evolution` to get itself looked at more often.
    """

    def __init__(self):
        self._pools = {p: [] for p in POOLS}

    def register(self, episode, pool: str) -> None:
        if pool not in POOLS:
            raise ValueError("unknown pool: %r" % pool)
        if not getattr(episode, "episode_id", ""):
            raise ValueError("episode has no episode_id; it cannot be joined to its history")
        for existing in self._pools.values():
            if any(e.episode_id == episode.episode_id for e in existing):
                raise ValueError("duplicate episode_id: %s" % episode.episode_id)
        self._pools[pool].append(episode)

    def get(self, pool: str) -> list:
        if pool not in POOLS:
            raise ValueError("unknown pool: %r" % pool)
        return list(self._pools[pool])

    def all_ids(self) -> dict:
        return {p: [e.episode_id for e in eps] for p, eps in self._pools.items()}

    def optimiser_visible(self) -> list:
        """Everything an optimiser is allowed to inspect. Sealed is absent by construction.

        Callers that want "all episodes" should say so explicitly; the default has to be
        the safe one, because the unsafe version of this call is indistinguishable at the
        call site and is the whole failure mode.
        """
        return self.get(EVOLUTION) + self.get(REGRESSION)


REGISTRY = PoolRegistry()


def register(pool: str):
    """Decorator: attach an episode class to a pool at import time."""
    def deco(cls):
        REGISTRY.register(cls(), pool)
        return cls
    return deco
