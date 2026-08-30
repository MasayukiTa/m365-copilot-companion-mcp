"""Send a fleet worker's file and shell work to the container instead of to this machine.

WHAT THIS IS THE LAST PIECE OF. The broker (bench/remote/broker.sh) is the door and
relay/broker_client.py knocks on it; both are verified. Until something actually routes
through them the containment is a box nobody is put in -- measured: no tool referenced the
client at all, and the worktrees were still on this laptop.

Routing them closes three holes that looked separate and are one. A worker that cannot write
locally cannot forge an approval in .companion_gates, cannot reach the harness's virtualenv,
and cannot rewrite the policy file that constrains it. Those were listed as three items until
it became clear that the same OS-level fact underlies all of them: on a single-user machine
with no boundary, a worker writes wherever the operator can.

THE RULE THAT MAKES IT WORTH ANYTHING: there is no local fallback. If routing is on and the
instance cannot be resolved, the call is REFUSED. A router that quietly runs the command here
when it cannot reach the container leaves the run looking identical and unconfined, which is
worse than one that fails -- the operator would have no way to tell the two apart.
"""
from __future__ import annotations

import io
import json
import os

#: Local worktree root -> the path the same checkout has inside the container. Established by
#: the Step 3 pilot: these images carry the repository at /app, and /work is the per-instance
#: writable mount. A tool pointed at /work would run cleanly and edit nothing.
CONTAINER_REPO = "/app"

WT_MAP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      ".fleet", "swe", "pro_wt_map.json")

#: The tools this router knows how to send. Deliberately smaller than the fleet's allowed set:
#: a tool that is allowed but not yet routable must be refused while routing is on, not run
#: locally, and keeping the two lists separate is what makes that distinction expressible.
ROUTABLE = ("shell_exec", "run_python", "read_file", "write_file", "list_directory")


class NotRoutable(Exception):
    """The call cannot be sent to a container, and must not be run here instead."""


def _worktrees():
    try:
        m = json.load(io.open(WT_MAP, encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return {k: os.path.normcase(os.path.abspath(v)) for k, v in m.items()
            if isinstance(v, str)}


def instance_for(path):
    """Which instance a path belongs to, or None.

    Matched on the worktree root rather than on an instance id in the text, because the goals
    do not carry the id -- a join on the id matched nothing at all the first time it was tried.
    A path under two roots resolves to NEITHER: an ambiguous answer here would send one
    instance's edit into another instance's container.
    """
    if not path:
        return None
    p = os.path.normcase(os.path.abspath(path))
    hits = [inst for inst, root in _worktrees().items()
            if p == root or p.startswith(root + os.sep)]
    return hits[0] if len(hits) == 1 else None


def to_container_path(local_path, instance):
    """The same file, named from inside the container."""
    roots = _worktrees()
    root = roots.get(instance)
    if not root:
        raise NotRoutable("no worktree recorded for %s" % instance)
    p = os.path.normcase(os.path.abspath(local_path))
    if p == root:
        return CONTAINER_REPO
    if not p.startswith(root + os.sep):
        raise NotRoutable("%s is outside the instance's checkout" % local_path)
    rel = os.path.relpath(local_path, root).replace(os.sep, "/")
    return CONTAINER_REPO + "/" + rel


def route(name, args):
    """Run one call in the instance's container, or raise NotRoutable. Never runs locally.

    Returns the string a tool would have returned, so the caller can hand it back unchanged.
    """
    from relay import broker_client as bc
    if not bc.enabled():
        raise NotRoutable("routing is off")
    if name not in ROUTABLE:
        raise NotRoutable("%s has no container equivalent yet; refusing rather than "
                          "running it on the operator's machine" % name)

    a = dict(args or {})
    where = a.get("working_dir") or a.get("path") or os.getcwd()
    inst = instance_for(where)
    if not inst:
        raise NotRoutable("no instance owns %s; a call that cannot be placed in a container "
                          "must not run outside one" % where)

    if name in ("shell_exec", "run_python"):
        body = a.get("command") or a.get("code") or ""
        if not str(body).strip():
            raise NotRoutable("empty command")
        cmd = str(body) if name == "shell_exec" else (
            "python3 - <<'PYEOF'\n%s\nPYEOF" % body)
        # cd into the checkout: the container's default working directory is the scratch
        # mount, and a build run there would succeed while touching nothing.
        res = bc.exec_(inst, "cd %s && %s" % (CONTAINER_REPO, cmd),
                       timeout=int(a.get("timeout") or 600))
        out = res.get("output") or ""
        return "%s\n[exit %s]" % (out, res.get("rc"))

    if name == "read_file":
        return bc.get(inst, to_container_path(a.get("path"), inst).lstrip("/"))
    if name == "write_file":
        bc.put(inst, to_container_path(a.get("path"), inst).lstrip("/"),
               a.get("content") or "")
        return "wrote %s in %s" % (a.get("path"), inst)
    if name == "list_directory":
        res = bc.exec_(inst, "ls -la %s" % to_container_path(a.get("path") or ".", inst),
                       timeout=60)
        return res.get("output") or ""
    raise NotRoutable("unhandled routable tool %s" % name)
