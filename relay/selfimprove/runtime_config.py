"""Make the active genome actually change what the harness does.

The brief is blunt about this and it is the right thing to be blunt about: "The active
genome must actually affect runtime behavior. Do not merely record intent." A manifest that
is written into an archive row and then ignored produces experiments where the ON and OFF
arms are the same program. Every such experiment measures noise, and the archive fills with
confident rows describing changes that never happened.

So this module is the single place a manifest turns into values the running code reads, and
the test that matters is not that it parses -- it is that two different genomes produce two
different behaviours at a call site that exists.

Deliberately small. It resolves and caches; it does not decide anything. Components that
need richer behaviour than a version string and a few numbers should grow their own module
and read their version from here, rather than this file growing a switch statement per
component.
"""
from __future__ import annotations

import json
import os
import threading

from relay.selfimprove import manifest as M

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ACTIVE_PATH = os.path.join(REPO, ".fleet", "selfimprove", "active_manifest.json")

# Set to a manifest path to override the on-disk active one. Used by the evaluator to run a
# candidate arm without mutating the operator's active harness -- an A/B that switches the
# live configuration is not an A/B, it is two sequential deployments.
OVERRIDE_ENV = "MCP_HARNESS_MANIFEST"

_lock = threading.Lock()
_cache: dict | None = None
_cache_key: tuple | None = None


def _load(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    try:
        M.validate(data)
    except M.ManifestError:
        # An invalid manifest on disk must not silently become "some other harness". Fall
        # back to the base, which is a known configuration, and let the caller see it via
        # active_harness_id() rather than running something nobody chose.
        return None
    return data


def active_manifest(refresh: bool = False) -> dict:
    """The manifest in force right now.

    Cached on (path, mtime) so a long fleet run does not re-read it per call, and so an
    operator editing the file mid-run gets picked up rather than needing a restart.
    """
    global _cache, _cache_key
    path = os.environ.get(OVERRIDE_ENV, "").strip() or ACTIVE_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    key = (path, mtime)
    with _lock:
        if not refresh and _cache is not None and _cache_key == key:
            return _cache
        data = _load(path) if mtime is not None else None
        _cache = data if data is not None else M.base_manifest()
        _cache_key = key
        return _cache


def active_harness_id() -> str:
    return M.harness_id(active_manifest())


def parameter(name: str, default=None):
    """One tuned value. Unknown names return `default` rather than raising.

    Callers are ordinary runtime code; a typo in a parameter name should degrade to the
    default, not take down a fleet run mid-flight.
    """
    return active_manifest().get("parameters", {}).get(name, default)


def component(name: str, default: str = "") -> str:
    return active_manifest().get("components", {}).get(name, default)


def write_active(manifest: dict, path: str | None = None) -> str:
    """Install a manifest as the active harness. Returns its harness_id.

    Validates first: activating a manifest that names a forbidden component would be the
    single worst thing this module could do, and it is exactly what an unchecked write
    would allow.

    `path` defaults to None and is resolved HERE rather than in the signature. A default of
    `path=ACTIVE_PATH` binds the module attribute once at import, so anything that later
    redirects ACTIVE_PATH -- a test, or an operator pointing at a second profile -- is
    silently ignored and the manifest lands somewhere nobody is looking.
    """
    path = path or ACTIVE_PATH
    M.validate(manifest)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)
    global _cache, _cache_key
    with _lock:
        _cache, _cache_key = None, None
    return M.harness_id(manifest)


# ---------------------------------------------------------------------------------------
# Call sites: the proof that a genome does something
# ---------------------------------------------------------------------------------------

def memory_max_items() -> int:
    """How many past entries project_memory primes into a goal.

    A real knob with a real trade-off: too few and the agent rediscovers what it already
    learned, too many and the recall crowds out the task. Exactly the shape of thing the
    evolver should be tuning, and it is measurable because both failure directions show up
    in the benchmark.
    """
    try:
        return max(0, int(parameter("memory_max_items", 5)))
    except (TypeError, ValueError):
        return 5


def max_retries() -> int:
    try:
        return max(0, int(parameter("max_retries", 3)))
    except (TypeError, ValueError):
        return 3


def review_threshold() -> float:
    try:
        return float(parameter("review_threshold", 0.35))
    except (TypeError, ValueError):
        return 0.35


def max_context_budget() -> int:
    try:
        return max(0, int(parameter("max_context_budget", 18000)))
    except (TypeError, ValueError):
        return 18000
