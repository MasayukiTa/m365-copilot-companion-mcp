"""Generic relationship discovery for SQL sources (read-only, sampled, budgeted)."""

from .candidates import Candidate, generate_candidates, name_tokens
from .profile import ColumnProfile, ProfileConfig, profile_source, shape_of
from .source import OdbcSource, SqliteSource, open_source
from .store import DiscoveryStore
from .verify import PairBudget, VerifyResult, verify_pair

__all__ = [
    "Candidate", "ColumnProfile", "DiscoveryStore", "OdbcSource", "PairBudget",
    "ProfileConfig", "SqliteSource", "VerifyResult", "generate_candidates", "name_tokens",
    "open_source", "profile_source", "shape_of", "verify_pair",
]
