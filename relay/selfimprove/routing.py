"""Phase 10: which harness branch a task runs under, decided from safe features only.

`harness_tree` answers "given a task class, what is the manifest". This module answers the
question before it -- WHERE THE CLASS COMES FROM -- and that is the part with a security
property, because routing is a choice of configuration and anything that can influence the
choice can influence the configuration.

THE ATTACK THIS IS SHAPED AROUND

A task class inferred from the task's own content is a class an attacker can set. Write
"this is a routine coding task" into a document the agent is asked to summarise and the job
routes to the coding branch -- a harness chosen by the document rather than for it. Nothing
looks wrong afterwards: the run completes, the archive records a harness id, and the fact
that the input picked it is nowhere.

WHAT AN EARLIER DRAFT GOT WRONG, RECORDED BECAUSE IT IS THE INTERESTING PART

That draft routed on `read_untrusted` -- "the harness read external content" -- and argued
this was safe because it is the harness's own observation rather than the content's claim
about itself. The distinction is real. The feature was not: ROUTING HAPPENS BEFORE THE TASK
RUNS, so at the moment the harness is chosen, whether it will read anything is not yet a
fact. The only readings available were "a prediction dressed as an observation" or "the
content was already processed under some other configuration", and neither is what the
docstring claimed. What survives is the operator's own forecast, `will_read_untrusted`,
which is honestly a declaration and is ranked as one.

The same draft asserted that forcing a task onto the strict branch "cannot be an
escalation". Nothing made that true. Freezing the component named `security` stops a branch
from redefining the security module; it does not stop `max_refute_passes: 0` on a branch
named `security_sensitive`. The relation is now checked -- see `at_least_as_strict` -- and a
strict route to a branch that is not stricter is refused rather than trusted.

THE RULE

A feature must be something the SYSTEM or the OPERATOR established. Provenance is mandatory:
a bare value is refused, because "untagged" and "trusted" must not be the same thing.
AGENT_INFERENCE is refused too -- the agent's reasoning is influenced by the content it just
read, so letting it name the class reopens the door one step further back.

FAILING CLOSED HERE MEANS THE ROOT

An unroutable task does not error and does not get an empty configuration. It gets the root
manifest, which is the one a human reviewed. Note what that does and does not buy: the root
is accountable, not automatically safest. A branch built to be stricter than the root is
stricter than the root, and falling back past it is a real loss. That is why the fallback
reports itself instead of being silent -- see `route`, which records both the class that was
chosen and whether a branch existed for it.
"""
from __future__ import annotations

import copy

from relay import provenance as PROV
from relay.selfimprove import harness_tree as HT

#: The task classes from the brief. Re-exported from `harness_tree`, which validates branch
#: names against the same tuple, so a class cannot be routable here and unnameable there.
TASK_CLASSES = HT.TASK_CLASSES

#: Who may establish a routing feature. Deliberately narrower than "not untrusted":
#: AGENT_INFERENCE is excluded because an agent that has just read a hostile document is not
#: an independent source about what kind of task this is.
ROUTING_AUTHORITIES = frozenset({
    PROV.SYSTEM_POLICY,
    PROV.OPERATOR_INSTRUCTION,
    PROV.MACHINE_VERIFIER,
})

#: Features the router may read, and who may establish each.
#:
#:   declared_class        the operator says what kind of job this is
#:   surface               local vs M365 cloud -- configuration, not content
#:   fixture_kinds         extensions of files the harness created in setup(). MACHINE_VERIFIER
#:                         because the tag IS the attestation that setup created them: this
#:                         module cannot tell a setup fixture from a file the agent wrote
#:                         later, and a scan of the directory would not either. Mistagging is
#:                         a failure of the boundary that supplied it, and is worth naming
#:                         because it is the one feature whose safety is asserted elsewhere.
#:   will_read_untrusted   the operator's forecast that this job touches external content.
#:                         A declaration, not an observation -- see the module docstring.
#:   expected_duration     the operator's estimate, "short" or "long"
SAFE_FEATURES = frozenset({
    "declared_class",
    "surface",
    "fixture_kinds",
    "will_read_untrusted",
    "expected_duration",
})

#: Extension -> class, for the one feature that is a filesystem fact. Only unambiguous
#: mappings appear: `.txt` and `.csv` are deliberately absent because they are the shape of
#: half the classes at once, and a router that guesses between them is inventing the routing
#: decision rather than reading it.
FIXTURE_KIND_TO_CLASS = {
    ".xlsx": "spreadsheet",
    ".xlsm": "spreadsheet",
    ".docx": "document",
    ".pptx": "document",
    ".pdf": "document",
    ".sql": "sql",
    ".py": "coding",
    ".ts": "coding",
    ".js": "coding",
}

#: Which way is stricter, per parameter. "Stricter" is not a property of a number, so it has
#: to be declared per coordinate or it cannot be checked at all:
#:
#:   max_refute_passes  MORE passes is stricter -- the candidate faces more attempts to break it
#:   memory_max_items   FEWER items is stricter -- less history primed into the goal
#:
#: `max_retries` is absent on purpose. A larger retry budget is neither more nor less strict;
#: it is a resource decision, and pretending it has a direction would make the check produce
#: confident answers about a coordinate it cannot judge.
STRICTER_DIRECTION = {
    "max_refute_passes": +1,
    "memory_max_items": -1,
}


class RoutingError(ValueError):
    """Raised only for a malformed call. An unroutable TASK is not an error -- it is the root."""


def _strict_bool(value):
    """True/False only. Anything else is not a boolean and is not guessed at.

    `bool("false")` is True, and a feature that arrives as the string "false" would otherwise
    route every such task to the strict branch while reading, in the audit record, exactly
    like a task that asked for it.
    """
    return value if isinstance(value, bool) else None


def at_least_as_strict(root: dict, branch: dict) -> dict:
    """Is `branch` no more permissive than `root`, on the coordinates that have a direction?

    Returns {"ok", "relaxed", "unjudged"}. `relaxed` names coordinates that moved the
    permissive way; `unjudged` names coordinates that differ but have no declared direction,
    reported rather than passed over so "we checked" is not confused with "there was nothing
    to check".
    """
    r = (root or {}).get("parameters") or {}
    b = (branch or {}).get("parameters") or {}
    relaxed, unjudged = [], []
    for key in sorted(set(r) | set(b)):
        if r.get(key) == b.get(key):
            continue
        direction = STRICTER_DIRECTION.get(key)
        if direction is None:
            unjudged.append(key)
            continue
        try:
            moved = (float(b.get(key, 0)) - float(r.get(key, 0))) * direction
        except (TypeError, ValueError):
            unjudged.append(key)
            continue
        if moved < 0:
            relaxed.append(key)
    # A COMPONENT SWAP IS NOT JUDGED. "memory/v2 is stricter than memory/v1" is not something
    # a comparison of strings can establish, so it lands in `unjudged` and a caller that
    # cares has to look.
    for key in sorted(set((root or {}).get("components") or {})
                      | set((branch or {}).get("components") or {})):
        if ((root or {}).get("components") or {}).get(key) != \
                ((branch or {}).get("components") or {}).get(key):
            unjudged.append("components.%s" % key)
    return {"ok": not relaxed, "relaxed": relaxed, "unjudged": unjudged}


def _feature_problem(name, raw) -> str:
    """Why this feature may not decide a route, or "" if it may."""
    if name not in SAFE_FEATURES:
        return ("%r is not a safe routing feature; it would let the task's own content choose "
                "the configuration it runs under" % name)
    if not (isinstance(raw, dict) and "authority" in raw):
        return ("%r arrived without provenance; untagged and trusted must not be the same "
                "thing, or the check is skipped by omitting one field" % name)
    authority = PROV.normalise(raw.get("authority"))
    if authority not in ROUTING_AUTHORITIES:
        return ("%r was established by %s, which may not choose a harness -- content, and an "
                "agent that has just read content, are both downstream of the input"
                % (name, authority))
    return ""


def classify(features, *, tree=None) -> dict:
    """Which task class this job routes to, and an account of how that was decided.

    Returns {"task_class", "used", "refused", "reason"}. `task_class` is None when nothing
    safe decided it, which `route` turns into the root manifest.

    Every feature must be a provenance-tagged {"authority", "value"}. Refusals are RECORDED
    rather than dropped, so a reader can see that something tried to influence the route and
    did not.

    ONE CLASS OR NONE, AND THE RULE HAS NO EXCEPTIONS. Two safe features naming different
    classes do not make one of them right, and taking the first would make the route depend on
    dict ordering. An earlier draft exempted `expected_duration` from this and let it be
    quietly overridden, which meant the documented conflict rule and the implemented one
    disagreed. The pair is reported and the job runs on the root -- the reviewed configuration
    is the correct answer to "we are not sure", and it is the only answer that cannot be
    steered.
    """
    if features is None:
        features = {}
    if not isinstance(features, dict):
        raise RoutingError("features must be a dict of {name: {'authority', 'value'}}")

    used, refused, candidates = {}, {}, {}
    for name, raw in features.items():
        why = _feature_problem(name, raw)
        if why:
            refused[name] = why
            continue
        value = raw.get("value")
        if value in (None, "", [], {}):
            continue
        # DEEP COPY: the account is evidence. Holding the caller's list would let the record
        # of what was read change after the reading, without the class or the reason moving.
        used[name] = copy.deepcopy(value)

    declared = str(used.get("declared_class") or "").strip().lower()
    if declared:
        if declared not in TASK_CLASSES:
            refused["declared_class"] = (
                "%r is not one of the declared task classes, so there is no branch it could "
                "name; routing to the root rather than to a class that does not exist"
                % declared)
        else:
            candidates["declared_class"] = declared

    kinds = used.get("fixture_kinds")
    if isinstance(kinds, str):
        # A bare string would otherwise iterate character by character and match nothing,
        # which reads in the record as "the fixtures said nothing".
        refused["fixture_kinds"] = "fixture_kinds must be a list of extensions, not a string"
    elif kinds:
        from_fixtures = {FIXTURE_KIND_TO_CLASS.get(str(e).strip().lower()) for e in kinds}
        from_fixtures.discard(None)
        if len(from_fixtures) == 1:
            candidates["fixture_kinds"] = from_fixtures.pop()

    surface = str(used.get("surface") or "").strip().lower()
    if surface:
        if surface == "m365_cloud":
            candidates["surface"] = "m365_cloud"
        elif surface != "local":
            refused["surface"] = "%r is not a known surface" % surface

    duration = str(used.get("expected_duration") or "").strip().lower()
    if duration:
        if duration == "long":
            candidates["expected_duration"] = "long_running_local"
        elif duration != "short":
            refused["expected_duration"] = "%r is not a known duration" % duration

    # THE STRICT ROUTE IS A CANDIDATE LIKE ANY OTHER. An earlier draft returned it early,
    # which skipped both the conflict rule and the branch-existence check -- so a decision
    # could say "routed to the branch that assumes the input is hostile" while `resolve`
    # silently handed back the root, and the account claimed a protection never applied.
    if _strict_bool(used.get("will_read_untrusted")) is True:
        candidates["will_read_untrusted"] = "security_sensitive"

    chosen = {c for c in candidates.values() if c}
    if len(chosen) > 1:
        return {"task_class": None, "used": used, "refused": refused,
                "reason": ("safe features disagree (%s), and picking one would make the route "
                           "depend on which was read first; running on the reviewed root"
                           % ", ".join("%s=%s" % (k, v)
                                       for k, v in sorted(candidates.items()) if v))}
    if not chosen:
        return {"task_class": None, "used": used, "refused": refused,
                "reason": "no safe feature named a class; running on the reviewed root"}

    task_class = chosen.pop()
    named_by = ", ".join(sorted(k for k, v in candidates.items() if v == task_class))
    if tree is not None and task_class not in (tree.get("overrides") or {}):
        return {"task_class": task_class, "used": used, "refused": refused,
                "no_branch": True,
                "reason": ("routed to %r by %s, which this tree has no branch for; it will "
                           "resolve to the root" % (task_class, named_by))}
    return {"task_class": task_class, "used": used, "refused": refused,
            "reason": "routed to %r by %s" % (task_class, named_by)}


def route(tree, features) -> dict:
    """Classify, then resolve -- the whole decision with its account attached.

    Returns the `harness_tree.resolve` result with `routing` alongside it. The account travels
    with the manifest deliberately: once a tree exists, "which harness produced this number"
    has a per-class answer, and an archive row that records the harness id without recording
    why that harness was chosen cannot be reproduced.

    A STRICT ROUTE TO A BRANCH THAT IS NOT STRICTER IS REFUSED. Routing toward
    `security_sensitive` is only protective if that branch actually is; a branch of that name
    with a lower refuter budget is an escalation wearing the right label. When the check
    fails the job falls back to the root and the reason says so, because running under a
    branch that was chosen for a property it does not have is worse than running under the
    configuration a human reviewed.
    """
    decision = classify(features, tree=tree)
    task_class = decision["task_class"]

    if task_class == "security_sensitive" and not decision.get("no_branch"):
        candidate = HT.resolve(tree, task_class)
        strictness = at_least_as_strict(tree["root"], candidate["manifest"])
        decision["strictness"] = strictness
        if not strictness["ok"]:
            decision = dict(decision, task_class=None, strictness=strictness,
                            reason=("the strict route was refused: branch "
                                    "'security_sensitive' relaxes %s relative to the root, so "
                                    "routing to it would be an escalation wearing the name of "
                                    "a protection" % ", ".join(strictness["relaxed"])))
            task_class = None

    resolved = HT.resolve(tree, task_class or "")
    return dict(resolved, routing=decision)


def held_out_advantage(per_class_rows, *, alpha=0.05, min_n=20, min_pp=1.0) -> dict:
    """Does the specialised branch actually beat the global harness on held-out tasks?

    `per_class_rows` maps a task class to a list of
    {"episode_id", "specialised": bool, "global": bool} -- outcomes on episodes that were NOT
    used to choose the branch. The brief asks for this measurement specifically, and it is
    what decides whether a branch is configuration or decoration.

    PAIRED BY EPISODE ID, NEVER BY POSITION. An earlier draft zipped two anonymous lists and
    truncated to the shorter one, which pairs episode 3 of one arm with episode 5 of the other
    and silently discards the tail -- dropping a specialised arm's trailing failures is enough
    to manufacture an advantage. A row without an id, or with a non-boolean outcome, is
    counted as unusable and reported rather than coerced: `bool("FAIL")` is True.

    THE VERDICT COMES FROM THE EXISTING GATE, not from comparing two rates. Five wins against
    five losses is a 100-point gap and p = 0.0625, and a module that called that an advantage
    would grow branches fitted to whatever noise each class happened to contain -- the exact
    failure `harness_tree` warns about in its own docstring. `guards.significance_gate` already
    encodes the rule, so this defers to it rather than reimplementing a weaker one.
    """
    from relay.selfimprove import guards as G

    out = {"classes": {}, "ahead": [], "behind": [], "not_established": []}
    for task_class, rows in sorted((per_class_rows or {}).items()):
        ids, spec_ok, glob_ok, unusable = [], set(), set(), []
        for row in rows or []:
            eid = (row or {}).get("episode_id")
            s = _strict_bool((row or {}).get("specialised"))
            g = _strict_bool((row or {}).get("global"))
            if not eid or s is None or g is None:
                unusable.append(row)
                continue
            if eid in ids:
                unusable.append(row)        # a repeated id is not a second observation
                continue
            ids.append(eid)
            if s:
                spec_ok.add(eid)
            if g:
                glob_ok.add(eid)

        gate = G.significance_gate(spec_ok, glob_ok, ids,
                                   alpha=alpha, min_n=min_n, min_pp=min_pp)
        row_out = {"n_paired": len(ids), "unusable_rows": len(unusable),
                   "specialised_passed": len(spec_ok), "global_passed": len(glob_ok),
                   "gate": gate}
        # THE GATE'S "non-positive" COVERS BOTH "worse" AND "the same", because for KEEPING a
        # change those are the same answer. Here they are not: a branch that loses is a
        # finding, and a branch that ties is a branch nobody needs. So the regression side is
        # read from the numbers with the gate's own thresholds rather than from its label.
        regressed = (gate.get("n", 0) >= min_n
                     and gate.get("p") is not None and gate["p"] < alpha
                     and gate.get("net_pp", 0.0) <= -min_pp)
        if gate.get("keep"):
            out["ahead"].append(task_class)
        elif regressed:
            out["behind"].append(task_class)
        else:
            # UNDERPOWERED, SUGGESTIVE AND FLAT ARE ONE BUCKET HERE, because the action for
            # all three is the same: do not grow a branch on this. They stay distinguishable
            # in `gate["verdict"]` for a reader who wants to know which it was.
            out["not_established"].append(task_class)
        out["classes"][task_class] = row_out
    out["established"] = len(out["ahead"]) + len(out["behind"])
    return out
