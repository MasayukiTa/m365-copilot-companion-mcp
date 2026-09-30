# -*- coding: utf-8 -*-
"""bench/remote_grade.py with a fake transport: no network, no host, no secrets."""
import json
import os

import pytest

from bench import remote_grade as R

INST = "astropy__astropy-12907"
HOST = "SECRET-HOST-ALIAS-42"


class Fake:
    """Stands in for ssh/scp. `verdict_file` is what the remote verdict file would contain."""

    def __init__(self, verdict_file="VERDICT=RESOLVED\nRUNNER_DONE\n", ps_out=None,
                 scp_to_ok=True, raise_on=None, stage_ok=True):
        self.verdict_file = verdict_file
        self.ps_out = ps_out
        self.scp_to_ok = scp_to_ok
        self.raise_on = raise_on
        self.stage_ok = stage_ok
        self.calls = []

    def ps(self, script, timeout):
        self.calls.append(("ps", script))
        if self.raise_on == "ps":
            raise R.TransportError("unreachable", "connection refused to " + HOST)
        if "New-Item" in script:
            return 0, "ok\n" if self.stage_ok else "ssh: connect failed to " + HOST
        if self.ps_out is not None:
            return 0, self.ps_out
        return 0, "HELD_DONE"

    def scp_to(self, local, remote, timeout=120):
        self.calls.append(("scp_to", remote))
        if self.raise_on == "scp_to":
            raise R.TransportError("timeout")
        return self.scp_to_ok

    def scp_from(self, remote, local, timeout=120):
        self.calls.append(("scp_from", remote))
        if self.verdict_file is None:
            return False
        with open(local, "w", encoding="utf-8") as f:
            f.write(self.verdict_file)
        return True


@pytest.fixture
def patch(tmp_path):
    p = tmp_path / "x.patch"
    p.write_text("diff --git a/a b/a\n", encoding="utf-8")
    return str(p)


def _grade(patch, fake, **kw):
    return R.grade(INST, patch, fake, host=HOST, **kw)


def test_resolved(patch):
    r = _grade(patch, Fake())
    assert (r.resolved, r.verdict, r.error_class, r.infra_reason) == (True, "RESOLVED", "none", "")


def test_graded_fail_is_a_measurement_not_infra(patch):
    r = _grade(patch, Fake(verdict_file="VERDICT=not\nRUNNER_DONE\n"))
    assert (r.resolved, r.error_class) == (False, "graded-fail")


def test_evalerr_is_infra_not_a_failed_patch(patch):
    r = _grade(patch, Fake(verdict_file="VERDICT=EVALERR\nRUNNER_DONE\n"))
    assert (r.resolved, r.error_class, r.infra_reason) == (False, "infra", "evalerr")


def test_timeout_reported_by_host_side_hold(patch):
    r = _grade(patch, Fake(ps_out="HELD_TIMEOUT"), timeout=200)
    assert (r.error_class, r.infra_reason) == ("infra", "timeout")


def test_timeout_raised_by_transport(patch):
    r = _grade(patch, Fake(raise_on="scp_to"))
    assert (r.error_class, r.infra_reason) == ("infra", "timeout")


def test_unreachable_host_transport_error(patch):
    r = _grade(patch, Fake(raise_on="ps"))
    assert (r.resolved, r.error_class, r.infra_reason) == (False, "infra", "unreachable")


def test_unreachable_host_no_answer_to_stage(patch):
    r = _grade(patch, Fake(stage_ok=False))
    assert (r.error_class, r.infra_reason) == ("infra", "unreachable")


def test_upload_failure(patch):
    r = _grade(patch, Fake(scp_to_ok=False))
    assert (r.error_class, r.infra_reason) == ("infra", "scp_failed")


@pytest.mark.parametrize("content", [None, "", "VERDICT=RESOLVED\n", "garbage RUNNER_DONE\n",
                                     "VERDICT=??\nRUNNER_DONE\n"])
def test_malformed_output_is_infra_never_resolved(patch, content):
    r = _grade(patch, Fake(verdict_file=content))
    assert (r.resolved, r.error_class, r.infra_reason) == (False, "infra", "malformed")


def test_bad_inputs_send_nothing(tmp_path):
    f = Fake()
    assert R.grade("nonsense", str(tmp_path / "nope"), f, host=HOST).infra_reason == "bad_input"
    assert R.grade(INST, str(tmp_path / "nope"), f, host=HOST).infra_reason == "bad_input"
    assert f.calls == []


def test_each_grade_uses_a_fresh_run_id(patch):
    a, b = Fake(), Fake()
    _grade(patch, a)
    _grade(patch, b)
    ra = [c[1] for c in a.calls if c[0] == "scp_from"][0]
    rb = [c[1] for c in b.calls if c[0] == "scp_from"][0]
    assert ra != rb          # a stale verdict file can never be mistaken for this run's


def test_secret_host_never_in_result_or_output(patch, capsys, monkeypatch):
    for fake in (Fake(raise_on="ps"), Fake(stage_ok=False), Fake(ps_out="HELD_DONE " + HOST,
                                                                verdict_file=None)):
        r = _grade(patch, fake)
        assert HOST not in r.to_json()
    monkeypatch.setenv("SWE_EVAL_HOST", HOST)
    monkeypatch.delenv("EVAL_SSH_HOST", raising=False)
    monkeypatch.setattr(R, "Transport", lambda host: Fake(raise_on="ps"))
    R.main([INST, patch, "--json"])
    cap = capsys.readouterr()
    assert HOST not in cap.out + cap.err


def test_scrub_redacts_credential_shapes():
    s = R._scrub("password=hunter2 token: abcd API_KEY=zzz", "")
    assert "hunter2" not in s and "abcd" not in s and "zzz" not in s


def test_no_host_configured_is_reported_without_sending(monkeypatch, patch, capsys):
    monkeypatch.delenv("EVAL_SSH_HOST", raising=False)
    monkeypatch.delenv("SWE_EVAL_HOST", raising=False)
    monkeypatch.setattr(R, "configured_host", lambda: "")
    rc = R.main([INST, patch, "--json"])
    d = json.loads(capsys.readouterr().out)
    assert rc == 2 and d["infra_reason"] == "not_configured"


def test_cli_exit_codes(monkeypatch, patch):
    monkeypatch.setattr(R, "configured_host", lambda: HOST)
    for content, code in (("VERDICT=RESOLVED\nRUNNER_DONE\n", 0), ("VERDICT=not\nRUNNER_DONE\n", 1),
                          (None, 2)):
        monkeypatch.setattr(R, "Transport", lambda host, c=content: Fake(verdict_file=c))
        assert R.main([INST, patch]) == code


def test_source_holds_no_hostnames_or_secrets():
    src = open(R.__file__, encoding="utf-8").read()
    for bad in ("shuttle-scope", "kiyus", "M118", "resonac", "password="):
        assert bad not in src.replace("password=<redacted>", "")


def test_real_transport_maps_process_errors(monkeypatch):
    import subprocess
    import types

    def boom(argv, timeout=None):
        raise subprocess.TimeoutExpired(argv, timeout)
    monkeypatch.setattr(R, "_child_run", boom)
    with pytest.raises(R.TransportError) as ei:
        R.Transport(HOST).ps("x", 1)
    assert ei.value.kind == "timeout"

    ok = types.SimpleNamespace(returncode=0, stdout="ok\x00\n", stderr="")
    monkeypatch.setattr(R, "_child_run", lambda argv, timeout=None: ok)
    rc, out = R.Transport(HOST).ps("x", 1)
    assert rc == 0 and "ok" in out and "\x00" not in out
