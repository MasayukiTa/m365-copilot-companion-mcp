"""The broker's request parser, exercised the way the broker runs it.

Run as a SUBPROCESS with the request on stdin, not by importing main(): the parser's contract
is "JSON on stdin, shell assignments on stdout", and the first version of it could not receive
its input at all -- it was inlined as `python3 - <<'PY'`, which makes the heredoc stdin, so the
request was discarded and the parser read its own source. Importing the function would have
passed against that bug.
"""
import json
import os
import subprocess
import sys

PARSER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "broker_parse.py")


def _parse(req):
    """Return the assignments the parser emits, as a dict. Refusals appear as BROKER_ERR."""
    proc = subprocess.run([sys.executable, PARSER], input=json.dumps(req),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = {}
    for line in (proc.stdout or "").splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        # shlex.quote'd; strip the single quotes it adds around anything non-trivial.
        if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
            v = v[1:-1].replace("'\\''", "'")
        out[k] = v
    return out


def test_an_omitted_network_gets_no_network_at_all():
    """The permissive mode has to be asked for.

    It defaulted to "bridge", so a request that simply left the field out got unrestricted
    egress. Wrong twice over: a permission nobody asked for should not be granted, and egress
    here also decides the SCORE -- these repositories are public and the commit that fixes each
    instance is upstream, so a solver that can reach the network can fetch the answer.
    """
    out = _parse({"verb": "create", "instance": "i1", "image": "jefzda/sweap-images:x"})
    assert out.get("BROKER_ERR") is None, out
    assert out["SWE_NET"] == "none"


def test_bridge_is_still_available_when_stated():
    out = _parse({"verb": "create", "instance": "i1", "image": "jefzda/sweap-images:x",
                  "network": "bridge"})
    assert out["SWE_NET"] == "bridge"


def test_any_other_network_is_refused_rather_than_handed_to_docker():
    """docker would accept a named network -- including one somebody else created."""
    out = _parse({"verb": "create", "instance": "i1", "image": "jefzda/sweap-images:x",
                  "network": "host"})
    assert "BROKER_ERR" in out
    assert "bridge or none" in out["BROKER_ERR"]


def test_an_image_from_anywhere_else_is_refused():
    out = _parse({"verb": "create", "instance": "i1", "image": "ubuntu:latest"})
    assert "BROKER_ERR" in out


def test_a_path_may_not_escape_the_work_root():
    out = _parse({"verb": "get", "instance": "i1", "path": "../../etc/passwd"})
    assert "BROKER_ERR" in out


def test_an_unknown_verb_is_refused():
    out = _parse({"verb": "rm", "instance": "i1"})
    assert "BROKER_ERR" in out
