# -*- coding: utf-8 -*-
"""The kinds a board entry may be, AS DATA, with a version so a reader can tell which set it is.

WHY A SEPARATE MODULE. agent_board.py used to hard-code the four kinds and their fields inline,
so adding a kind meant editing validation, the digest renderer and the tests at once. Here the
whole vocabulary is one table, `REGISTRY`, keyed by kind name. A new kind is one more row: its
required fields, which field (if any) is its subject for filtering, and a one-line template for
the digest. Nothing in agent_board.py names a kind literally any more; it reads this table. That
is the "room to grow without a code change" the owner asked for -- a code change to ONE data table,
not to the logic around it.

VERSION. `SCHEMA_VERSION` is the version of THIS vocabulary (the kinds and their fields), separate
from the per-row `v` that agent_board writes (the on-disk row format, agent_board.SCHEMA). Bumping
SCHEMA_VERSION records that the kind set changed; old rows of a removed kind simply stop being
well-formed and are skipped on read, exactly like any other unknown kind.

ORDERLY, NOT SILENT. Every kind lists EXACTLY its allowed fields. agent_board.validate refuses an
unknown key rather than dropping it, so a typo'd field is a visible refusal, not a quiet loss.

DATA, NOT INSTRUCTIONS. None of this executes anything. The templates are %-format strings filled
with already-cleaned field values and shown under a line that says the board is data, not orders.

STDLIB ONLY: imported very early by the MCP server, like agent_board.
"""
from __future__ import annotations

#: The version of the kind vocabulary itself (not the on-disk row format). Bump when the set of
#: kinds or their fields changes, so a reader can tell which vocabulary produced a digest.
SCHEMA_VERSION = 2

#: Per-field length ceilings (characters). A field name not here is not allowed to appear.
FIELD_LIMITS = {"subject": 60, "text": 200, "ref": 200}

#: THE VOCABULARY, AS DATA. One row per kind:
#:   fields   -- the exact, all-required field names (an unknown key is refused, not dropped).
#:   subject  -- which field is the kind's "subject" for board_digest(subject=...) filtering,
#:               or None when the kind has no subject to filter on.
#:   worker   -- whether a worker (not only the coordinator) may post this kind in p1.
#:   cap      -- a worker's concurrent-live ceiling for this kind (coordinator is uncapped).
#:   ttl_s    -- how long an entry of this kind stays visible, in seconds.
#:   template -- a %-format string over the kind's fields, rendered into one digest line.
#:
#: TO ADD A KIND: add one row here and bump SCHEMA_VERSION. No other file needs editing for the
#: store, the validator, the digest or the filters to understand it -- they all read this table.
REGISTRY = {
    "claim": {
        "fields": ("subject",),
        "subject": "subject",
        "worker": True,
        "cap": 3,
        "ttl_s": 30 * 60,
        "template": "%(subject)s",
    },
    "result": {
        "fields": ("subject", "ref"),
        "subject": "subject",
        # COORDINATOR-ONLY. `result` points at a finished child's output, which only the
        # coordinator posts (post_result). A worker that asks for it through board_post is
        # refused with "role"; this flag is what makes that a data decision, not a literal.
        "worker": False,
        "cap": 5,
        "ttl_s": 2 * 3600,
        "template": "%(subject)s \u7d50\u679c\u306f %(ref)s",
    },
    "fact": {
        "fields": ("subject", "text"),
        "subject": "subject",
        "worker": True,
        "cap": 5,
        "ttl_s": 2 * 3600,
        "template": "%(subject)s: %(text)s",
    },
    "question": {
        "fields": ("text",),
        "subject": None,
        "worker": True,
        "cap": 2,
        "ttl_s": 2 * 3600,
        "template": "%(text)s",
    },
}

#: Kinds in a stable order (definition order), for anything that needs to iterate them.
KINDS = tuple(REGISTRY)


def is_kind(kind) -> bool:
    """Whether `kind` is a known kind. A non-string or unknown name is False, never an error."""
    return isinstance(kind, str) and kind in REGISTRY


def fields_of(kind):
    """The exact required field names of `kind`, or () when the kind is unknown."""
    spec = REGISTRY.get(kind)
    return spec["fields"] if spec else ()


def subject_field(kind):
    """The name of the field used as the subject of `kind` for filtering, or None."""
    spec = REGISTRY.get(kind)
    return spec["subject"] if spec else None


def subject_value(row):
    """The subject string of a stored row for subject-filtering, or "" when the kind has none."""
    try:
        name = subject_field(row.get("kind"))
        return row.get(name, "") if name else ""
    except Exception:
        return ""


def worker_may_post(kind) -> bool:
    """Whether a worker (not just the coordinator) may post `kind` in p1."""
    spec = REGISTRY.get(kind)
    return bool(spec and spec.get("worker"))


def author_cap(kind):
    """A worker's concurrent-live ceiling for `kind`, or 0 when the kind is unknown."""
    spec = REGISTRY.get(kind)
    return int(spec["cap"]) if spec else 0


def ttl_of(kind, default_s):
    """The visibility window of `kind` in seconds, or `default_s` when the kind is unknown."""
    spec = REGISTRY.get(kind)
    return int(spec["ttl_s"]) if spec else int(default_s)


def render(row):
    """The body of a digest line for a stored row, from the kind's template. Never raises: an
    unknown kind or a field the template wants but the row lacks falls back to a plain join of
    the kind's field values, so a mis-set template degrades instead of crashing a digest."""
    try:
        spec = REGISTRY.get(row.get("kind"))
        if not spec:
            return ""
        return spec["template"] % row
    except Exception:
        try:
            return " ".join(str(row.get(f, "")) for f in fields_of(row.get("kind")))
        except Exception:
            return ""
