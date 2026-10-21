"""Override store for organisation definition text, and the stored text of promoted versions (slice D6-b).

WHAT DECISION THIS SERVES. Whether a promoted definition can really be rolled back: the ledger in
relay/org/promotion.py keeps hashes only, so there was no earlier TEXT to return to. This module
keeps that text outside the repository and the ledger (internal prompts) and gives a definition
module one way to take an override.

TWO KINDS OF FILE, both under STORE_DIR (`<repo>/.fleet/org_defs`, or the directory named by the
environment variable ORG_DEFS_DIR, which a test or an operator with a different state directory
sets):
  overrides.json     {"relay.planner.PLAN_PROMPT": "<text>"}  part label -> text, the text a
                     process uses instead of the one in the source.
  <genotype_id>.json {"genotype_id", "parts": {label: text}, "previous": {"genotype_id",
                     "parts"}}  the text of a promoted version and of the version promoted
                     before it.

READ ONCE, AT IMPORT. A definition module calls `override(label, default)` once, when it is
imported. Nothing re-reads the store afterwards: no hot reload, no polling, no watcher. So a
write to the store cannot change a process that is already running; it takes effect for the next
process that starts. (Unlike relay/selfimprove/runtime_config, which re-reads on mtime change.)

WRITES ARE ATOMIC. Each file is written to a temporary file in the same directory and moved into
place with os.replace, so a process that starts during a write reads the old file or the new one,
never half of one.

ONLY THE PARTS LISTED IN OVERRIDABLE CAN BE OVERRIDDEN OR STORED. Today that is the planner
prompt. Any other label is refused with an error that names it; widening the list is a code
change with its own review, because each part reaches every worker in a different way.

FAILURE-SAFE ON READ. `override` never raises: a missing, unreadable or malformed store, or a
label that is not overridable, leaves the source text in force.
"""
from __future__ import annotations

import io
import json
import os
import tempfile

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
#: Runtime state, untracked. Tests redirect it (conftest LIVE_RECORD_REDIRECTS).
STORE_DIR = os.path.join(_REPO, ".fleet", "org_defs")
ENV_DIR = "ORG_DEFS_DIR"
OVERRIDES = "overrides.json"

#: The parts whose text may be overridden. Only the planner prompt in this slice (it is used only
#: when planner/v2 is selected). Every other part is refused, not silently ignored.
OVERRIDABLE = ("relay.planner.PLAN_PROMPT",)


class StoreError(Exception):
    """A refusal with a message an operator can act on."""


def store_dir(directory=None) -> str:
    if directory:
        return os.path.abspath(str(directory))
    return os.path.abspath(os.environ.get(ENV_DIR) or STORE_DIR)


def check_label(label) -> str:
    if label not in OVERRIDABLE:
        raise StoreError("part %r cannot be overridden or rolled back; only %s can"
                         % (label, ", ".join(OVERRIDABLE)))
    return label


def _read_json(path):
    with io.open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def read_overrides(directory=None) -> dict:
    """label -> text from overrides.json, only for overridable labels with string text.
    Never raises; a missing or damaged file gives {}."""
    try:
        obj = _read_json(os.path.join(store_dir(directory), OVERRIDES))
        if not isinstance(obj, dict):
            return {}
        return {k: v for k, v in obj.items()
                if k in OVERRIDABLE and isinstance(v, str) and v}
    except Exception:
        return {}


def override(label, default, directory=None):
    """The override text for `label`, or `default`. Meant to be called once, at import."""
    try:
        return read_overrides(directory).get(label, default)
    except Exception:
        return default


def _atomic_write(path, obj) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    try:
        with io.open(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(obj, fh, ensure_ascii=False, sort_keys=True, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_overrides(mapping, directory=None) -> None:
    """Replace overrides.json with `mapping` (label -> text). Refuses a label that is not
    overridable before writing anything."""
    clean = {}
    for label, text in dict(mapping).items():
        check_label(label)
        if not isinstance(text, str) or not text:
            raise StoreError("the text for %r is empty" % (label,))
        clean[label] = text
    _atomic_write(os.path.join(store_dir(directory), OVERRIDES), clean)


def _version_path(genotype_id, directory=None) -> str:
    gid = str(genotype_id or "")
    if not gid or not gid.isalnum():
        raise StoreError("a genotype id is a non-empty hex name, got %r" % (gid,))
    return os.path.join(store_dir(directory), gid + ".json")


def save_version(genotype_id, parts, previous_id="", directory=None) -> None:
    """Store the text of a promoted version and, beside it, the text of the version promoted
    before it (read from that version's own file; empty when none was stored)."""
    for label in parts:
        check_label(label)
    prev = {"genotype_id": "", "parts": {}}
    if previous_id:
        try:
            old = load_version(previous_id, directory)
            prev = {"genotype_id": str(previous_id), "parts": dict(old["parts"])}
        except StoreError:
            prev = {"genotype_id": str(previous_id), "parts": {}}
    _atomic_write(_version_path(genotype_id, directory),
                  {"genotype_id": str(genotype_id), "parts": dict(parts), "previous": prev})


def load_version(genotype_id, directory=None) -> dict:
    """{"genotype_id", "parts", "previous"} for a stored version; StoreError with a clear message
    when the file is missing, damaged, or carries no usable text."""
    path = _version_path(genotype_id, directory)
    try:
        obj = _read_json(path)
    except FileNotFoundError:
        raise StoreError("no stored text for version %s (expected %s); it cannot be restored"
                         % (genotype_id, path))
    except Exception:
        raise StoreError("the stored text for version %s is unreadable" % (genotype_id,))
    parts = obj.get("parts") if isinstance(obj, dict) else None
    if (not isinstance(parts, dict) or not parts
            or any(not isinstance(v, str) or not v for v in parts.values())):
        raise StoreError("the stored text for version %s holds no usable parts" % (genotype_id,))
    for label in parts:
        check_label(label)
    return {"genotype_id": str(genotype_id), "parts": parts,
            "previous": obj.get("previous") if isinstance(obj.get("previous"), dict) else {}}
