"""coding_discipline.py -- the shared production coding-discipline instruction block.

This is the single source of truth for the strengthened red->green / minimal-and-complete
diff discipline that gets appended to a self-verifying coding goal (one that carries a
verification `checks` gate). It was lifted INLINE out of code_task.build_goal so that the
several production entry points that emit such goals (code_task, folder_coder single mode)
share one verbatim block instead of drifting copies.

It is mirrored conceptually from bench/swe_batch_setup.py's SWE_STRONG_SELFTEST +
SWE_MINIMALITY blocks, but de-SWE-specialized for general project tasks: UNLIKE the
benchmark there is NO hidden held-out test here -- the acceptance gate IS the project's
own auto-detected tests (`checks`), so running the project's failing/relevant tests is
fully legitimate and is what we anchor red->green on.
"""
from __future__ import annotations

from relay.quality_cards import quality_cards_text


def coding_discipline_text(task_text: str = "") -> str:
    """Return the strengthened coding-discipline block (Japanese).

    Caller appends this to a goal's text ONLY when the goal has a verification gate
    (`checks` non-empty): a no_verify task has nothing to run red->green against, so
    demanding it would be noise. The returned string begins with a leading "\\n\\n" so it
    concatenates cleanly onto the end of an existing goal body.
    """
    text = ("\n\n【自己検証＝red→green を徹底】まず**編集する前に**、報告された問題を再現する小さな"
            "スクリプト/テストを書き、未修正コードで run_python か shell_exec で実行して**実際に失敗する**"
            "ことを確認せよ。その同じ再現が**通るまで**修正を続け、通ってから DONE。仕上げに、変更箇所を"
            "覆うこのプロジェクト自身のテストも実行し、回帰が無いことを確認する（ここでの受け入れ基準は"
            "隠しテストではなくプロジェクト自身のテストなので、それを実行するのが正しい）。\n"
            "再現は**報告に書かれた通りのオブジェクト・入力・シグネチャ（リテラルなクラス名・引数・呼び出し形）"
            "で組み立て**よ。似て非なる代用テストではなく、報告されたそのシナリオを動かすこと。さらに"
            "**期待される完全な出力をアサート**せよ——『例外が出ない』『N passed』では不十分で、正しい値・"
            "文字列・形まで突き合わせる。\n"
            "再現が通っても、データが流れる**全ての地点（生成→整形→消費 / producer→formatter→consumer）を"
            "漏れなく編集していなければ完全性は証明できていない**。報告されたシナリオを実際には動かさない既存の"
            "グリーンなテスト群は、何の証拠にもならない。\n"
            "**自分の再現テストを緩めて通すのは禁止**——通すためにアサーションを書き換えたくなったら、"
            "それは修正が間違っているサイン。テストではなく実装を直せ。\n"
            "\n【最小かつ完全な差分】正しい修正は通常ごく小さい（多くは数行）。**根本原因に的確に当たる"
            "最小の差分**だけを書け（余計なリファクタ・防御的コード・無関係な整形は過剰修正の元）。"
            "ただし小さすぎ・的外れも欠陥なので、変更した値や契約を使う経路は最後まで追う。\n"
            "\n【レビュー指摘は調査してから対応・検証済みの編集を捨てるな】レビュアーが反証(refute)してきたら、"
            "まず**調査してから**動け。すでに red→green で検証した編集を、それを**否定する具体的な失敗アサーションを"
            "名指しできない限り**、git checkout 等で破棄・巻き戻ししてはならない。反証は『最初からやり直せ』の合図"
            "ではなく、**さらに詰めろ**という指し示しである。")
    return text + quality_cards_text(
        task_text,
        domain="coding",
        include_output=False,
        include_paired=False,
    )
