# -*- coding: utf-8 -*-
"""Load the local, gitignored scan profile.

EVERY site-specific name (ODBC connection name, table/view, lot column, product
column, analysis-id examples, etc.) lives in ``.config/scan_profile.json`` under
the repository root. That path is gitignored (see ``.gitignore``: ``.config/``)
so the profile never reaches the public repository. A documented FAKE example is
in ``docs/CONFIG.md``.

This module hardcodes no business value. If the profile is absent, callers that
need a value get a clear error rather than a silent default that happens to name
a real system.

The profile shape (all keys optional unless a caller requires them):

    {
      "connection":      "<odbc connection name>",
      "lot_table":       "<schema.table or view>",
      "unit_column":     "<column counted DISTINCT as a lot>",
      "product_column":  "<stratification column>",
      "ledger_path":     "<path to the backlog ledger json>"
    }

The connection STRING, server, credentials and env-var names are NOT here; they
stay in the ODBC/.env layer that the injected query callable owns. This file
holds only logical names.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional

# Default profile location, relative to the repository root (…/scripts/periodic_scan
# -> parents[2] is the repo root). Override with the env var below or an explicit
# path argument.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PROFILE = _REPO_ROOT / ".config" / "scan_profile.json"
_ENV_VAR = "PERIODIC_SCAN_PROFILE"


class ProfileNotFound(FileNotFoundError):
    """Raised when a required profile value is requested but no profile exists."""


class ProfileKeyMissing(KeyError):
    """Raised when the profile exists but lacks a required key."""


def profile_path(explicit: Optional[str] = None) -> Path:
    """Resolve the profile path: explicit arg > env var > default location."""
    if explicit:
        return Path(explicit)
    env = os.environ.get(_ENV_VAR)
    if env:
        return Path(env)
    return _DEFAULT_PROFILE


def load_profile(explicit: Optional[str] = None) -> dict[str, Any]:
    """Load the scan profile as a dict. Returns {} when the file is absent.

    Absence is not an error here (a dry-run needs no profile); it becomes an
    error only when a caller asks for a required value via ``require``.
    """
    p = profile_path(explicit)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("scan profile must be a JSON object")
    return data


def get(profile: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """Read an optional key from an already-loaded profile."""
    return profile.get(key, default)


__all__ = [
    "ProfileNotFound",
    "ProfileKeyMissing",
    "profile_path",
    "load_profile",
    "get",
]
