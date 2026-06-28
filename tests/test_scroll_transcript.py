"""Hermetic unit tests for the scroll-and-accumulate transcript scraper.

Tests the pure accumulate_messages() / _msg_key() logic in bridge/copilot_bridge.py
WITHOUT a live browser (no Playwright, no Edge, no CDP).

Scenarios covered:
  (a) All messages collected despite only a sliding window being "visible" per step.
  (b) Dedup: a message seen at multiple scroll positions appears exactly once.
  (c) Order preserved: messages come out in top-to-bottom reading order.
  (d) Bound hit -> truncated=True marker returned, captured count reflects actual count.
  (e) No-progress convergence: early stop when consecutive windows are all duplicates.
  (f) Empty input -> empty output, not truncated.
  (g) scrape_full_transcript() fallback: when the page mock has no turns in the DOM
      the function returns ([], False) without raising.

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_scroll_transcript.py -v
or:
    .venv\\Scripts\\python.exe tests/test_scroll_transcript.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# ── bootstrap sys.path ─────────────────────────────────────────────────────────
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from bridge.copilot_bridge import accumulate_messages, _msg_key, scrape_full_transcript


# ── helpers ────────────────────────────────────────────────────────────────────

def _msg(role: str, text: str, dom_idx: int = 0) -> dict:
    return {"role": role, "text": text, "dom_idx": dom_idx}


def _window(*msgs: dict) -> list[dict]:
    """Shortcut: build a window (list of msgs)."""
    return list(msgs)


# ── Test (a): all messages collected across non-overlapping windows ─────────────

def test_full_collection_non_overlapping():
    """Ten messages split across 5 windows of 2 -- all 10 must appear in output."""
    all_msgs = [_msg("user", f"user message {i}", dom_idx=i*2)
                for i in range(5)]
    all_msgs += [_msg("assistant", f"reply {i}", dom_idx=i*2+1)
                 for i in range(5)]
    # Sort by dom_idx to give reading order
    all_msgs.sort(key=lambda m: m["dom_idx"])

    # Split into windows of 2
    windows = [all_msgs[i:i+2] for i in range(0, len(all_msgs), 2)]

    msgs, truncated = accumulate_messages(windows)
    assert not truncated, "should NOT be truncated for 5 windows within max_steps"
    assert len(msgs) == 10, f"expected 10 messages, got {len(msgs)}: {msgs}"


# ── Test (b): deduplicate -- message seen in 3 consecutive windows appears once ─

def test_dedup_overlapping_windows():
    """Simulate a sliding window where each scroll step overlaps the previous by 1.
    A message in the overlap zone must appear exactly once."""
    m0 = _msg("user", "first user message", dom_idx=0)
    m1 = _msg("assistant", "first assistant reply", dom_idx=1)
    m2 = _msg("user", "second user message", dom_idx=2)
    m3 = _msg("assistant", "second assistant reply", dom_idx=3)

    # Overlap: m1 appears in windows 0, 1, and 2
    windows = [
        _window(m0, m1),
        _window(m1, m2),
        _window(m2, m3),
    ]

    msgs, truncated = accumulate_messages(windows)
    assert not truncated
    texts = [m["text"] for m in msgs]
    assert texts.count("first assistant reply") == 1, "dedup failed: message appeared multiple times"
    assert len(msgs) == 4, f"expected 4 unique messages, got {len(msgs)}: {msgs}"


# ── Test (c): order preserved (top-to-bottom reading order) ──────────────────

def test_order_preserved():
    """Messages must come out in reading order even though a sliding window exposes
    them in mixed DOM positions."""
    # 6 messages in reading order, dom_idx 0..5
    ordered = [
        _msg("user", "Q1", dom_idx=0),
        _msg("assistant", "A1", dom_idx=1),
        _msg("user", "Q2", dom_idx=2),
        _msg("assistant", "A2", dom_idx=3),
        _msg("user", "Q3", dom_idx=4),
        _msg("assistant", "A3", dom_idx=5),
    ]
    # Windows: top 3, middle 3 (overlap), bottom 3 (overlap)
    windows = [
        ordered[0:3],
        ordered[2:5],
        ordered[3:6],
    ]
    msgs, truncated = accumulate_messages(windows)
    assert not truncated
    assert len(msgs) == 6
    expected_texts = ["Q1", "A1", "Q2", "A2", "Q3", "A3"]
    actual_texts = [m["text"] for m in msgs]
    assert actual_texts == expected_texts, (
        f"Order mismatch:\n  expected: {expected_texts}\n  actual:   {actual_texts}"
    )


# ── Test (d): bound hit -> truncated=True, captured count is accurate ──────────

def test_max_steps_bound_triggers_truncated():
    """Passing more windows than max_steps must return truncated=True."""
    # 20 windows each with a unique message
    windows = [[_msg("user", f"message {i}", dom_idx=i)] for i in range(20)]

    msgs, truncated = accumulate_messages(windows, max_steps=5)
    assert truncated, "expected truncated=True when max_steps exceeded"
    # Only the first 5 windows' messages should be captured
    assert len(msgs) == 5, f"expected 5 messages (first 5 steps), got {len(msgs)}"


# ── Test (e): no-progress convergence -- early stop when all duplicate ─────────

def test_no_progress_early_stop():
    """If N consecutive windows yield zero new messages, accumulate_messages stops early
    WITHOUT setting truncated=True (it converged naturally)."""
    m0 = _msg("user", "the only message", dom_idx=0)
    # First window has the message; all subsequent windows repeat it
    windows = [[m0]] * 20  # 20 windows all with the same message

    msgs, truncated = accumulate_messages(windows, no_progress_limit=5)
    # Should stop after the first window + 5 no-progress steps = 6 windows total.
    # truncated depends on whether we exhaust max_steps; since we stop early via
    # no_progress, truncated should be False.
    assert not truncated, "no-progress early stop should NOT set truncated"
    assert len(msgs) == 1, f"expected 1 unique message, got {len(msgs)}"


# ── Test (f): empty input ──────────────────────────────────────────────────────

def test_empty_input():
    msgs, truncated = accumulate_messages([])
    assert msgs == []
    assert not truncated


# ── Test (g): _msg_key deduplication correctness ──────────────────────────────

def test_msg_key_same_text_same_key():
    k1 = _msg_key("user", "hello world")
    k2 = _msg_key("user", "hello world")
    assert k1 == k2


def test_msg_key_role_differentiates():
    k_user = _msg_key("user", "same text")
    k_asst = _msg_key("assistant", "same text")
    assert k_user != k_asst, "different roles with same text must have different keys"


def test_msg_key_prefix_80_chars_stability():
    """A message seen twice -- once truncated at 80 chars, once in full -- should
    produce the SAME key (because _msg_key hashes only the first 80 chars)."""
    base = "x" * 80
    full_text = base + " extra content that wasn't captured in the first pass"
    k_trunc = _msg_key("assistant", base)
    k_full = _msg_key("assistant", full_text)
    assert k_trunc == k_full, (
        "truncated and full capture of same message should share a key "
        "(first-80-chars hashing)"
    )


# ── Test (h): scrape_full_transcript fallback on a page with no turns ──────────

class _FakePageNullDOM:
    """Simulates a Playwright page where the conversation container has no turn blocks."""

    def __init__(self):
        self._scroll = 0

    def evaluate(self, js: str, arg: Any = None) -> Any:
        # _SCROLL_TO_JS
        if "container.scrollTop = scrollTop" in js:
            self._scroll = int(arg or 0)
            return self._scroll
        # _SCROLL_STEP_JS -- return empty msgs and a trivially short container
        if "scrollHeight" in js:
            return {
                "scrollTop": 0,
                "scrollHeight": 100,
                "clientHeight": 200,   # clientHeight >= scrollHeight -> no scroll needed
                "atBottom": True,
                "msgs": [],
            }
        return None

    def wait_for_timeout(self, ms: int) -> None:
        pass  # no-op in tests


def test_scrape_full_transcript_null_dom():
    """scrape_full_transcript on a page with no turn blocks returns ([], False) gracefully."""
    page = _FakePageNullDOM()
    msgs, truncated = scrape_full_transcript(page)
    assert msgs == [], f"expected empty list, got {msgs}"
    assert not truncated


# ── Test (i): scrape_full_transcript simulated virtualised DOM ─────────────────

class _FakePageVirtualised:
    """Simulates a Playwright page with a virtualised conversation.

    total_messages messages exist logically; only a window of window_size are
    rendered in the DOM at any one time (centred on the current scrollTop).
    """

    def __init__(self, total_messages: int = 30, window_size: int = 6):
        self._scroll_top = 0
        self._scroll_height = 3000
        self._client_height = 400
        self._all_msgs = self._build_msgs(total_messages)
        self._window_size = window_size
        # Each message occupies scroll_height/total_messages pixels of logical space.
        self._msg_height = self._scroll_height / total_messages

    @staticmethod
    def _build_msgs(n: int) -> list[dict]:
        out = []
        for i in range(n):
            role = "user" if i % 2 == 0 else "assistant"
            out.append({"role": role, "text": f"message {i}", "dom_idx": i})
        return out

    def _visible_msgs(self) -> list[dict]:
        """Msgs whose logical position overlaps the current viewport."""
        visible = []
        for i, m in enumerate(self._all_msgs):
            msg_top = i * self._msg_height
            msg_bot = msg_top + self._msg_height
            view_top = self._scroll_top
            view_bot = self._scroll_top + self._client_height
            if msg_bot > view_top and msg_top < view_bot:
                visible.append(m)
        return visible

    def evaluate(self, js: str, arg: Any = None) -> Any:
        if "container.scrollTop = scrollTop" in js:
            self._scroll_top = max(0, min(int(arg or 0),
                                          self._scroll_height - self._client_height))
            return self._scroll_top
        if "scrollHeight" in js:
            at_bottom = (self._scroll_top + self._client_height + 4) >= self._scroll_height
            return {
                "scrollTop": self._scroll_top,
                "scrollHeight": self._scroll_height,
                "clientHeight": self._client_height,
                "atBottom": at_bottom,
                "msgs": self._visible_msgs(),
            }
        return None

    def wait_for_timeout(self, ms: int) -> None:
        pass


def test_scrape_full_transcript_virtualised():
    """scrape_full_transcript must collect ALL 30 messages even though only ~6 are
    in the simulated DOM at any one time."""
    total = 30
    page = _FakePageVirtualised(total_messages=total, window_size=6)
    msgs, truncated = scrape_full_transcript(page)
    assert not truncated, "30 messages should not require hitting a safety bound"
    assert len(msgs) == total, (
        f"expected {total} messages, got {len(msgs)}: "
        f"{[m['text'] for m in msgs]}"
    )
    # Verify order: "message 0", "message 1", ... "message 29"
    expected_texts = [f"message {i}" for i in range(total)]
    actual_texts = [m["text"] for m in msgs]
    assert actual_texts == expected_texts, (
        f"Order wrong:\n  expected: {expected_texts[:5]}...\n"
        f"  actual:   {actual_texts[:5]}..."
    )


# ── standalone runner (no pytest required) ────────────────────────────────────

_ALL_TESTS = [
    test_full_collection_non_overlapping,
    test_dedup_overlapping_windows,
    test_order_preserved,
    test_max_steps_bound_triggers_truncated,
    test_no_progress_early_stop,
    test_empty_input,
    test_msg_key_same_text_same_key,
    test_msg_key_role_differentiates,
    test_msg_key_prefix_80_chars_stability,
    test_scrape_full_transcript_null_dom,
    test_scrape_full_transcript_virtualised,
]

if __name__ == "__main__":
    passed = failed = 0
    for fn in _ALL_TESTS:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as exc:
            print(f"  FAIL  {fn.__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
