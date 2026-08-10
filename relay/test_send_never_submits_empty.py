"""An empty composer must never be submitted.

Measured on 2026-08-10: two of five sends reached the agent as an empty turn. The replies
were "I notice your message appears to be empty" and a bare capability list -- which a
judge reading reply text scores as a refusal, so the defect masquerades as the agent being
over-constrained. The cause is the last-ditch `Enter`: it fires precisely when the Send
button never armed, and the commonest reason it never arms is that the text failed to land
in the composer at all. Pressing Enter in that state submits an empty turn.
"""
import re
from pathlib import Path

SRC = Path(__file__).with_name("copilot_autopilot_relay.py").read_text(encoding="utf-8")


def _send_block() -> str:
    i = SRC.index("Type -> wait for Send to ARM")
    return SRC[i:i + 3000]


def test_enter_fallback_is_guarded_by_composer_content():
    """The Enter fallback must be unreachable while the composer is empty."""
    block = _send_block()
    guard = re.search(r"elif not self\._composer_text\(\):", block)
    assert guard, "空の入力欄で Enter に落ちる経路が塞がれていない"
    enter = block.index('self.page.keyboard.press("Enter")')
    assert guard.start() < enter, "ガードは Enter フォールバックより前になければならない"


def test_empty_composer_branch_retries_instead_of_submitting():
    """The empty branch must continue to the next attempt, not submit anything."""
    block = _send_block()
    i = block.index("elif not self._composer_text():")
    branch = block[i:block.index("else:", i)]
    assert "continue" in branch, "空だったとき次の試行へ回していない"
    assert "press(" not in branch, "空の入力欄に対してキー送信している"
    assert "click(" not in branch, "空の入力欄に対して送信クリックしている"
