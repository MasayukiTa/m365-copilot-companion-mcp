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

#: Written into the .prev file when no manifest was in force. Distinguishable from any
#: real manifest because it is deliberately not JSON.
NO_MANIFEST = "(no manifest was in force)"

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

    THE OVERRIDE IS HONOURED ON WRITE, NOT ONLY ON READ. It was read-only, so a caller that
    had redirected the active manifest to a temp file still WROTE to the production path.
    That is not hypothetical: a controller test with activate=True installed a manifest into
    the real `.fleet/selfimprove/active_manifest.json`, which is gitignored, so it never
    appeared in a diff -- and from then on every fleet run used a retry budget of 3 instead
    of 10 and primed 9 memory entries instead of 5. Read and write must resolve the same
    location, or "the active manifest" means two different files depending on the verb.
    """
    path = path or os.environ.get(OVERRIDE_ENV, "").strip() or ACTIVE_PATH
    M.validate(manifest)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    # THE UNDO POINT IS TAKEN HERE, BEFORE THE WRITE, OR THERE IS NO UNDO.
    #
    # `relay/selfimprove/apply.py` has apply/revert with authority-ledger records, and it
    # operates on `active_genome.json` -- a store whose own docstring says the part that
    # reads it is deferred. So the rollback machinery was attached to the file nothing
    # reads, while THIS file, the one every fleet run resolves its harness from, was
    # overwritten in place with no backup and no record. Turning activation on in that
    # state would have applied a genome with nothing to roll back to and nothing saying
    # who applied it.
    # A SENTINEL WHEN THERE WAS NOTHING, BECAUSE "NOTHING" IS ALSO A STATE TO RETURN TO.
    #
    # The first version wrote a backup only when a manifest already existed, which left the
    # FIRST activation -- the one that takes a system from base to evolved -- as the single
    # one that could not be undone. That was this repository's exact state: no active
    # manifest on disk, so the next apply would have been irreversible, and the demonstration
    # of the rollback is what surfaced it. Absence is now recorded explicitly, and revert
    # restores it by removing the file.
    prev = _read_raw(path)
    with open(path + ".prev", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(NO_MANIFEST if prev is None else prev)

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)
    _invalidate()
    _note(path, "genome_apply", "activated %s" % M.harness_id(manifest), prev)
    return M.harness_id(manifest)


def _read_raw(path):
    """The file's bytes, or None if it is not there.

    Not parsed: the undo point is what was ON DISK, including a file this version cannot
    validate. A revert that could only restore manifests the current code accepts would fail
    exactly when it is most needed.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return None


def _invalidate():
    global _cache, _cache_key
    with _lock:
        _cache, _cache_key = None, None


def _digest(text):
    import hashlib
    if text is None:
        return ""
    return hashlib.sha256(text.replace(chr(13) + chr(10), chr(10)).encode("utf-8")).hexdigest()[:16]


def _note(path, event, reason, prev_text):
    """Record the change in the authority ledger. Never raises, never silent.

    A failed record must not fail the write -- the operator asked for the harness to change
    and it did. But it must not pass unnoticed either, because an activation nobody can
    attribute is the thing the ledger exists to prevent.
    """
    try:
        from relay.selfimprove import authority_ledger as AL
        AL.append(event, reason=reason, actor_claimed="runtime_config.write_active",
                  authorization=AL.SELF_INITIATED,
                  changed={path: {"before": _digest(prev_text),
                                  "after": _digest(_read_raw(path))}})
    except Exception as exc:
        print("[runtime_config] could not record %s: %s: %s"
              % (event, type(exc).__name__, exc), flush=True)


def revert_active(path=None):
    """Swap the active manifest with the one it replaced. Undo -- and undo the undo.

    NOT ONE-WAY. The first version restored from `.prev` and left `.prev` untouched, so the
    genome you had just backed out of was held nowhere and could not be put back. An operator
    who reverted a good change by mistake had no way forward except to remember what it was.

    So `.prev` is not a backup, it is THE OTHER STATE, and this swaps them. Calling it twice
    returns you exactly where you started. Two slots and no more: the archive and the
    authority ledger hold the long history, and a deeper stack here would invite unwinding
    several activations at once, which is not the question anyone has in front of a bad
    harness.

    Because it is a swap, the caller decides what to call it. `pending_swap()` says which
    harness the next call would install, so a button can read "undo" or "redo" honestly
    rather than being labelled once and lying half the time.

    Returns False when there is no other state -- before anything has ever been applied. That
    must never be reported as a successful rollback.
    """
    path = path or os.environ.get(OVERRIDE_ENV, "").strip() or ACTIVE_PATH
    prev_path = path + ".prev"
    other = _read_raw(prev_path)
    if other is None:
        return False
    current = _read_raw(path)

    # The state we are leaving becomes the one the next call restores. Written FIRST: if the
    # process dies between the two writes, the worse outcome is having the old manifest in
    # both slots, not having the current one in neither.
    with open(prev_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(NO_MANIFEST if current is None else current)

    if other == NO_MANIFEST:
        # Back to no manifest at all, which resolves to the base harness. Removing the file is
        # the undo; writing the base manifest into it would leave a system that LOOKS
        # activated at the base rather than one that was never activated -- a different state,
        # and not the one an operator undoing a bad night asked for.
        try:
            os.remove(path)
        except OSError:
            pass
    else:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(other)
    _invalidate()
    _note(path, "genome_revert", "swapped with %s" % os.path.basename(prev_path), current)
    return True


def pending_swap(path=None):
    """What `revert_active()` would install next, as a harness id -- or None if nothing.

    Exists so a control can be labelled with what it will actually do. A button that says
    "roll back" after you have already rolled back is telling you the opposite of the truth.
    "" means the swap would return the system to having no manifest at all.
    """
    path = path or os.environ.get(OVERRIDE_ENV, "").strip() or ACTIVE_PATH
    other = _read_raw(path + ".prev")
    if other is None:
        return None
    if other == NO_MANIFEST:
        return ""
    try:
        return M.harness_id(json.loads(other))
    except Exception:
        # A manifest this version cannot read is still a state worth returning to; the caller
        # just cannot be told its id.
        return "(unreadable)"

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
    # The fallback must be the PRODUCTION value, not a smaller round number. A fallback that
    # disagrees with the base manifest silently changes behaviour exactly when the manifest
    # cannot be read -- which is the moment nobody is looking.
    try:
        return max(0, int(parameter("max_retries", 10)))
    except (TypeError, ValueError):
        return 10


def max_research() -> int:
    """How many on-demand research side-pages a worker may open.

    The other half of what `max` and `ultra` mean, and the half that costs RAM rather than
    turns: every side-page is a browser tab under the admission floor. Both directions are
    visible in the benchmark -- too few and the agent guesses at APIs it could have read, too
    many and the run stalls waiting for room it did not need.
    """
    try:
        return max(0, int(parameter("max_research", 3)))
    except (TypeError, ValueError):
        return 3


def review_lens_count() -> int:
    """How many of PANEL_LENSES review a candidate answer. 0 means no panel.

    THE KNOB THAT MAKES `ultra` EXPRESSIBLE. Everything else separating `ultra` from `auto`
    was already a manifest parameter or a call argument; the panel was neither, so a harness
    asked for one resolved to the other and the two were recorded under the same harness_id.

    The count takes lenses from the front of PANEL_LENSES, so the ladder is a real ladder:
    each step is a superset of the one below it, and a comparison between two counts is a
    comparison of one added reviewer rather than of two different panels.
    """
    try:
        return max(0, min(3, int(parameter("review_lens_count", 0))))
    except (TypeError, ValueError):
        return 0


def max_refute_passes() -> int:
    """How many refuter passes a candidate answer gets before it is accepted.

    This was `review_threshold`, a 0..1 float that nothing read. Wiring it meant inventing a
    mapping -- count = threshold * 10 -- and a knob whose name says "threshold" while its
    value is secretly a count is the same species of dishonesty this review exists to find.
    Renamed to what production actually consumes.

    A real trade-off with both directions visible in the benchmark: too few passes and wrong
    answers are upheld, too many and every turn pays for review it did not need.
    """
    try:
        return max(0, int(parameter("max_refute_passes", 2)))
    except (TypeError, ValueError):
        return 2


def reset_to_base(path=None) -> bool:
    """Return the harness to base, whatever the two swap slots happen to hold.

    MAIN IS ALWAYS REACHABLE, AND THE SWAP ALONE DOES NOT GUARANTEE THAT.

    `revert_active` holds two states. Apply v2, then apply v3, and those two slots are v3 and
    v2 -- the un-activated base has fallen out of both, and an operator who wanted "put it
    back the way it shipped" had no move left. That is the one branch that must never be
    prunable, and it does not need a slot: the base manifest is CONSTRUCTED, not remembered,
    so returning to it is always available no matter how many activations happened.

    Deliberately not a third slot and not a stack. Two slots for undo/redo, plus a way home
    that history cannot lose. Removing the file rather than writing the base into it, because
    "never activated" and "activated at the base" are different states and only the first is
    where the system shipped.

    Returns False if there was nothing to reset -- already at base.
    """
    path = path or os.environ.get(OVERRIDE_ENV, "").strip() or ACTIVE_PATH
    current = _read_raw(path)
    if current is None:
        return False
    # The state being left still goes into the other slot, so "back to base" is itself
    # undoable. Going home should not be the one move you cannot take back.
    with open(path + ".prev", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(current)
    try:
        os.remove(path)
    except OSError:
        pass
    _invalidate()
    _note(path, "genome_revert", "reset to base: the harness as it shipped", current)
    return True
