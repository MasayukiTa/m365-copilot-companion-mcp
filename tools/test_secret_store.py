"""What the unlock password reader says when it cannot read one.

Split from test_security because the two fail differently: the gate decides who may call, this
decides whether there is anything to compare against, and conflating them once hid a DPAPI
fault behind a message about an unset variable.
"""
# ── a failure to decrypt is not an absence ────────────────────────────────────────────────────

def test_an_unset_password_is_reported_as_unset():
    from tools import secret_store as S
    assert S.unlock_password_from_env({}) == ""
    assert S.unlock_password_problem() == S.PROBLEM_UNSET


def test_a_blob_this_account_cannot_open_is_not_reported_as_unset():
    """THE DEFECT THIS PAIR EXISTS FOR. A DPAPI value is bound to one Windows user on one
    machine. An .env carried to a new PC holds a blob this account cannot open; the except
    swallowed that, "" came back, and unlock() said "MCP_UNLOCK_PASSWORD is not configured" --
    naming the variable that was ABSENT while the one that was PRESENT sat there undecryptable.
    Startup is unaffected, so the stack looks healthy until the first mutating tool."""
    from tools import secret_store as S
    got = S.unlock_password_from_env({S.UNLOCK_PASSWORD_PROTECTED_VAR: "dpapi:AAAAnotarealblob=="})
    assert got == "", "an undecryptable value must still read as no password"
    assert S.unlock_password_problem() == S.PROBLEM_UNDECRYPTABLE, (
        "an undecryptable blob is being reported as simply unset")


def test_a_readable_password_clears_the_problem():
    """The state must not latch. A machine that was mis-set and then fixed would otherwise keep
    explaining a fault it no longer has."""
    from tools import secret_store as S
    S.unlock_password_from_env({S.UNLOCK_PASSWORD_PROTECTED_VAR: "dpapi:AAAAnotarealblob=="})
    assert S.unlock_password_problem() == S.PROBLEM_UNDECRYPTABLE
    assert S.unlock_password_from_env({S.UNLOCK_PASSWORD_VAR: "plain"}) == "plain"
    assert S.unlock_password_problem() == ""


def test_the_diagnosis_never_reaches_stdout(capsys):
    """This module is imported by processes whose stdout is parsed as data, and by the MCP
    server's stdio transport where a stray line breaks the protocol. The explanation goes to a
    logger, so stdout stays empty."""
    from tools import secret_store as S
    S.unlock_password_from_env({S.UNLOCK_PASSWORD_PROTECTED_VAR: "dpapi:AAAAnotarealblob=="})
    assert capsys.readouterr().out == "", "the diagnosis was printed onto the data channel"
