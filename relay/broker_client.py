"""Talk to the execution broker on the eval host.

WHAT THIS IS FOR. The containment plan's Step 2 moves the fleet's file and shell tools off
this machine, so that a worker's blast radius is one container on an idle box rather than the
operator's own account. bench/remote/broker.sh is the far side; this is the near side.

WHY A CLIENT MODULE AND NOT A FEW LINES IN EACH TOOL. There are sixteen tools in the fleet's
allowed set and every one of them needs the same four things: the request framed as JSON, the
transport pinned to a key that can only reach the broker, a refusal that says which of the two
sides refused, and no path from this machine leaking into the request. Written per tool, that
is sixteen chances to get one of them wrong -- which is the shape this repository has been
bitten by before (the same fault reached by two callers is one fault).

WHAT IT DELIBERATELY DOES NOT DO. It does not fall back to running the command locally. A
transport that silently degrades to the thing it was built to avoid is worse than one that
fails: the run would carry on, look identical, and be unconfined. Every failure here is
returned as a refusal.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess

#: The ssh host alias whose key is pinned to a forced command on the far side. Not a hostname:
#: the alias carries the ProxyCommand and the identity file, and neither belongs in this file.
BROKER_HOST = os.environ.get("SWE_BROKER_HOST", "swe-broker")

#: Off by default. Step 2 is a change to where every fleet tool lands, and it is switched on
#: deliberately rather than by importing this module.
def enabled() -> bool:
    return (os.environ.get("SWE_BROKER") or "").strip().lower() in ("1", "on", "true", "yes")


class BrokerError(RuntimeError):
    """The broker refused, or could not be reached. Never raised for a tool's own failure."""


def call(request: dict, timeout: int = 900) -> dict:
    """One request, one response. Raises BrokerError; never runs anything locally.

    The request goes on STDIN because the far side is an SSH forced command: whatever is put
    on the command line is discarded, so the command line is not a channel.
    """
    body = json.dumps(request, ensure_ascii=False)
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", BROKER_HOST],
            input=body, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise BrokerError("broker did not answer within %ds" % timeout)
    except OSError as exc:
        raise BrokerError("could not reach the broker: %s: %s" % (type(exc).__name__, exc))

    out = (proc.stdout or "").strip()
    if not out:
        # THE STDERR MATTERS HERE. An empty stdout with a non-zero rc is ssh failing, not the
        # broker refusing, and telling those apart is the difference between "fix the key" and
        # "fix the request".
        raise BrokerError("broker returned nothing (rc=%d): %s"
                          % (proc.returncode, (proc.stderr or "").strip()[:300]))
    try:
        # The broker answers with exactly one JSON object; anything else means the forced
        # command is not what we think it is.
        res = json.loads(out.splitlines()[-1])
    except ValueError:
        raise BrokerError("broker answer was not JSON: %s" % out[:300])
    if not isinstance(res, dict):
        raise BrokerError("broker answer was not an object")
    if not res.get("ok"):
        raise BrokerError("broker refused: %s" % res.get("error", "(no reason given)"))
    return res


def ping() -> dict:
    return call({"verb": "ping"}, timeout=60)


def create(instance: str, image: str, network: str = "bridge") -> dict:
    """Start the instance's container.

    `network` is stated rather than defaulted silently. "bridge" lets the build fetch its
    dependencies -- which it must, and which also means egress is not restricted. "none"
    gives no network at all and is the right choice for evaluation, where nothing should be
    downloaded and anything reaching out is a finding rather than a normal step.
    """
    if network not in ("bridge", "none"):
        raise BrokerError("network must be bridge or none, not %r" % network)
    return call({"verb": "create", "instance": instance, "image": image,
                 "network": network}, timeout=1800)


def destroy(instance: str) -> dict:
    return call({"verb": "destroy", "instance": instance}, timeout=300)


def exec_(instance: str, cmd: str, timeout: int = 600) -> dict:
    """Run a shell command inside the instance's container.

    `cmd` is base64'd rather than quoted. Quoting a shell command through JSON, ssh, and two
    shells is four chances to change what runs, and a command that changes on the way is worse
    than one that is refused.
    """
    return call({"verb": "exec", "instance": instance,
                 "cmd": base64.b64encode(cmd.encode("utf-8")).decode("ascii"),
                 "timeout": int(timeout)}, timeout=timeout + 120)


def put(instance: str, rel_path: str, content: str) -> dict:
    return call({"verb": "put", "instance": instance, "path": rel_path,
                 "content_b64": base64.b64encode(content.encode("utf-8")).decode("ascii")})


def get(instance: str, rel_path: str) -> str:
    res = call({"verb": "get", "instance": instance, "path": rel_path})
    return base64.b64decode(res["content_b64"]).decode("utf-8", "replace")
