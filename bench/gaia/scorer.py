"""
bench/gaia/scorer.py
--------------------
Official GAIA question scorer, ported from the GAIA leaderboard evaluation code.

Source: https://huggingface.co/spaces/gaia-benchmark/leaderboard
        (HuggingFace Space, file: scorer.py)
        Published under Apache-2.0 alongside the GAIA dataset.

The scorer normalises both prediction and ground-truth with the same pipeline,
then returns True iff they match exactly after normalisation.

Normalisation steps (exactly matching the leaderboard):
  1. Strip leading/trailing whitespace.
  2. If parseable as a number, convert to canonical float string (no trailing .0).
  3. Otherwise: lowercase, remove articles (a/an/the), remove punctuation,
     collapse whitespace.

Usage:
    from bench.gaia.scorer import question_scorer
    correct = question_scorer(prediction="42", ground_truth="42")
"""

import re
import string
import unicodedata


# ---------------------------------------------------------------------------
# Number normalisation helpers (from official GAIA scorer)
# ---------------------------------------------------------------------------

def _is_number(text: str) -> bool:
    """Return True if text represents a numeric value."""
    try:
        float(text.replace(",", ""))
        return True
    except ValueError:
        return False


def _normalise_number(text: str) -> str:
    """Normalise a numeric string to a canonical form.

    Removes commas (thousands separators), parses as float, then returns
    int-string if no fractional part (e.g. '42.0' -> '42'), else float-string.
    """
    text = text.replace(",", "")
    val = float(text)
    # Drop .0 suffix for whole numbers
    if val == int(val):
        return str(int(val))
    return str(val)


# ---------------------------------------------------------------------------
# String normalisation helpers (from official GAIA scorer)
# ---------------------------------------------------------------------------

_ARTICLES_RE = re.compile(r"\b(a|an|the)\b", re.IGNORECASE)
_PUNCT_TABLE = str.maketrans("", "", string.punctuation)
_WHITESPACE_RE = re.compile(r"\s+")


def _normalise_string(text: str) -> str:
    """Lowercase, strip articles, remove punctuation, collapse whitespace."""
    text = text.lower()
    text = _ARTICLES_RE.sub(" ", text)
    text = text.translate(_PUNCT_TABLE)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Top-level scorer (public API)
# ---------------------------------------------------------------------------

def normalise_answer(text: str) -> str:
    """Apply the official GAIA normalisation pipeline to a single answer string."""
    text = text.strip()
    if _is_number(text):
        return _normalise_number(text)
    return _normalise_string(text)


def question_scorer(prediction: str, ground_truth: str) -> bool:
    """Official GAIA binary scorer.

    Returns True iff normalise_answer(prediction) == normalise_answer(ground_truth).

    This is the exact scoring function used by the GAIA leaderboard.
    Both strings are normalised identically before comparison, so whitespace,
    capitalisation, articles (a/an/the), punctuation, and number formatting
    differences are ignored.
    """
    return normalise_answer(prediction) == normalise_answer(ground_truth)
