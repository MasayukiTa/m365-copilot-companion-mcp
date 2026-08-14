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
    # To the end of the type/arm/click/clear attempt, not a fixed byte count: the block
    # grew past 3000 characters when the composer-verification loop was added, and the
    # tests started passing vacuously against a truncated window.
    end = SRC.index("for i in range(48)", i)
    return SRC[i:end + 400]


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


def test_the_text_is_verified_to_have_landed_before_the_send():
    """新規会話直後は、DOM に入っても SPA の編集モデルに入っていないことがある。

    実測 2026-08-14: 合図を返させる依頼を12回投げ、4回が空で届いた。内訳は
    毎回 /new が 4/8、会話使い回しが 0/8 で、原因は新規会話の初期化競合。
    このときは Send が「有効化される」ため、有効化を待つだけの経路では気づけない。
    """
    block = _send_block()
    # Assert on the statements, not on a byte window after the first insert. The
    # explanation above the loop is long, and a fixed-size window stopped covering the
    # very code it was meant to protect -- a test that passes because it is looking at
    # comments is worse than no test.
    assert block.count("insert_text(one_line)") >= 2, "空だったときに再投入していない"
    assert block.index("if self._composer_text():") > block.index("insert_text(one_line)"), \
        "投入する前に照合している"


def test_the_reinsert_loop_is_bounded():
    block = _send_block()
    assert "for _settle in range(" in block, "無制限に再投入しうる"
