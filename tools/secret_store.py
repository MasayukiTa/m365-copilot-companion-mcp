"""Local secret helpers for optional Windows DPAPI-protected .env values."""
from __future__ import annotations

import base64
import ctypes
import os
from ctypes import wintypes

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


def unlock_password_from_env(environ=None) -> str:
    """Read the unlock password from a plain legacy or protected env value."""
    env = environ if environ is not None else os.environ
    plain = (env.get(UNLOCK_PASSWORD_VAR) or "").strip()
    if plain:
        return plain
    protected = (env.get(UNLOCK_PASSWORD_PROTECTED_VAR) or "").strip()
    if not protected:
        return ""
    try:
        return unprotect_secret(protected)
    except Exception:
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
