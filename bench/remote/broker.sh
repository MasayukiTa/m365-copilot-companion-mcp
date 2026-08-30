#!/bin/bash
# The only program a fleet worker's SSH key is allowed to run on the eval host.
#
# WHY THIS EXISTS. Measured 2026-08-30: workers run as the logged-in user of the laptop with
# no enforced boundary. One installed Go system-wide through winget; another installed an
# open-source project's requirements into the harness's own virtualenv and stopped the MCP
# server from importing. Neither bypassed anything -- the tools did what they said, in the
# wrong place. An external review's verdict was that a regex denylist is the wrong shape and
# the only real control is the process token plus the resources it can reach.
#
# So execution moves here, and this file is the door. Two properties matter:
#
#   1. THE CLIENT DOES NOT CHOOSE THE PROGRAM. This runs as an SSH forced command, so
#      whatever the client puts on the command line is discarded. The request arrives on
#      STDIN and is parsed here. That also sidesteps WSLENV: SSH_ORIGINAL_COMMAND does not
#      cross into WSL without being forwarded, and a door whose lock depends on an
#      environment variable arriving intact is not a lock.
#
#   2. THE HOST IS NOT THE SANDBOX. This script runs as root inside WSL and can see the whole
#      of /mnt/c -- the entire home server. So it never hands a path through: every verb
#      operates inside one container per instance, the work root lives in the Linux
#      filesystem and NOT under /mnt/c, and no container gets the docker socket. A container
#      escape must land on an idle box, not on the operator's files.
# WSL IS THIS MACHINE'S ACCIDENT, NOT A REQUIREMENT OF THIS DESIGN.
#
# Nothing executable below mentions WSL: it is bash and docker, and it runs on any Linux
# host. The eval box here happens to reach Linux through WSL because that is how this
# particular owner's test machine is set up -- most machines have no WSL at all and are not
# expected to. The dependency is exactly ONE LINE, and it is not in this file: the forced
# command in the server's authorized_keys. On this box that line reads
#     command="C:\Windows\System32\wsl.exe -d Ubuntu -e /opt/swe-broker/broker.sh"
# and on a native Linux host it reads
#     command="/opt/swe-broker/broker.sh"
# Keep it that way. A design that assumes WSL cannot be moved to the machine that eventually
# runs it, and the comments in this file mentioning /mnt/c describe what THIS host exposes,
# not something the broker needs.

set -u
umask 077

WORK_ROOT=/srv/swe-work
LOG=/var/log/swe-broker.log
MEM_LIMIT=${SWE_MEM_LIMIT:-4g}
CPU_LIMIT=${SWE_CPU_LIMIT:-2}
PIDS_LIMIT=${SWE_PIDS_LIMIT:-512}
EXEC_TIMEOUT_MAX=3600

log() { printf '%s %s\n' "$(date -Is)" "$*" >> "$LOG" 2>/dev/null; }
fail() { printf '{"ok":false,"error":%s}\n' "$(printf '%s' "$1" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')"; log "DENY $1"; exit 1; }

mkdir -p "$WORK_ROOT" 2>/dev/null

REQ=$(cat)
[ -n "$REQ" ] || fail "empty request"
[ "${#REQ}" -le 4000000 ] || fail "request too large"

# Parsed with python, not with a shell case over the raw text: the fields are attacker-shaped
# and a shell that splits them is a shell that can be made to run them.
# Parsed by a SEPARATE PROGRAM, not by a shell case over the raw text: the fields are
# attacker-shaped and a shell that splits them is a shell that can be made to run them.
# It is a file rather than a heredoc because `python3 - <<EOF` makes the heredoc stdin, and
# the request piped in would be thrown away -- the parser would read its own source.
eval "$(printf '%s' "$REQ" | python3 /opt/swe-broker/broker_parse.py)"

if [ -n "${BROKER_ERR:-}" ]; then fail "$BROKER_ERR"; fi

CNAME="swe_${INSTANCE:-none}"

case "$VERB" in
  ping)
    printf '{"ok":true,"pong":true,"work_root":"%s"}\n' "$WORK_ROOT"; log "ping"; exit 0 ;;

  list)
    printf '{"ok":true,"containers":%s}\n' \
      "$(docker ps -a --filter 'name=^swe_' --format '{{.Names}}' | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read().split()))')"
    exit 0 ;;

  create)
    mkdir -p "$WORK_ROOT/$INSTANCE"
    docker rm -f "$CNAME" >/dev/null 2>&1
    # --entrypoint, BECAUSE THE IMAGE HAS ONE. These images set ENTRYPOINT ["/bin/bash"], so
    # `docker run <img> sleep infinity` runs `bash /usr/bin/sleep infinity` and the container
    # dies with 126 "cannot execute binary file". Measured on the first probe: created, and
    # gone before the next call could reach it.
    #
    # /work IS SCRATCH; THE CHECKOUT IS AT /app. Measured in the Step 3 pilot: these images
    # carry the instance's repository at /app, and /work is the per-instance writable mount
    # that maps back to $WORK_ROOT on the host. Tools that edit the repository must target
    # /app -- pointing them at /work would run cleanly and edit nothing, which is the
    # hardest kind of broken to notice.
    #
    # ROOT INSIDE THE CONTAINER, and that is a deliberate trade. uid 1000 does not exist in
    # these images and their build and test scripts assume root; forcing a non-existent user
    # breaks the very thing this is here to run. The boundary is the container -- no docker
    # socket, no /mnt/c, no host paths, capabilities dropped, no-new-privileges -- not the
    # uid inside it. What that buys: an escape lands on an idle box rather than on the
    # operator's files. What it does not buy: protection from a kernel-level escape, which
    # would hold uid 0 on the host, because there is no user-namespace remapping here.
    # NETWORK IS AN EXPLICIT CHOICE, NOT A DEFAULT NOBODY MADE.
    #
    # A container that can reach the whole internet can fetch arbitrary code and send
    # anything out, and these run third-party build systems -- an install step IS arbitrary
    # code execution. Blocking egress outright breaks the dependency fetch every one of
    # these instances needs, so the honest arrangement is two modes with the decision
    # recorded per instance, rather than one default nobody chose:
    #
    #   bridge  (default) dependencies can be fetched; egress is NOT restricted
    #   none              no network at all -- correct for evaluation, where nothing should
    #                     be downloaded and anything reaching out is a finding
    #
    # This is not an allowlist. A real one needs a proxy or host firewall rules, and saying
    # so is better than shipping the bridge default and calling it controlled.
    # --restart unless-stopped, BECAUSE THE DAEMON DOES NOT STAY UP.
    #
    # dockerd here runs inside WSL, and WSL terminates the distribution when the last process
    # in it exits -- which systemd sees as "Stopping docker.service". Measured 2026-08-31: 21
    # daemon starts in one day, and every container the broker had created was gone with
    # `Exited (255)` and no logs, while the one container that predated the broker survived
    # each cycle. Its only difference was a restart policy.
    #
    # Without this, a routed run loses every container the moment anything touches WSL, and
    # the near side sees "no running container for that instance" -- which reads like a bug
    # in the request rather than a daemon that went away. The container's filesystem layer
    # survives the restart, so a worker's edits to /app are not lost with it.
    NET="${SWE_NET:-bridge}"
    case "$NET" in
      bridge|none) ;;
      *) fail "network mode must be bridge or none" ;;
    esac
    if ! docker run -d --name "$CNAME" --network "$NET"         --restart unless-stopped         --entrypoint sleep         --memory "$MEM_LIMIT" --cpus "$CPU_LIMIT" --pids-limit "$PIDS_LIMIT"         --security-opt no-new-privileges         --cap-drop ALL         --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER         --cap-add SETUID --cap-add SETGID --cap-add FSETID         -v "$WORK_ROOT/$INSTANCE:/work" -w /work         "$IMAGE" infinity >/dev/null 2>>"$LOG"; then
      fail "container did not start"
    fi
    printf '{"ok":true,"container":"%s","network":"%s"}\n' "$CNAME" "$NET"; log "create $INSTANCE $IMAGE net=$NET"; exit 0 ;;

  exec)
    docker inspect -f '{{.State.Running}}' "$CNAME" 2>/dev/null | grep -q true || fail "no running container for that instance"
    OUT=$(printf '%s' "$CMD_B64" | base64 -d | timeout "$TIMEOUT" docker exec -i "$CNAME" bash -s 2>&1)
    RC=$?
    # `RC=... python3` -- AN ENVIRONMENT PREFIX, NOT AN ARGV ELEMENT. It was written as
    # `python3 -c '...' RC="$RC"`, which passes RC as sys.argv[1]; os.environ["RC"] then
    # raised KeyError, the `|| printf` fallback fired, and every single probe came back
    # {"ok":true,"rc":N,"output":""}. The limits were right, the container was right, and
    # the tool returned nothing -- which reads exactly like a command that printed nothing.
    printf '%s' "$OUT" | RC="$RC" python3 -c 'import json,os,sys; print(json.dumps({"ok":True,"rc":int(os.environ["RC"]),"output":sys.stdin.read()[-200000:]}))'
    log "exec $INSTANCE rc=$RC"; exit 0 ;;

  put)
    D="$WORK_ROOT/$INSTANCE"
    mkdir -p "$(dirname "$D/$RPATH")" 2>/dev/null
    printf '%s' "$CONTENT_B64" | base64 -d > "$D/$RPATH" || fail "write failed"
    printf '{"ok":true,"wrote":"%s"}\n' "$RPATH"; log "put $INSTANCE $RPATH"; exit 0 ;;

  get)
    F="$WORK_ROOT/$INSTANCE/$RPATH"
    [ -f "$F" ] || fail "no such file"
    printf '{"ok":true,"content_b64":"%s"}\n' "$(base64 -w0 < "$F")"; exit 0 ;;

  destroy)
    docker rm -f "$CNAME" >/dev/null 2>&1
    rm -rf "${WORK_ROOT:?}/$INSTANCE"
    printf '{"ok":true,"destroyed":"%s"}\n' "$INSTANCE"; log "destroy $INSTANCE"; exit 0 ;;
esac
fail "unreachable"
