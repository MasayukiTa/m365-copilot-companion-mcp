# -*- coding: utf-8 -*-
"""INST-09: pip verifies PyPI's certificate with this machine's exported roots; --trusted-host
(certificate checking OFF) only when the operator explicitly asks for it.

Driven, not read: the REAL step_install_deps runs `python -m pip ...` as a real child process,
and `pip` is a stub package placed first on PYTHONPATH that records its argv and the TLS
environment it was given, then exits with a chosen code and output.
"""
from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))

import bootstrap as B  # noqa: E402

_TLS_VARS = ("PIP_CERT", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE",
             B.INSECURE_PIP_ENV)
_PEM = "# Subject: CN=stub\n-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"


@pytest.fixture
def stub(tmp_path, monkeypatch):
    """A fake repo root, a stub `pip` package, and the install step's other children stubbed."""
    root = tmp_path / "repo"
    (root / ".setup").mkdir(parents=True)
    req = root / "requirements.txt"
    req.write_text("somepkg>=1\n", encoding="utf-8")
    monkeypatch.setattr(B, "ROOT", root)
    monkeypatch.setattr(B, "REQUIREMENTS", req)
    monkeypatch.setattr(B, "CA_BUNDLE", root / ".setup" / "ca-bundle.pem")
    monkeypatch.setattr(B, "TRANSCRIPT", tmp_path / "bootstrap.log")
    monkeypatch.setattr(B, "venv_python", lambda: Path(sys.executable))
    monkeypatch.setattr(B, "_broken_distributions", lambda py: [])
    monkeypatch.setattr(B, "_import_main_in_venv", lambda for_install=False: (42, ""))
    for v in _TLS_VARS:
        monkeypatch.delenv(v, raising=False)

    shim = tmp_path / "shim"
    (shim / "pip").mkdir(parents=True)
    record = tmp_path / "pip_calls.jsonl"
    (shim / "pip" / "__init__.py").write_text("", encoding="utf-8")
    (shim / "pip" / "__main__.py").write_text(textwrap.dedent("""
        import json, os, sys
        with open(os.environ["STUB_PIP_RECORD"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"argv": sys.argv[1:], "env": {k: os.environ.get(k) for k in %r}}) + "\\n")
        sys.stdout.write(os.environ.get("STUB_PIP_OUTPUT", ""))
        sys.exit(int(os.environ.get("STUB_PIP_RC", "0")))
    """ % (list(_TLS_VARS),)), encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(shim))
    monkeypatch.setenv("STUB_PIP_RECORD", str(record))
    monkeypatch.delenv("STUB_PIP_RC", raising=False)
    monkeypatch.delenv("STUB_PIP_OUTPUT", raising=False)

    def run(**kw):
        state = {"done": {}}
        B.step_install_deps(state=state, state_file=root / ".setup" / "state.json", **kw)
        return state

    def calls():
        if not record.exists():
            return []
        return [json.loads(l) for l in record.read_text(encoding="utf-8").splitlines() if l]

    return root, run, calls


def _no_trusted_host(argv):
    return "--trusted-host" not in argv


def test_the_stub_really_is_what_runs(stub):
    root, run, calls = stub
    run(upgrade_pip=False)
    got = calls()
    assert len(got) == 1 and got[0]["argv"][0] == "install"
    assert got[0]["argv"][-2:] == ["-r", str(root / "requirements.txt")]


def test_the_exported_machine_bundle_is_passed_and_verification_stays_on(stub, capsys):
    root, run, calls = stub
    bundle = root / ".setup" / "ca-bundle.pem"
    bundle.write_text(_PEM, encoding="utf-8")
    run()                                       # pip self-upgrade + -r requirements
    got = calls()
    assert len(got) == 2
    for c in got:
        argv = c["argv"]
        assert _no_trusted_host(argv), argv
        assert argv[argv.index("--cert") + 1] == str(bundle), argv
    assert "pip TLS: verified against %s (ca_bundle.ps1)" % bundle in capsys.readouterr().out


def test_an_operators_single_root_ssl_cert_file_loses_to_the_machine_bundle(stub, monkeypatch):
    """This machine's real shape: SSL_CERT_FILE is one corporate root kept for another tool."""
    root, run, calls = stub
    bundle = root / ".setup" / "ca-bundle.pem"
    bundle.write_text(_PEM, encoding="utf-8")
    single = root / "one-root.pem"
    single.write_text(_PEM, encoding="utf-8")
    monkeypatch.setenv("SSL_CERT_FILE", str(single))
    run(upgrade_pip=False)
    c = calls()[0]
    assert c["argv"][c["argv"].index("--cert") + 1] == str(bundle)
    assert c["env"]["SSL_CERT_FILE"] == str(single)          # not rewritten for anyone else


def test_pip_cert_wins_and_a_der_or_missing_file_is_never_handed_to_pip(stub, monkeypatch):
    root, run, calls = stub
    der = root / "corp.cer"
    der.write_bytes(b"\x30\x82\x01\x0a" + b"\x00" * 64)       # DER, not PEM
    monkeypatch.setenv("PIP_CERT", str(root / "does-not-exist.pem"))
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(der))
    pem = root / "curl.pem"
    pem.write_text(_PEM, encoding="utf-8")
    monkeypatch.setenv("CURL_CA_BUNDLE", str(pem))
    run(upgrade_pip=False)
    argv = calls()[0]["argv"]
    assert argv[argv.index("--cert") + 1] == str(pem) and _no_trusted_host(argv)

    good = root / "pip.pem"
    good.write_text(_PEM, encoding="utf-8")
    monkeypatch.setenv("PIP_CERT", str(good))
    (root / ".setup" / "ca-bundle.pem").write_text(_PEM, encoding="utf-8")
    run(upgrade_pip=False)
    argv = calls()[1]["argv"]
    assert argv[argv.index("--cert") + 1] == str(good)


def test_no_bundle_means_pip_default_verification_not_disabled_verification(stub, capsys):
    root, run, calls = stub
    run(upgrade_pip=False)
    argv = calls()[0]["argv"]
    assert "--cert" not in argv and _no_trusted_host(argv), argv
    assert "Windows certificate store" in capsys.readouterr().out


def test_trusted_host_only_on_explicit_opt_in_and_it_is_said(stub, monkeypatch, capsys):
    root, run, calls = stub
    (root / ".setup" / "ca-bundle.pem").write_text(_PEM, encoding="utf-8")
    monkeypatch.setenv(B.INSECURE_PIP_ENV, "1")
    run(upgrade_pip=False)
    argv = calls()[0]["argv"]
    assert argv.count("--trusted-host") == 3 and "--cert" not in argv
    out = capsys.readouterr().out
    assert "certificate checking is OFF" in out and B.INSECURE_PIP_ENV in out


def test_a_certificate_failure_is_not_retried_insecurely_and_says_what_to_do(stub, monkeypatch,
                                                                            tmp_path):
    root, run, calls = stub
    (root / ".setup" / "ca-bundle.pem").write_text(_PEM, encoding="utf-8")
    monkeypatch.setenv("STUB_PIP_RC", "1")
    monkeypatch.setenv("STUB_PIP_OUTPUT",
                       "WARNING: Retrying ... SSLError(SSLCertVerificationError(1, '[SSL: "
                       "CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get "
                       "local issuer certificate'))\nERROR: No matching distribution found "
                       "for somepkg>=1\n")
    with pytest.raises(B.StepError) as ei:
        run(upgrade_pip=False, pip_log=tmp_path / "pip.log")
    assert len(calls()) == 1 and _no_trusted_host(calls()[0]["argv"])   # no insecure retry
    msg = str(ei.value)
    assert "ca-extra.pem" in msg and B.INSECURE_PIP_ENV in msg and "証明書" in msg


def test_a_non_tls_failure_keeps_the_ordinary_message(stub, monkeypatch, tmp_path):
    root, run, calls = stub
    monkeypatch.setenv("STUB_PIP_RC", "1")
    monkeypatch.setenv("STUB_PIP_OUTPUT", "ERROR: No matching distribution found for somepkg>=1\n")
    with pytest.raises(B.StepError) as ei:
        run(upgrade_pip=False, pip_log=tmp_path / "pip.log")
    assert "ca-extra.pem" not in str(ei.value)
    assert "No matching distribution" in str(ei.value)
