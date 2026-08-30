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
ROUTABLE = (
    # execution
    "shell_exec", "run_python",
    # reading
    "read_file", "read_json", "list_directory",
    # searching -- WITHOUT THESE A CODING AGENT CANNOT WORK.
    #
    # The set started at five tools, which left eleven of the fleet's sixteen refused: no
    # glob, no grep, no find_files, no git_status. Measured on the first routed run that got
    # past the gateway: workers reported STUCK in batches, and a worker that cannot search the
    # checkout it was asked to fix has nothing else to report. Routing that stops the fleet
    # working is worse than no routing, because it looks like the models failing.
    "glob", "grep", "find_files", "git_status", "git_diff",
    # writing
    "write_file", "write_json", "append_file", "create_directory",
    "replace_in_file", "multi_edit",
)


class NotRoutable(Exception):
    """The call cannot be sent to a container, and must not be run here instead."""


class NotAFleetPath(NotRoutable):
    """This call is not the fleet's -- it names somewhere no run has staged.

    SEPARATE FROM NotRoutable ON PURPOSE. Routing was first gated on "is an autonomy contract
    armed", which is a DIFFERENT mechanism: the contract file is written by an operator, not
    by a bench run, so during a real routed run the predicate read False and every worker
    quietly executed on this machine -- in the address directories staging had left empty,
    because staging had correctly stopped cloning. The switch was on, the operator was told it
    was on, and nothing was contained.

    The predicate that actually separates the two populations is whether the path belongs to a
    staged instance. An operator's call names somewhere else and carries on unchanged; a call
    under the staging root that cannot be placed is refused, never run here.
    """


#: Where staging puts worktrees. A path under this root is the fleet's by construction, so a
#: call naming one that cannot be placed in a container is a failure, not a call to pass
#: through.
STAGING_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            ".fleet", "swe", "work")


def is_fleet_path(path):
    if not path:
        return False
    root = os.path.normcase(os.path.abspath(STAGING_ROOT))
    p = os.path.normcase(os.path.abspath(path))
    return p == root or p.startswith(root + os.sep)


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



# ── running a small program inside the container ───────────────────────────────────────────
#
# The search tools are reimplemented by RUNNING PYTHON IN THE CONTAINER rather than by
# approximating them with find/grep flags. `glob("**/*.py")`, grep's case folding and its
# `glob=` filter, and find_files' substring match all have exact semantics in the tool the
# fleet is used to, and an approximation that differs quietly is worse than a refusal: the
# worker gets a plausible answer that is missing files.
#
# The program is base64'd to a file and then run. Quoting a program through JSON, ssh, bash and
# docker exec is four chances to change what runs.
def _run_py(bc, inst, program, argv=(), timeout=120):
    import base64 as _b64
    import shlex as _shlex
    b = _b64.b64encode(program.encode("utf-8")).decode("ascii")
    args = " ".join(_shlex.quote(str(a)) for a in argv)
    cmd = ("printf %s '" + b + "' | base64 -d > /tmp/_routed_tool.py && "
           "cd /app && python3 /tmp/_routed_tool.py " + args)
    res = bc.exec_(inst, cmd, timeout=timeout)
    return res.get("output") or ""


#: Read a file out of the container, apply a pure transform, write it back. The edit tools all
#: have this shape, and doing it here keeps their semantics in one place instead of three.
def _edit_in_container(bc, inst, cpath, transform):
    current = _cat(bc, inst, cpath)
    new, note = transform(current)
    if new is None:
        return note
    _write(bc, inst, cpath, new)
    return note



# ── reading and writing files INSIDE the container ─────────────────────────────────────────
#
# NOT bc.put / bc.get. Those write to $WORK_ROOT/<instance> on the host, which the container
# sees at /work -- the scratch mount. The checkout is at /app. So a worker calling write_file
# on a source file got "wrote <path>", read_file handed the same bytes straight back, and the
# file the build and the graded diff read was never touched. Self-consistently wrong is the
# hardest kind of broken to notice, and broker.sh warns about exactly this in its own comments.
#
# Measured: create_directory (which goes through exec) made /app/_probe_dir, write_file put the
# file at /work/app/_probe_dir/t.txt, and git_status in /app showed nothing.
#
# base64 both ways: the content is arbitrary source text, and quoting it through JSON, ssh,
# bash and docker exec is four chances to change what lands on disk.
def _cat(bc, inst, cpath, timeout=120):
    """The file's text, or None if it is not there."""
    import base64 as _b64
    import shlex as _shlex
    res = bc.exec_(inst, "base64 -w0 -- %s 2>/dev/null" % _shlex.quote(cpath), timeout=timeout)
    raw = (res.get("output") or "").strip()
    if res.get("rc") or not raw:
        return None
    try:
        return _b64.b64decode(raw).decode("utf-8", "replace")
    except Exception:
        return None


def _write(bc, inst, cpath, text, timeout=180):
    """Replace the file's contents. Creates parent directories."""
    import base64 as _b64
    import shlex as _shlex
    b = _b64.b64encode((text or "").encode("utf-8")).decode("ascii")
    q = _shlex.quote(cpath)
    cmd = ("mkdir -p \"$(dirname %s)\" && printf %%s '%s' | base64 -d > %s" % (q, b, q))
    res = bc.exec_(inst, cmd, timeout=timeout)
    if res.get("rc"):
        raise NotRoutable("could not write %s in the container: %s"
                          % (cpath, (res.get("output") or "").strip()[:200]))
    return True

_GLOB_PROG = """
import glob, os, sys
pattern, base, cap, hidden = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4] == '1'
os.chdir(base)
hits = [p for p in glob.glob(pattern, recursive=True) if os.path.isfile(p)]
if not hidden:
    hits = [p for p in hits if not any(part.startswith('.') for part in p.split(os.sep))]
hits.sort()
print('%d matches' % len(hits))
for p in hits[:cap]:
    print(os.path.join(base, p))
"""

_FIND_PROG = """
import os, sys
needle, base, cap = sys.argv[1], sys.argv[2], int(sys.argv[3])
hits = []
for dp, dn, fn in os.walk(base):
    dn[:] = [d for d in dn if not d.startswith('.')]
    for f in fn:
        if needle.lower() in f.lower():
            hits.append(os.path.join(dp, f))
hits.sort()
print('%d files' % len(hits))
for p in hits[:cap]:
    print(p)
"""

_GREP_PROG = """
import fnmatch, os, sys
pattern, base, globpat, cap, cs = (sys.argv[1], sys.argv[2], sys.argv[3],
                                   int(sys.argv[4]), sys.argv[5] == '1')
needle = pattern if cs else pattern.lower()
out, total = [], 0
for dp, dn, fn in os.walk(base):
    dn[:] = [d for d in dn if not d.startswith('.')]
    for f in sorted(fn):
        if globpat and not fnmatch.fnmatch(f, globpat):
            continue
        path = os.path.join(dp, f)
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as fh:
                for i, line in enumerate(fh, 1):
                    hay = line if cs else line.lower()
                    if needle in hay:
                        total += 1
                        if len(out) < cap:
                            out.append('%s:%d: %s' % (path, i, line.rstrip()[:300]))
        except (OSError, UnicodeError):
            continue
print('%d matches' % total)
for line in out:
    print(line)
"""

def route(name, args):
    """Run one call in the instance's container, or raise NotRoutable. Never runs locally.

    Returns the string a tool would have returned, so the caller can hand it back unchanged.
    """
    from relay import broker_client as bc
    if not bc.enabled():
        raise NotRoutable("routing is off")
    # WHOSE CALL IS THIS, BEFORE WHAT THE TOOL IS.
    #
    # The ROUTABLE check came first, so with routing switched on this function refused every
    # one of the ~150 tools outside that list -- for the operator as much as for the fleet.
    # Measured: `stop_check` came back "has no container equivalent yet", and stop_check does
    # not touch the filesystem at all. The path predicate that separates the two populations
    # was never reached, so the lockout the previous fix was written to prevent was still
    # there by another door.
    a = dict(args or {})
    where = a.get("working_dir") or a.get("path") or os.getcwd()
    inst = instance_for(where)
    if not inst and not is_fleet_path(where):
        raise NotAFleetPath("%s is not under the staging root; this is not a fleet call"
                            % where)

    # From here the call names the fleet's own tree, so a tool with no container equivalent is
    # a refusal rather than a pass-through: running it here is the thing being prevented.
    if name not in ROUTABLE:
        raise NotRoutable("%s has no container equivalent yet; refusing rather than "
                          "running it on the operator's machine" % name)
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
        text = _cat(bc, inst, to_container_path(a.get("path"), inst))
        if text is None:
            return "[read_file: %s does not exist in the container]" % a.get("path")
        return text
    if name == "write_file":
        _write(bc, inst, to_container_path(a.get("path"), inst), a.get("content") or "")
        return "wrote %s in %s" % (a.get("path"), inst)
    if name == "list_directory":
        res = bc.exec_(inst, "ls -la %s" % to_container_path(a.get("path") or ".", inst),
                       timeout=60)
        return res.get("output") or ""

    # ── searching ──────────────────────────────────────────────────────────────────────────
    if name == "glob":
        return _run_py(bc, inst, _GLOB_PROG,
                       (a.get("pattern") or "*",
                        to_container_path(a.get("path") or ".", inst),
                        int(a.get("max_results") or 200),
                        "1" if a.get("include_hidden") else "0"))
    if name == "find_files":
        return _run_py(bc, inst, _FIND_PROG,
                       (a.get("name_contains") or "",
                        to_container_path(a.get("path") or ".", inst),
                        int(a.get("max_results") or 200)))
    if name == "grep":
        return _run_py(bc, inst, _GREP_PROG,
                       (a.get("pattern") or "",
                        to_container_path(a.get("path") or ".", inst),
                        a.get("glob") or "",
                        int(a.get("max_matches") or 100),
                        "1" if a.get("case_sensitive") else "0"), timeout=180)
    if name == "git_status":
        res = bc.exec_(inst, "cd %s && git status --porcelain" % CONTAINER_REPO, timeout=90)
        return res.get("output") or ""
    if name == "git_diff":
        flag = " --staged" if a.get("staged") else ""
        cap = int(a.get("max_lines") or 800)
        res = bc.exec_(inst, "cd %s && git diff%s | head -n %d" % (CONTAINER_REPO, flag, cap),
                       timeout=120)
        return res.get("output") or ""

    # ── writing ────────────────────────────────────────────────────────────────────────────
    if name == "read_json":
        text = _cat(bc, inst, to_container_path(a.get("path"), inst))
        if text is None:
            return "[read_json: %s does not exist in the container]" % a.get("path")
        return text
    if name == "write_json":
        _write(bc, inst, to_container_path(a.get("path"), inst), a.get("json_data") or "")
        return "wrote %s in %s" % (a.get("path"), inst)
    if name == "create_directory":
        cp = to_container_path(a.get("path"), inst)
        bc.exec_(inst, "mkdir -p %s" % cp, timeout=60)
        return "created %s" % a.get("path")
    if name == "append_file":
        cp = to_container_path(a.get("path"), inst)

        def _append(current):
            return (current or "") + (a.get("content") or ""), "appended to %s" % a.get("path")
        return _edit_in_container(bc, inst, cp, _append)
    if name == "replace_in_file":
        cp = to_container_path(a.get("path"), inst)
        old_s, new_s = a.get("old") or "", a.get("new") or ""
        want = a.get("expected_replacements")

        def _replace(current):
            if current is None:
                return None, "[replace_in_file: %s does not exist in the container]" % a.get("path")
            n = current.count(old_s)
            # THE COUNT IS CHECKED BEFORE WRITING, as the local tool does. An edit that matched
            # a different number of places than the caller expected is the caller being wrong
            # about the file, and writing it anyway is how a fix lands somewhere else.
            if want is not None and n != int(want):
                return None, ("[replace_in_file: expected %s replacements, found %d]"
                              % (want, n))
            if n == 0:
                return None, "[replace_in_file: no occurrence of the given text]"
            return current.replace(old_s, new_s), "replaced %d occurrence(s) in %s" % (n, a.get("path"))
        return _edit_in_container(bc, inst, cp, _replace)
    if name == "multi_edit":
        cp = to_container_path(a.get("path"), inst)
        edits = a.get("edits") or []

        def _multi(current):
            if current is None:
                return None, "[multi_edit: %s does not exist in the container]" % a.get("path")
            text, done = current, 0
            for e in edits:
                if not isinstance(e, dict):
                    continue
                o, n = e.get("old") or e.get("old_string") or "", e.get("new") or e.get("new_string") or ""
                if o and o in text:
                    text = text.replace(o, n)
                    done += 1
                else:
                    # ALL OR NOTHING, like the local tool. A partially applied multi_edit leaves
                    # a file that compiles against neither the old shape nor the new one.
                    return None, ("[multi_edit: edit %d did not match; nothing was written]"
                                  % (done + 1))
            return text, "applied %d edit(s) to %s" % (done, a.get("path"))
        return _edit_in_container(bc, inst, cp, _multi)

    raise NotRoutable("unhandled routable tool %s" % name)
