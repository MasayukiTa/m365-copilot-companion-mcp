# -*- coding: utf-8 -*-
"""Make a JSON-able structure safe for a case-INSENSITIVE object reader.

WHY THIS EXISTS. Windows PowerShell 5.1's ``ConvertFrom-Json`` builds each JSON object
into a case-insensitive dictionary, so two keys in the SAME object that differ only in
case -- e.g. ``"turn"`` and ``"Turn"`` -- make it throw
``Cannot convert the JSON string ... duplicate keys 'turn' and 'Turn'`` and read nothing.
Python's ``json`` keeps both keys, so a status.json the fleet writes without trouble is
unreadable from the cockpit's own PowerShell helpers. Measured 2026-09-17: the panel's
``ConvertFrom-Json`` tail failed a few thousand characters in while python read the same
file fine.

The snapshot is assembled from many additive blocks (effort, fan-out, tree budget, ...),
any of which MAY one day emit a key that only differs in case from a sibling. Rather than
audit every producer forever, the writer folds the structure once on the way out: within
each object, if two keys collide case-insensitively, the LAST one wins (same rule the
reader would apply) and the others are dropped. Values are untouched; only a name clash is
resolved. Order is preserved. Lists and nested objects are walked.

Stdlib only and never raises on ordinary JSON-able input.
"""
from __future__ import annotations


def fold_case_collisions(obj):
    """Return a copy of `obj` in which no object has two keys differing only in case.

    LAST writer wins, matching how a case-insensitive reader would collapse them, so the
    value the cockpit sees is the value that was written last for that name. Non-dict,
    non-list values are returned unchanged. Pure; does not mutate the input.
    """
    if isinstance(obj, dict):
        # Walk in insertion order; a later key that case-collides with an earlier one
        # replaces it IN PLACE (keeps position stable and predictable), last value wins.
        folded = {}
        canon = {}   # lower-case name -> the actual key currently kept
        for k, v in obj.items():
            fv = fold_case_collisions(v)
            if isinstance(k, str):
                low = k.lower()
                keep = canon.get(low)
                if keep is not None:
                    # Same name under case-folding: overwrite the kept key's value and
                    # adopt this spelling, without adding a second entry.
                    if keep != k:
                        del folded[keep]
                        canon[low] = k
                    folded[k] = fv
                    if keep == k:
                        canon[low] = k
                    continue
                canon[low] = k
            folded[k] = fv
        return folded
    if isinstance(obj, list):
        return [fold_case_collisions(v) for v in obj]
    return obj


def has_case_collision(obj) -> bool:
    """True if any object anywhere in `obj` has two keys differing only in case.

    Used by tests and by a one-line writer assertion; never raises on JSON-able input.
    """
    if isinstance(obj, dict):
        seen = set()
        for k in obj:
            if isinstance(k, str):
                low = k.lower()
                if low in seen:
                    return True
                seen.add(low)
        return any(has_case_collision(v) for v in obj.values())
    if isinstance(obj, list):
        return any(has_case_collision(v) for v in obj)
    return False
