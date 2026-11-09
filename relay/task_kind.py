# -*- coding: utf-8 -*-
"""A derived label for WHAT KIND OF WORK a goal asks for (roadmap slice D0).

WHY THIS EXISTS. Later slices want to judge the fleet per kind of work (a role that is good at
bulk record checks may be poor at code changes). Nothing recorded says what kind a run was:
  * relay/task_router.py DESTINATION types (shell, python, coding, research, fleet_goal, ...)
    classify a queued JOB by where it runs. Every job file the fleet ever finished is the single
    type `fleet_goal`, so the type tells nothing apart.
  * relay/splittability.py judges a goal SPLIT / NO_SPLIT / UNCERTAIN. That is a property of the
    goal's shape, it is not stored per run, and it is not a kind of work.
  * relay/fanout.py's family view labels solo / parent / child / aggregator / stalled_parent.
    That is the position in a fan-out family, which the capability profile already records as
    the ROLE.
So the label is derived here from the goal text, by fixed rules, and only by the report
scripts: no fleet module imports this file and no record is ever written with it.

CONTRACT
  * Deterministic: the same text gives the same label (no clock, no randomness, no state).
  * The first rule that matches wins, in the order of RULES. A goal that matches none is
    "unknown". A label is never guessed from weak hints: a rule needs one of its own markers.
  * Non-text input (None, numbers) is "unknown".

DATA-SOURCE KEYWORDS ARE NOT HARD-CODED TO ONE DEPLOYMENT. The DATA_QUERY rule keeps a small,
generic built-in vocabulary (the words any data query uses: database, sql, query, odbc, table,
...). A deployment that has its own source names (a specific database, a driver, a kind of
data) puts those in a gitignored local file, .config/domain_terms.json, which this module reads
and merges on top of the built-in list. The default -- no file, or an empty one -- is the
built-in list alone, so the public code still classifies generic data queries and carries no
deployment's vocabulary. See docs/CONFIG.md for the file format.
"""
from __future__ import annotations

import json
import os

UNKNOWN = "unknown"
CODE_CHANGE = "code_change"
BULK_CHECK = "bulk_check"
DATA_QUERY = "data_query"
M365_LOOKUP = "m365_lookup"
TOOL_PROBE = "tool_probe"
LOCAL_FILES = "local_files"
WRITING = "writing"     # prose or research write-ups produced from the model's own knowledge

#: The generic data-source vocabulary any data query uses, regardless of deployment. These are
#: technology words, not the name of anyone's system or data -- so they are safe in public code
#: and still let the classifier recognise an ordinary "query the database" goal.
_BUILTIN_DATA_SOURCE_TERMS = (
    "database", "sql", "query", "odbc", "table", "dataset", "spreadsheet",
    "csv", "\u30c7\u30fc\u30bf\u30d9\u30fc\u30b9", "\u30af\u30a8\u30ea", "\u96c6\u8a08", "\u76f8\u95a2",
)

#: The local config file a deployment uses to add its own source names, relative to the repo
#: root. The .config/ directory is gitignored, so these words never reach the public repository.
_DOMAIN_TERMS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".config", "domain_terms.json")


def _load_local_data_source_terms():
    """Deployment-specific data-source keywords read from .config/domain_terms.json.

    Returns a tuple of lowercase marker strings, or () when the file is absent, unreadable, or
    carries no usable list (the default). The file is a JSON object whose "data_source_terms"
    key is an array of strings; any other shape yields the empty default rather than an error,
    so a malformed local file never breaks classification.
    """
    try:
        with open(_DOMAIN_TERMS_FILE, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return ()
    terms = data.get("data_source_terms") if isinstance(data, dict) else None
    if not isinstance(terms, list):
        return ()
    return tuple(str(t).casefold() for t in terms if isinstance(t, str) and t.strip())


def _data_query_markers():
    """The built-in generic data-source words plus any local deployment terms, casefolded."""
    builtin = tuple(t.casefold() for t in _BUILTIN_DATA_SOURCE_TERMS)
    return builtin + _load_local_data_source_terms()


#: The generic word public code uses for the thing a comparison must hold fixed across groups.
#: A deployment whose data names that thing specifically (e.g. a particular kind of product
#: catalogue column) puts its own word in .config/domain_terms.json under "product_label"; the
#: public default is this generic term, so no deployment's vocabulary ships in the repo.
_DEFAULT_PRODUCT_LABEL = "\u88fd\u54c1"  # "製品" (product)


def product_label():
    """The word for the product/stratum a design must hold fixed, from .config/domain_terms.json.

    Reads the optional local config's "product_label" string and returns it when it is a
    non-empty string; otherwise returns the generic built-in default ("製品"). A missing,
    unreadable, or malformed file yields the default rather than an error, so public code never
    carries a deployment's own column name and never breaks on a bad local file.
    """
    try:
        with open(_DOMAIN_TERMS_FILE, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return _DEFAULT_PRODUCT_LABEL
    term = data.get("product_label") if isinstance(data, dict) else None
    if isinstance(term, str) and term.strip():
        return term.strip()
    return _DEFAULT_PRODUCT_LABEL


#: (label, markers) in priority order. Markers are matched against the casefolded goal text.
#: Order matters: a code task that mentions a database, or a database task that names a repo
#: path, must land on its own kind, so the narrower kinds come first.
#: DATA_QUERY's markers are computed at import time from the built-in generic vocabulary and the
#: optional local .config/domain_terms.json (empty by default), never hard-coded to a deployment.
RULES = (
    (CODE_CHANGE, ("implementation task", "small fix", "doc rewrite", "implement the task")),
    (BULK_CHECK, ("\u7a81\u5408", "\u30d5\u30ea\u30ac\u30ca", "\u53f0\u5e33")),
    (DATA_QUERY, _data_query_markers()),
    (M365_LOOKUP, ("\u30e1\u30fc\u30eb", "\u4e88\u5b9a\u3092", "teams", "onedrive", "sharepoint", "\u4e0a\u53f8", "outlook",
                   "\u53d7\u4fe1")),
    (TOOL_PROBE, ("__noop_probe__", "harmless", "run_python")),
    (WRITING, ("handbook", "booklet", "manual", "briefing", "deliverable", "primer", "chapter",
               "glossar", "field guide", "study pack", "study sheet", "explainer", "recipe book",
               "two workers", "further worker", "in two sentences", "two parts", "two levels",
               "\u518a\u5b50", "\u6210\u679c\u7269", "\u30ec\u30dd\u30fc\u30c8", "\u30b5\u30d6\u30bf\u30b9\u30af", "\u4e92\u3044\u306b\u72ec\u7acb\u3057\u3066", "2\u5206\u91ce", "3\u9805\u76ee",
               "\u4e8c\u3064\u306e\u90e8", "\u4e8c\u6bb5\u968e")),
    (LOCAL_FILES, ("c:\\", "relay/", "relay\\", "tools/", "tools\\", ".py", "git repository",
                   "read-only", "\u8aad\u3080\u3060\u3051", "\u3092\u8aad\u3093\u3067", "\u30ea\u30dd\u30b8\u30c8\u30ea")),
)

KINDS = tuple(label for label, _ in RULES) + (UNKNOWN,)


def task_kind(goal):
    """The kind of work `goal` asks for: one of KINDS. "unknown" when no rule's marker is in it."""
    if not isinstance(goal, str) or not goal.strip():
        return UNKNOWN
    text = goal.casefold()
    for label, markers in RULES:
        for marker in markers:
            if marker in text:
                return label
    return UNKNOWN
