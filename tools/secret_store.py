"""Local secret helpers for optional Windows DPAPI-protected .env values."""
from __future__ import annotations

import base64
import ctypes
import logging
import os
from ctypes import wintypes

_log = logging.getLogger(__name__)

UNLOCK_PASSWORD_VAR = "MCP_UNLOCK_PASSWORD"
UNLOCK_PASSWORD_PROTECTED_VAR = "MCP_UNLOCK_PASSWORD_PROTECTED"
_DPAPI_PREFIX = "dpapi:"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.c_void_p),
    ]


def _require_windows() -> None:
    if os.name != "nt":
        raise RuntimeError("DPAPI protection is only available on Windows")


def _blob_from_bytes(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buf = ctypes.create_string_buffer(data)
    blob = _DataBlob(len(data), ctypes.cast(buf, ctypes.c_void_p))
    return blob, buf


def _blob_to_bytes(blob: _DataBlob) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        if blob.pbData:
            local_free = ctypes.windll.kernel32.LocalFree
            local_free.argtypes = [ctypes.c_void_p]
            local_free.restype = ctypes.c_void_p
            local_free(ctypes.c_void_p(blob.pbData))


def protect_secret(value: str) -> str:
    """Return a Windows-user-bound DPAPI protected value for storage in .env."""
    _require_windows()
    crypt32 = ctypes.windll.crypt32
    plain_blob, _plain_buf = _blob_from_bytes(value.encode("utf-8"))
    protected_blob = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(plain_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(protected_blob),
    )
    if not ok:
        raise ctypes.WinError()
    protected = base64.b64encode(_blob_to_bytes(protected_blob)).decode("ascii")
    return _DPAPI_PREFIX + protected


def unprotect_secret(value: str) -> str:
    """Decrypt a value returned by protect_secret."""
    _require_windows()
    if not value.startswith(_DPAPI_PREFIX):
        raise ValueError("unsupported protected secret format")
    encrypted = base64.b64decode(value[len(_DPAPI_PREFIX):])
    encrypted_blob, _encrypted_buf = _blob_from_bytes(encrypted)
    plain_blob = _DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(encrypted_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(plain_blob),
    )
    if not ok:
        raise ctypes.WinError()
    return _blob_to_bytes(plain_blob).decode("utf-8")


#: Why the unlock password could not be read, or "" when there is nothing wrong.
#:
#: A FAILURE TO DECRYPT IS NOT AN ABSENCE, AND SAYING SO COST A DAY OF DIAGNOSIS.
#: The protected value is bound to ONE Windows user on ONE machine (protect_secret calls
#: CryptProtectData with no LOCAL_MACHINE flag), so an .env carried to a new PC -- or to a second
#: account on the same PC -- holds a blob this user cannot open. That raised, the except swallowed
#: it, "" came back, and unlock() reported "MCP_UNLOCK_PASSWORD is not configured": a message
#: naming the variable that ISN'T set while the one that IS set sat there undecryptable. Nothing
#: in the logs said DPAPI. Startup is unaffected, so the stack looks healthy right up to the first
#: mutating tool, and then every one of them is refused for a reason nobody can see.
#:
#: Module state rather than an exception because every caller treats "" as "not configured" and
#: changing that contract would rewrite the gate itself. The reason is recorded beside the answer
#: so a caller that wants to explain can, and one that does not is unaffected.
_LAST_PROBLEM = [""]

#: Diagnostic codes. Callers should test these rather than match the prose.
PROBLEM_UNSET = "unset"
PROBLEM_UNDECRYPTABLE = "undecryptable"


def unlock_password_problem() -> str:
    """The reason the last read failed: "", PROBLEM_UNSET or PROBLEM_UNDECRYPTABLE."""
    return _LAST_PROBLEM[0]


def unlock_password_from_env(environ=None) -> str:
    """Read the unlock password from a plain legacy or protected env value.

    Returns "" when it cannot be read; call unlock_password_problem() for WHY.
    """
    env = environ if environ is not None else os.environ
    plain = (env.get(UNLOCK_PASSWORD_VAR) or "").strip()
    if plain:
        _LAST_PROBLEM[0] = ""
        return plain
    protected = (env.get(UNLOCK_PASSWORD_PROTECTED_VAR) or "").strip()
    if not protected:
        _LAST_PROBLEM[0] = PROBLEM_UNSET
        return ""
    try:
        value = unprotect_secret(protected)
        _LAST_PROBLEM[0] = ""
        return value
    except Exception as exc:
        _LAST_PROBLEM[0] = PROBLEM_UNDECRYPTABLE
        # STDERR, NEVER STDOUT. This module is imported by processes whose stdout is parsed as
        # data and by the MCP server's stdio transport, where a stray line breaks the protocol.
        try:
            # THE EXCEPTION OBJECT DOES NOT GO IN. `exc` is raised while handling the
            # protected value, so its message can carry a fragment of that value -- and this
            # is the unlock password's own path. The TYPE carries the whole diagnostic
            # difference that matters here (an OSError is "wrong machine or account", a
            # binascii/ValueError is "the value is malformed"), so nothing is lost by leaving
            # the message out. Structural, not conditional: a runtime `if` around a log line
            # does not remove the flow, it only hides it from the reader.
            _log.warning(
                "%s is set but this Windows account cannot decrypt it (%s). DPAPI values are "
                "bound to one user on one machine, so an .env copied from another PC or account "
                "cannot be opened here. Re-run the setup on THIS machine to re-protect it.",
                UNLOCK_PASSWORD_PROTECTED_VAR, type(exc).__name__)
        except Exception:
            pass
        return ""


def unlock_password_local(environ=None) -> str:
    """The unlock password as a LOCAL process can see it: env first, then the repo .env.

    The relay carried its own copy of this env-then-dotenv fallback while the bridge
    carried none, which is why auto-unlock worked for fleet runs and never for the main
    chat. One implementation, so a caller cannot be quietly left out.

    Local only, by design: the password is read on this machine and injected into a turn,
    never written into the agent's persistent configuration where it would live forever.
    Returns '' when unset.
    """
    pw = unlock_password_from_env(environ)
    if pw:
        return pw
    try:
        from dotenv import load_dotenv

        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        load_dotenv(os.path.join(repo, ".env"))
    except Exception:
        return ""
    return unlock_password_from_env(environ)


# 記録に残してはいけない値。名前で拾う。解錠パスワードだけを伏せていた頃、
# エージェントが .env の中身を読み上げた回があり、API キーと HF トークンが
# そのまま転写ログに平文で残った（2026-08-06 実データで1件発見）。
# 秘密を1つずつ足していく形だと、次に増えた鍵をまた取りこぼす。
SECRET_NAME_HINTS = ("PASSWORD", "TOKEN", "SECRET", "API_KEY", "APIKEY", "CREDENTIAL")

# 短すぎる値は伏せない。"1" や "auto" のような設定値まで置換すると、本文が
# 読めなくなるうえ、伏字だらけで何が起きたのか追えなくなる。
_MIN_SECRET_LEN = 8


def secret_values(environ=None) -> list[str]:
    """伏せるべき値を集める。環境変数と .env の両方から、名前で選ぶ。"""
    env = dict(os.environ if environ is None else environ)
    try:
        from dotenv import dotenv_values

        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for k, v in (dotenv_values(os.path.join(repo, ".env")) or {}).items():
            if v and k not in env:
                env[k] = v
    except Exception:
        pass

    out: list[str] = []
    for name, value in env.items():
        if not value or len(value.strip()) < _MIN_SECRET_LEN:
            continue
        upper = name.upper()
        if any(hint in upper for hint in SECRET_NAME_HINTS):
            out.append(value.strip())
    # 長いものから消す。短い値が長い値の一部だったとき、先に短い方を消すと
    # 長い方が部分的に残る。
    return sorted(set(out), key=len, reverse=True)


#: What replaces a secret in anything written down. A CONSTANT because a test asserted
#: a marker -- "<redacted-unlock-password>" -- that no code has ever emitted, and it
#: sat failing where nobody ran it. The literal being in one place is what stops the
#: reader and the writer disagreeing about it again.
#:
#: MARKED, NOT DELETED. Removing the secret silently would leave a transcript that
#: reads as though nothing was there, and "no secret in this file" and "a secret was
#: taken out of this file" are different facts.
REDACTION_MARKER = "<redacted>"


def redact_secrets(text: str, environ=None) -> str:
    """本文から秘密を伏せる。書き出す直前にだけ使う。

    送る文には掛けないこと。解錠は本物のパスワードが相手に届いて初めて通る。
    掛けてよいのは「ファイルに書く瞬間」だけ。
    """
    value = text or ""
    try:
        for secret in secret_values(environ):
            if secret in value:
                value = value.replace(secret, REDACTION_MARKER)
    except Exception:
        pass
    return value
