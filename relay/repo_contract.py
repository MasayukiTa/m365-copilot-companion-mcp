"""repo_contract.py -- the shared エージェント契約 instruction block.

This is the single source of truth for the standing constraints that were being hand-
copied into every fleet goal (隠離 / 選択的 add / main への直接コミット禁止 / 新規テストの
CI 登録 / Windows 依存の禁止 / レビュー指摘は裏を取ってから). It is modelled directly on
coding_discipline.py: keep the shared block in one file and prepend it to the jobs it
applies to, rather than letting drifting copies accumulate in each goal string.

UNLIKE coding_discipline, which is only attached to a self-verifying (`checks`) goal,
this contract applies to EVERY fleet worker: the constraints are about not disturbing a
shared working tree and shared history, which is true of a read-only job as much as a
writing one. So it is composed in at the one point every worker passes through
(relay_fleet._with_repo_contract, called from the composed_goal line), not gated on a
verification card.

IT IS A PREFIX, LIKE THE MEMORY AND THE PROCEDURE. The composed body must END with the
bare goal by construction -- relay_fleet takes `_composed_prefix` by stripping the goal
off the suffix, and the replay / token-limit-recycle branches rebuild a fresh chat from
that prefix. Appending the contract AFTER the goal breaks that suffix invariant (and the
test that guards it), so the block below is emitted as a leading section that ends with a
separator, and _with_repo_contract prepends it.

MECHANISM IS NOT PRESCRIBED. Point (1) states the isolation REQUIREMENT -- do not change
what other runs can see until your work is done -- and deliberately does NOT say to use
`git worktree`. Whether isolation is needed, and how to get it, is the executing agent's
call: a read-only job needs none, so fixing the mechanism would both apply it where it is
not wanted and fail to apply it where it is.
"""
from __future__ import annotations

#: Emitted verbatim as the last line of the block so a reader (and _with_repo_contract's
#: idempotence guard) can see where the contract ends and the rest of the body begins.
CONTRACT_END = "--- 契約ここまで ---"


def repo_contract_text() -> str:
    """Return the standing エージェント契約 block (Japanese), as a leading section.

    The string ends with "...\\n\\n" so the caller can prepend it directly onto a goal body
    and have the goal start on its own line. The six numbered clauses are the constraints
    that were being written by hand on every goal; test_repo_contract.py asserts each one
    is present by calling this function, not by grepping the source.
    """
    return (
        "【エージェント契約（既定の共通制約）】以下はこの作業で常に守ること。\n"
        "(1) 隠離。自分の作業が終わるまで、他の走行から見える状態を変えないこと。作業ツリー・HEAD・"
        "共有ファイルは他の走行と共有されている。どうやって隠離するか、そもそも隠離が要るかは"
        "実行するあなたが判断する（読むだけの作業に隠離は不要）。\n"
        "(2) git add -A / git add . を使わず、変更したファイルだけを明示的に add する。\n"
        "(3) 指示が無い限り main にコミットしない。\n"
        "(4) 新規テストファイルは .github/workflows/ci.yml の明示リストに追加する"
        "（python scripts/check_ci_test_manifest.py で確認）。忘れると収集されず、緑に見えて"
        "一度も走らない。\n"
        "(5) テストに Windows 専用の記述を入れない（Linux の CI で落ちる）。\n"
        "(6) レビューや外部からの指摘は、まず自分で裏を取る。誤っていたら「誤りである」と"
        "根拠つきで書き、直さない。\n"
        + CONTRACT_END + "\n\n"
    )
