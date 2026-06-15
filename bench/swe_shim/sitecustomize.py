"""PYTHONPATH-preloaded shim for the SWE-bench harness (swebench 4.1.0).

Why this exists
---------------
swebench's ``make_eval_script_list_py`` builds the in-container ``eval.sh`` with::

    test_files = get_modified_files(test_patch)
    reset_tests_command = f"git checkout {base_commit} {' '.join(test_files)}"

For an instance whose ``test_patch`` adds ONLY NEW files (every hunk's
``source_file == /dev/null``), ``get_modified_files`` returns ``[]``, so the
command degenerates to a *bare* ``git checkout {base_commit}`` that reverts the
WHOLE worktree to base -- undoing the ``pre_install`` edit that injected ``-rA``
into ``tox.ini``. pytest then runs with default dot output, emits no
``PASSED <id>`` lines, and ``parse_log_pytest_v2`` produces an empty status map,
so a genuinely-passing test is graded ``resolved: false``.

In SWE-bench Lite this hits exactly sphinx-doc__sphinx-8595 (new-file-only
test_patch + a parser-relevant ``-rA`` pre_install).

Fix
---
Export ``PYTEST_ADDOPTS`` so it carries ``-rA`` into pytest through a path the
bare checkout cannot touch: a shell ``export`` in ``eval.sh`` is process
environment, not a tracked file. sphinx's BASE ``tox.ini`` already whitelists it
(``passenv = ... PYTEST_ADDOPTS``) and forwards it
(``setenv = PYTEST_ADDOPTS = {env:PYTEST_ADDOPTS:} --color yes``), and that
``passenv`` line survives the bare checkout because it is in the base tree.

Batch-safe for all other instances:
  * pytest repos already pass ``-rA`` on the command line; a duplicate ``-rA``
    via PYTEST_ADDOPTS is an idempotent no-op.
  * django (./tests/runtests.py) and sympy (bin/test) are not pytest and ignore
    PYTEST_ADDOPTS entirely.

This file only takes effect when it is on PYTHONPATH (swe_check.py adds its
directory to PYTHONPATH in the WSL eval command). It is imported automatically
at interpreter startup, before ``-m swebench.harness.run_evaluation`` runs, so
the monkeypatch is in place before any TestSpec is built.
"""
import swebench.harness.test_spec.test_spec as _ts_mod
from swebench.harness.test_spec.create_scripts import (
    make_eval_script_list as _orig_make_eval_script_list,
)

# Report-chars we want pytest to emit: A == all (covers passed/failed/error/etc.)
# so parse_log_pytest_v2 can find the per-test status line.
_ADDOPTS = "-rA"


def _make_eval_script_list_with_addopts(
    instance, specs, env_name, repo_directory, base_commit, test_patch
):
    cmds = list(
        _orig_make_eval_script_list(
            instance, specs, env_name, repo_directory, base_commit, test_patch
        )
    )
    # ${PYTEST_ADDOPTS:-} keeps it safe under eval.sh's `set -u` and preserves any
    # value tox/coverage envs might already rely on. Insert right after the first
    # command (the initial `source .../activate`) so it is exported BEFORE the
    # reset checkout and the test command, and survives the bare checkout.
    export_line = 'export PYTEST_ADDOPTS="%s ${PYTEST_ADDOPTS:-}"' % _ADDOPTS
    if cmds:
        cmds.insert(1, export_line)
    else:
        cmds = [export_line]
    # Point HTTPBIN-reading suites (requests' test_requests.py) at a fast/reliable local httpbin
    # instead of the public httpbin.org (slow + 503s from here). Only when SWE_HTTPBIN_URL is set;
    # a repo that doesn't read HTTPBIN_URL ignores it. Inserted before the reset checkout so the
    # process-env export survives (same reasoning as PYTEST_ADDOPTS).
    import os as _os
    _hb = _os.environ.get("SWE_HTTPBIN_URL", "")
    if _hb:
        # 1) point the HTTPBIN_URL suite at httpbin.org (NOT an ip:port -- the scheme test reuses
        #    the netloc for https://, so it must have no explicit port);
        # 2) map httpbin.org -> the local gateway so http:// and hardcoded https://httpbin.org both
        #    hit the local server; 3) trust the local self-signed cert for the https side.
        _gw = _os.environ.get("SWE_HTTPBIN_GW", "172.17.0.1")
        cmds.insert(1, 'export HTTPBIN_URL="%s"' % _hb)
        cmds.insert(1, 'grep -q " httpbin.org" /etc/hosts 2>/dev/null || echo "%s httpbin.org" >> /etc/hosts' % _gw)
        _cert = _os.environ.get("SWE_HTTPBIN_CERT", "")
        if _cert and _os.path.isfile(_cert):
            import base64 as _b64
            _b = _b64.b64encode(open(_cert, "rb").read()).decode()
            cmds.insert(1, 'export CURL_CA_BUNDLE=/tmp/hbcert.pem')
            cmds.insert(1, 'export REQUESTS_CA_BUNDLE=/tmp/hbcert.pem')
            cmds.insert(1, 'echo %s | base64 -d > /tmp/hbcert.pem' % _b)
    return cmds


# Patch the name actually referenced inside test_spec.make_test_spec (it imported
# `make_eval_script_list` into its own module namespace).
_ts_mod.make_eval_script_list = _make_eval_script_list_with_addopts


# ── corporate-SSL workaround (opt-in: SWE_INSECURE_FETCH=1) ──────────────────────────────────
# This eval host's network INTERMITTENTLY MITM-inspects HTTPS with a self-signed root CA, which
# breaks swebench's requirements fetch from raw.githubusercontent.com
# (requests SSLError: CERTIFICATE_VERIFY_FAILED -> the env image can't build -> the whole eval
# produces no report -> a FALSE EVAL_ERROR even though the agent's patch is fine). Observed: a
# whole arm graded 7/12 EVAL_ERROR during an inspection window; minutes later the same fetch
# succeeded. For a benchmark host fetching PUBLIC files, skipping verification is safe and removes
# this confound. Off unless SWE_INSECURE_FETCH=1.
if _os.environ.get("SWE_INSECURE_FETCH") == "1":
    try:
        import ssl as _ssl
        _ssl._create_default_https_context = _ssl._create_unverified_context
    except Exception:
        pass
    try:
        import requests as _rq
        _orig_req = _rq.Session.request
        def _req_noverify(self, *a, **k):
            k.setdefault("verify", False)
            return _orig_req(self, *a, **k)
        _rq.Session.request = _req_noverify
        try:
            import urllib3 as _u3
            _u3.disable_warnings()
        except Exception:
            pass
    except Exception:
        pass
